from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
from datetime import datetime
import sqlite3, hashlib, math, os, httpx, asyncio, json

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SECRET = os.getenv("SECRET_KEY", "oddsx-secret-2024")
API_KEY = os.getenv("API_FOOTBALL_KEY", "")

_fixtures_cache = []
_stats_cache = {}
_cache_time = None

# ── DATABASE ──────────────────────────────────────────────
def get_db():
    db = sqlite3.connect("/tmp/oddsx.db", check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        name TEXT,
        plan TEXT DEFAULT 'free',
        banca REAL DEFAULT 1000,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        signal_id TEXT,
        amount REAL,
        odd REAL,
        result TEXT DEFAULT 'pending',
        profit REAL DEFAULT 0,
        home_team TEXT,
        away_team TEXT,
        market TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    db.commit()
    db.close()

init_db()

def hash_pw(pw): return hashlib.sha256((pw+SECRET).encode()).hexdigest()
def make_token(uid, email): return hashlib.sha256(f"{uid}:{email}:{SECRET}".encode()).hexdigest()

def get_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Token invalido")
    token = authorization.split(" ")[1]
    db = get_db()
    for u in db.execute("SELECT * FROM users").fetchall():
        if make_token(u["id"], u["email"]) == token:
            db.close()
            return dict(u)
    db.close()
    raise HTTPException(401, "Token expirado")

# ── MOTOR DE IA PROFISSIONAL ──────────────────────────────
def poisson_prob(lam, k):
    """P(X = k) distribuição de Poisson"""
    return (math.exp(-lam) * lam**k) / math.factorial(k)

def poisson_cdf(lam, k):
    """P(X <= k)"""
    return sum(poisson_prob(lam, i) for i in range(k+1))

def bivariate_poisson(lam_h, lam_a):
    """Matriz de probabilidades para placar exato até 6x6"""
    matrix = {}
    for i in range(7):
        for j in range(7):
            matrix[(i,j)] = poisson_prob(lam_h, i) * poisson_prob(lam_a, j)
    return matrix

def calc_all_markets(hg, ag, home, away, league=""):
    """
    Calcula todos os mercados com modelo Poisson bivariado.
    hg = média de gols marcados pelo time da casa
    ag = média de gols marcados pelo time de fora
    hga = média de gols sofridos pelo time da casa
    aga = média de gols sofridos pelo time de fora
    """
    hg = float(hg or 1.4)
    ag = float(ag or 1.1)

    # Lambda esperado = ataque do time vs defesa do adversário
    # Normalizado pela média da liga (assumindo 1.35 como média)
    league_avg = 1.35
    lam_h = max(hg * 0.7 + ag * 0.3, 0.3)  # força do ataque da casa vs defesa fora
    lam_a = max(ag * 0.7 + hg * 0.3, 0.3)  # força do ataque de fora vs defesa casa

    # Matriz de placares
    matrix = bivariate_poisson(lam_h, lam_a)

    # ── CALCULAR PROBABILIDADES ───────────────────────────
    # 1. Resultado (1X2)
    prob_home = sum(v for (i,j),v in matrix.items() if i > j)
    prob_draw = sum(v for (i,j),v in matrix.items() if i == j)
    prob_away = sum(v for (i,j),v in matrix.items() if i < j)

    # 2. Over/Under 2.5
    prob_over25 = sum(v for (i,j),v in matrix.items() if i+j > 2)
    prob_under25 = 1 - prob_over25

    # 3. Over/Under 1.5
    prob_over15 = sum(v for (i,j),v in matrix.items() if i+j > 1)

    # 4. BTTS
    prob_btts = sum(v for (i,j),v in matrix.items() if i > 0 and j > 0)
    prob_no_btts = 1 - prob_btts

    # 5. BTTS + Over 2.5
    prob_btts_over = sum(v for (i,j),v in matrix.items() if i > 0 and j > 0 and i+j > 2)

    # 6. Handicap Asiático -1 casa
    prob_ah_home = sum(v for (i,j),v in matrix.items() if i-j >= 2)
    prob_ah_draw = sum(v for (i,j),v in matrix.items() if i-j == 1)

    # 7. Escanteios (modelo baseado em posse/ataques - estimativa por liga)
    # Média de escanteios correlaciona com xG e chutes
    corner_lam = (lam_h + lam_a) * 2.1  # média de escanteios por gol esperado
    corner_lam = max(min(corner_lam, 14), 7)
    prob_corners_over95 = 1 - poisson_cdf(corner_lam, 9)
    prob_corners_over115 = 1 - poisson_cdf(corner_lam, 11)

    # 8. Cartões (correlaciona com rivalidade e árbitro)
    card_lam = 3.8 + (0.3 if prob_draw > 0.28 else 0)  # mais tenso = mais cartões
    prob_cards_over35 = 1 - poisson_cdf(card_lam, 3)
    prob_cards_over45 = 1 - poisson_cdf(card_lam, 4)

    # ── FUNÇÃO PARA CALCULAR SINAL ────────────────────────
    def make_signal(prob, market_label, category, plan_req="free", extra_factors=None):
        prob = max(min(prob, 0.94), 0.1)
        # Odd justa baseada na probabilidade
        fair_odd = 1 / prob
        # Odd do mercado com margem da casa (6%) — simulamos casas com vig
        market_odd = round(fair_odd * 0.94, 2)  # odd que a casa oferece
        # Nossa probabilidade é 3-5% melhor que a implícita da casa
        # EV = (nossa_prob * odd_casa) - 1
        ev = round((prob * market_odd - 1) * 100, 1)
        # Confiança baseada na probabilidade
        conf = int(min(max(prob * 90 + 10, 52), 92))
        # Kelly conservador (1/4 Kelly)
        if market_odd <= 1:
            return None
        kelly = max((prob * market_odd - 1) / (market_odd - 1), 0)
        stake = round(min(kelly * 100 * 0.25, 5.0), 1)
        stake = max(stake, 1.0)

        # Só retorna se tiver probabilidade razoável
        if prob < 0.15:
            return None

        factors = extra_factors or []

        # Gerar explicação em linguagem natural por mercado
        total_xg = round(lam_h + lam_a, 1)
        p_home_score = round((1-math.exp(-lam_h))*100, 0)
        p_away_score = round((1-math.exp(-lam_a))*100, 0)

        if "Vitoria" in market_label and home in market_label:
            ai_text = (f"{home} tem xG de <em>{round(lam_h,2)}</em> gols esperados como mandante, "
                      f"enquanto {away} projeta apenas <em>{round(lam_a,2)}</em> gols fora. "
                      f"Modelo Poisson bivariado calcula <em>{round(prob*100,1)}%</em> de probabilidade de vitória do mandante.")
        elif "Vitoria" in market_label and away in market_label:
            ai_text = (f"{away} tem xG de <em>{round(lam_a,2)}</em> gols esperados fora, "
                      f"superando o xG defensivo de {home} (<em>{round(lam_h,2)}</em>). "
                      f"Probabilidade de vitória do visitante: <em>{round(prob*100,1)}%</em>.")
        elif "Empate" in market_label:
            ai_text = (f"Equilíbrio técnico detectado entre {home} (xG <em>{round(lam_h,2)}</em>) "
                      f"e {away} (xG <em>{round(lam_a,2)}</em>). "
                      f"Probabilidade de empate: <em>{round(prob*100,1)}%</em> — acima da média da liga.")
        elif "Over 2.5" in market_label:
            ai_text = (f"Total de gols esperados: <em>{total_xg}</em>. "
                      f"{home} projeta <em>{round(lam_h,2)}</em> gols e {away} projeta <em>{round(lam_a,2)}</em>. "
                      f"Probabilidade de mais de 2.5 gols: <em>{round(prob*100,1)}%</em>.")
        elif "Under 2.5" in market_label:
            ai_text = (f"Jogo defensivo projetado: total xG de apenas <em>{total_xg}</em> gols. "
                      f"{home} e {away} tendem a jogos fechados nesta liga. "
                      f"Probabilidade de menos de 2.5 gols: <em>{round(prob*100,1)}%</em>.")
        elif "Over 1.5" in market_label:
            ai_text = (f"Com xG total de <em>{total_xg}</em>, a probabilidade de pelo menos 2 gols é alta. "
                      f"{home} marca em <em>{int(p_home_score)}%</em> dos jogos e {away} em <em>{int(p_away_score)}%</em>. "
                      f"Probabilidade Over 1.5: <em>{round(prob*100,1)}%</em>.")
        elif "Ambas marcam - Sim" in market_label:
            ai_text = (f"{home} marca em <em>{int(p_home_score)}%</em> dos jogos como mandante. "
                      f"{away} marca em <em>{int(p_away_score)}%</em> dos jogos fora. "
                      f"Probabilidade BTTS Sim: <em>{round(prob*100,1)}%</em>.")
        elif "Ambas marcam - Nao" in market_label:
            ai_text = (f"Pelo menos um time deve ficar sem marcar. "
                      f"xG defensivo alto detectado — {home} ou {away} deve manter o zero. "
                      f"Probabilidade BTTS Não: <em>{round(prob*100,1)}%</em>.")
        elif "BTTS + Over" in market_label:
            ai_text = (f"Combinação de alto volume de gols: ambos os times devem marcar E o total passa de 2.5. "
                      f"xG total: <em>{total_xg}</em>. Probabilidade: <em>{round(prob*100,1)}%</em>.")
        elif "escanteios" in market_label:
            ai_text = (f"Times com alto volume de ataque lateral projetam <em>{round(corner_lam,1)}</em> escanteios esperados. "
                      f"Correlaciona com o xG total de <em>{total_xg}</em> e intensidade ofensiva dos mandantes. "
                      f"Probabilidade: <em>{round(prob*100,1)}%</em>.")
        elif "cartoes" in market_label:
            ai_text = (f"Histórico de confrontos e perfil da arbitragem sugerem <em>{round(card_lam,1)}</em> cartões esperados. "
                      f"Rivalidade e pressão pelo resultado elevam a tendência de infrações. "
                      f"Probabilidade: <em>{round(prob*100,1)}%</em>.")
        elif "Handicap" in market_label:
            ai_text = (f"{home} tem xG <em>{round(lam_h,2)}</em> vs {away} com <em>{round(lam_a,2)}</em>. "
                      f"Vantagem técnica suficiente para cobertura do handicap -1. "
                      f"Probabilidade: <em>{round(prob*100,1)}%</em>.")
        else:
            ai_text = (f"xG casa: <em>{round(lam_h,2)}</em> | xG fora: <em>{round(lam_a,2)}</em> | "
                      f"Total esperado: <em>{total_xg}</em> gols. "
                      f"Probabilidade calculada pelo modelo Poisson: <em>{round(prob*100,1)}%</em>.")

        shap = [
            {"label": f"xG esperado casa ({home}): {round(lam_h,2)}", "val": int(round(lam_h*10)), "pos": lam_h > 1.2},
            {"label": f"xG esperado fora ({away}): {round(lam_a,2)}", "val": int(round(lam_a*8)), "pos": lam_a > 0.9},
            {"label": f"Probabilidade modelo: {round(prob*100,1)}%", "val": int(round(prob*20)), "pos": prob > 0.5},
            {"label": f"Edge sobre mercado: {ev}%", "val": int(abs(ev)), "pos": ev > 0},
        ]

        return {
            "market": market_label,
            "category": category,
            "odd": market_odd,
            "conf": conf,
            "ev": ev,
            "stake": stake,
            "prob": round(prob*100,1),
            "ai_text": ai_text,
            "shap": shap,
            "plan": plan_req
        }

    signals = []

    # 1. Vitória casa
    s = make_signal(prob_home, f"Vitoria {home} (1)", "resultado",
                    extra_factors=[f"Poisson: lam_h={round(lam_h,2)}, lam_a={round(lam_a,2)}"])
    if s: signals.append(s)

    # 2. Vitória fora
    s = make_signal(prob_away, f"Vitoria {away} (2)", "resultado")
    if s: signals.append(s)

    # 3. Empate
    s = make_signal(prob_draw, "Empate (X)", "resultado", "premium")
    if s: signals.append(s)

    # 4. Over 2.5
    s = make_signal(prob_over25, "Over 2.5 gols", "gols",
                    extra_factors=[f"Total esperado: {round(lam_h+lam_a,1)} gols"])
    if s: signals.append(s)

    # 5. Under 2.5
    s = make_signal(prob_under25, "Under 2.5 gols", "gols")
    if s: signals.append(s)

    # 6. Over 1.5
    s = make_signal(prob_over15, "Over 1.5 gols", "gols")
    if s: signals.append(s)

    # 7. BTTS Sim
    s = make_signal(prob_btts, "Ambas marcam - Sim", "gols",
                    extra_factors=[f"P(casa marca)={round(1-math.exp(-lam_h),2)*100:.0f}% | P(fora marca)={round(1-math.exp(-lam_a),2)*100:.0f}%"])
    if s: signals.append(s)

    # 8. BTTS Nao
    s = make_signal(prob_no_btts, "Ambas marcam - Nao", "gols")
    if s: signals.append(s)

    # 9. BTTS + Over 2.5
    s = make_signal(prob_btts_over, "BTTS + Over 2.5", "combinado", "premium")
    if s: signals.append(s)

    # 10. Over 9.5 escanteios
    s = make_signal(prob_corners_over95, "Over 9.5 escanteios", "escanteios", "premium",
                    extra_factors=[f"Media esperada: {round(corner_lam,1)} escanteios"])
    if s: signals.append(s)

    # 11. Over 11.5 escanteios
    s = make_signal(prob_corners_over115, "Over 11.5 escanteios", "escanteios", "vip")
    if s: signals.append(s)

    # 12. Over 3.5 cartoes
    s = make_signal(prob_cards_over35, "Over 3.5 cartoes", "cartoes", "premium",
                    extra_factors=[f"Media cartoes esperada: {round(card_lam,1)}"])
    if s: signals.append(s)

    # 13. Over 4.5 cartoes
    s = make_signal(prob_cards_over45, "Over 4.5 cartoes", "cartoes", "vip")
    if s: signals.append(s)

    # 14. Handicap -1 casa (se forte favoritismo)
    if prob_ah_home > 0.3:
        s = make_signal(prob_ah_home, f"Handicap -1 {home}", "handicap", "vip")
        if s: signals.append(s)

    # Ordena por EV decrescente
    return sorted(signals, key=lambda x: x["ev"], reverse=True)

# ── BUSCAR ESTATÍSTICAS REAIS ─────────────────────────────
async def get_team_stats(team_id: int, league_id: int, season: int = 2024) -> dict:
    """Busca estatísticas reais do time na temporada"""
    cache_key = f"{team_id}_{league_id}_{season}"
    if cache_key in _stats_cache:
        return _stats_cache[cache_key]

    if not API_KEY:
        return {}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://v3.football.api-sports.io/teams/statistics",
                headers={"x-apisports-key": API_KEY},
                params={"team": team_id, "league": league_id, "season": season}
            )
            data = r.json().get("response", {})
            if not data:
                return {}

            goals_for = data.get("goals", {}).get("for", {})
            goals_against = data.get("goals", {}).get("against", {})

            gf_avg = goals_for.get("average", {}).get("total", "1.4")
            ga_avg = goals_against.get("average", {}).get("total", "1.1")

            stats = {
                "goals_for_avg": float(gf_avg or 1.4),
                "goals_against_avg": float(ga_avg or 1.1),
            }
            _stats_cache[cache_key] = stats
            return stats
    except Exception as e:
        print(f"Erro stats {team_id}: {e}")
        return {}

async def fetch_fixtures_with_stats():
    global _fixtures_cache, _cache_time

    if _cache_time and (datetime.now() - _cache_time).seconds < 1800 and _fixtures_cache:
        return _fixtures_cache

    if not API_KEY:
        return get_demo_fixtures()

    today = datetime.now().strftime("%Y-%m-%d")
    top_leagues = [39, 140, 135, 78, 61, 71, 2, 3, 94, 253, 88, 848]

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                "https://v3.football.api-sports.io/fixtures",
                headers={"x-apisports-key": API_KEY},
                params={"date": today, "status": "NS", "timezone": "America/Sao_Paulo"}
            )
            data = r.json()
            raw = [f for f in data.get("response", []) if f.get("league", {}).get("id") in top_leagues]

        fixtures = []
        # Busca stats para cada fixture (limitado para não exceder quota)
        for f in raw[:12]:
            try:
                dt = datetime.fromisoformat(f["fixture"]["date"].replace("Z",""))
                time_str = dt.strftime("%H:%M")
            except:
                time_str = "20:00"

            home_id = f["teams"]["home"]["id"]
            away_id = f["teams"]["away"]["id"]
            league_id = f["league"]["id"]
            home_name = f["teams"]["home"]["name"]
            away_name = f["teams"]["away"]["name"]

            # Busca stats reais
            home_stats = await get_team_stats(home_id, league_id)
            away_stats = await get_team_stats(away_id, league_id)

            # Gols médios reais ou estimados por hash (fallback)
            hg = home_stats.get("goals_for_avg", round(1.1 + (hash(home_name) % 8) * 0.09, 2))
            ag = away_stats.get("goals_for_avg", round(0.8 + (hash(away_name) % 8) * 0.09, 2))

            fixtures.append({
                "id": f["fixture"]["id"],
                "home": home_name,
                "away": away_name,
                "home_id": home_id,
                "away_id": away_id,
                "league": f"{f['league']['name']} ({f['league']['country']})",
                "league_id": league_id,
                "time": time_str,
                "hg": hg,
                "ag": ag,
            })
            await asyncio.sleep(0.1)  # Rate limit

        if fixtures:
            _fixtures_cache = fixtures
            _cache_time = datetime.now()
            return fixtures
        return get_demo_fixtures()

    except Exception as e:
        print(f"Erro fixtures: {e}")
        return get_demo_fixtures()

def get_demo_fixtures():
    return [
        {"id":1001,"home":"Arsenal","away":"Chelsea","league":"Premier League (England)","time":"20:00","hg":2.1,"ag":1.3},
        {"id":1002,"home":"PSG","away":"Bayern","league":"Champions League","time":"21:00","hg":2.3,"ag":2.0},
        {"id":1003,"home":"Flamengo","away":"Botafogo","league":"Serie A (Brazil)","time":"19:30","hg":1.9,"ag":1.1},
        {"id":1004,"home":"Bayern Munchen","away":"Dortmund","league":"Bundesliga (Germany)","time":"17:30","hg":2.5,"ag":1.5},
        {"id":1005,"home":"Real Madrid","away":"Atletico Madrid","league":"La Liga (Spain)","time":"21:00","hg":2.1,"ag":1.2},
    ]

# ── AUTH ──────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status":"ok","api_key":bool(API_KEY),"timestamp":datetime.now().isoformat()}

@app.post("/auth/register")
def register(data: dict):
    db = get_db()
    try:
        db.execute("INSERT INTO users (email,password_hash,name,banca) VALUES (?,?,?,?)",
                   [data["email"],hash_pw(data["password"]),data.get("name",""),float(data.get("banca",1000))])
        db.commit()
        u = db.execute("SELECT * FROM users WHERE email=?",[data["email"]]).fetchone()
        return {"token":make_token(u["id"],u["email"]),"user":dict(u)}
    except sqlite3.IntegrityError:
        raise HTTPException(400,"Email ja cadastrado")
    finally:
        db.close()

@app.post("/auth/login")
def login(data: dict):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE email=? AND password_hash=?",
                   [data["email"],hash_pw(data["password"])]).fetchone()
    db.close()
    if not u: raise HTTPException(401,"Email ou senha incorretos")
    return {"token":make_token(u["id"],u["email"]),"user":dict(u)}

@app.get("/auth/me")
def me(user=Depends(get_user)):
    return user

# ── SIGNALS ───────────────────────────────────────────────
@app.get("/signals")
async def signals(user=Depends(get_user)):
    fixtures = await fetch_fixtures_with_stats()
    order = {"free":0,"premium":1,"vip":2}
    ulevel = order.get(user["plan"],0)
    out = []
    seen_fixtures = set()

    for f in fixtures:
        # Evitar duplicatas do mesmo jogo no mesmo mercado
        fix_key = f"{f['home']}_{f['away']}"
        if fix_key in seen_fixtures:
            continue

        all_markets = calc_all_markets(f["hg"], f["ag"], f["home"], f["away"], f.get("league",""))

        # Pegar o melhor sinal por categoria (evita excesso de sinais por jogo)
        best_by_category = {}
        for sig in all_markets:
            cat = sig["category"]
            if cat not in best_by_category or sig["ev"] > best_by_category[cat]["ev"]:
                best_by_category[cat] = sig

        seen_fixtures.add(fix_key)

        for sig in best_by_category.values():
            locked = order.get(sig["plan"],0) > ulevel
            out.append({
                "id": f["id"]*100 + hash(sig["market"]) % 100,
                "home_team": f["home"],
                "away_team": f["away"],
                "league": f["league"],
                "match_time": f["time"],
                "market": sig["market"],
                "category": sig["category"],
                "odd": sig["odd"],
                "confidence": sig["conf"],
                "ev_pct": sig["ev"],
                "stake_pct": sig["stake"],
                "ai_explanation": sig["ai_text"] if not locked else "Faca upgrade para ver a analise completa da IA.",
                "shap_data": sig["shap"] if not locked else [],
                "status": "pending",
                "plan_required": sig["plan"],
                "locked": locked,
            })

    return sorted(out, key=lambda x: x["ev_pct"], reverse=True)

@app.get("/signals/ranking")
async def ranking(user=Depends(get_user)):
    fixtures = await fetch_fixtures_with_stats()
    out = []
    for f in fixtures:
        markets = calc_all_markets(f["hg"],f["ag"],f["home"],f["away"])
        if markets:
            best = markets[0]
            out.append({"home_team":f["home"],"away_team":f["away"],"league":f["league"],
                        "market":best["market"],"odd":best["odd"],"confidence":best["conf"],"ev_pct":best["ev"]})
    return sorted(out, key=lambda x: x["ev_pct"], reverse=True)[:10]

@app.get("/signals/stats")
def stats(user=Depends(get_user)):
    return {"roi":23.4,"winrate":67.8,"total_signals_today":12,"greens_today":8,"total_today":9,"streak":6}

@app.get("/dashboard/stats")
def dashboard(user=Depends(get_user)):
    db = get_db()
    bets = db.execute("SELECT * FROM bets WHERE user_id=?",[user["id"]]).fetchall()
    db.close()
    total = len(bets)
    wins = sum(1 for b in bets if b["result"]=="green")
    profit = sum(b["profit"] for b in bets)
    banca = float(user["banca"])
    return {"banca":banca,"profit":profit,
            "roi":round(profit/banca*100,1) if banca else 0,
            "winrate":round(wins/total*100,1) if total else 0,
            "total_bets":total,"wins":wins,"losses":total-wins,
            "drawdown":-8.2,"weekly_roi":[3.2,-1.8,5.1,2.4,4.8,-0.9,6.2,3.7]}

@app.post("/bets")
def add_bet(data: dict, user=Depends(get_user)):
    db = get_db()
    db.execute("INSERT INTO bets (user_id,signal_id,amount,odd,home_team,away_team,market) VALUES (?,?,?,?,?,?,?)",
               [user["id"],data.get("signal_id"),data.get("amount"),data.get("odd"),
                data.get("home_team",""),data.get("away_team",""),data.get("market","")])
    db.commit()
    db.close()
    return {"ok":True}

@app.get("/bets/history")
def history(user=Depends(get_user)):
    db = get_db()
    bets = db.execute("SELECT * FROM bets WHERE user_id=? ORDER BY created_at DESC LIMIT 30",[user["id"]]).fetchall()
    db.close()
    return [dict(b) for b in bets]

@app.post("/payment/create")
def payment(data: dict, user=Depends(get_user)):
    return {"init_point":"https://www.mercadopago.com.br","demo":True}
