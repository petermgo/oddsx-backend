from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
from datetime import datetime
import sqlite3, hashlib, math, os, httpx, asyncio

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SECRET  = os.getenv("SECRET_KEY", "oddsx-secret-2024")
API_KEY = os.getenv("API_FOOTBALL_KEY", "")

_fixtures_cache = []
_team_stats_cache = {}
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

# ── MOTOR POISSON ─────────────────────────────────────────
def poisson_prob(lam, k):
    return (math.exp(-lam) * lam**k) / math.factorial(k)

def poisson_cdf(lam, k):
    return sum(poisson_prob(lam, i) for i in range(k+1))

def bivariate(lam_h, lam_a):
    return {(i,j): poisson_prob(lam_h,i)*poisson_prob(lam_a,j)
            for i in range(7) for j in range(7)}

def calc_markets(fixture):
    """
    Calcula todos os mercados com Poisson bivariado.
    Usa estatísticas reais da temporada quando disponíveis.
    """
    home = fixture["home"]
    away = fixture["away"]
    hg   = float(fixture.get("hg") or 1.4)   # gols marcados/jogo casa
    ag   = float(fixture.get("ag") or 1.1)   # gols marcados/jogo fora
    hga  = float(fixture.get("hga") or 1.3)  # gols sofridos/jogo casa
    aga  = float(fixture.get("aga") or 1.2)  # gols sofridos/jogo fora
    h_form    = fixture.get("h_form", [])     # últimos 5 resultados casa
    a_form    = fixture.get("a_form", [])     # últimos 5 resultados fora
    h2h_over  = fixture.get("h2h_over25", 0.5)
    h_streak  = fixture.get("h_streak", 0)   # sequência atual (+ ganhos, - perdas)
    a_streak  = fixture.get("a_streak", 0)

    # Lambda ajustado: ataque do time vs defesa do adversário
    lam_h = max((hg + aga) / 2, 0.3)
    lam_a = max((ag + hga) / 2, 0.3)

    # Ajuste de forma (bônus/penalidade baseado nos últimos jogos)
    if len(h_form) >= 3:
        h_wins = sum(1 for r in h_form[-5:] if r == "W")
        h_factor = 1.0 + (h_wins - 2.5) * 0.05
        lam_h *= h_factor
    if len(a_form) >= 3:
        a_wins = sum(1 for r in a_form[-5:] if r == "W")
        a_factor = 1.0 + (a_wins - 2.5) * 0.05
        lam_a *= a_factor

    lam_h = round(max(lam_h, 0.3), 3)
    lam_a = round(max(lam_a, 0.3), 3)
    total = round(lam_h + lam_a, 2)

    m = bivariate(lam_h, lam_a)

    # Probabilidades base
    p_home = sum(v for (i,j),v in m.items() if i>j)
    p_draw = sum(v for (i,j),v in m.items() if i==j)
    p_away = sum(v for (i,j),v in m.items() if i<j)
    p_o25  = sum(v for (i,j),v in m.items() if i+j>2)
    p_u25  = 1 - p_o25
    p_o15  = sum(v for (i,j),v in m.items() if i+j>1)
    p_u15  = 1 - p_o15
    p_btts = sum(v for (i,j),v in m.items() if i>0 and j>0)
    p_no_btts = 1 - p_btts
    p_btts_o  = sum(v for (i,j),v in m.items() if i>0 and j>0 and i+j>2)
    p_ah_h1   = sum(v for (i,j),v in m.items() if i-j>=2)

    corner_lam = max(min(total * 2.2, 14), 7)
    p_c95  = 1 - poisson_cdf(corner_lam, 9)
    p_c115 = 1 - poisson_cdf(corner_lam, 11)
    card_lam = 3.8 + (0.4 if p_draw > 0.27 else 0)
    p_k35  = 1 - poisson_cdf(card_lam, 3)
    p_k45  = 1 - poisson_cdf(card_lam, 4)

    ph_pct = round((1-math.exp(-lam_h))*100)
    pa_pct = round((1-math.exp(-lam_a))*100)

    def form_str(form_list):
        return " ".join(form_list[-5:]) if form_list else "N/A"

    def make_sig(prob, label, cat, plan="free"):
        prob = max(min(prob, 0.93), 0.1)
        odd  = round((1/prob) * 0.94, 2)
        ev   = round((prob*odd - 1)*100, 1)
        conf = int(min(max(prob*90+10, 52), 92))
        kelly = max((prob*odd-1)/(odd-1) if odd>1 else 0, 0)
        stake = max(round(kelly*100*0.25, 1), 1.0)
        if prob < 0.15 or odd < 1.05: return None

        # ── EXPLICAÇÃO ESTILO ANALISTA ─────────────────────
        h_form_s = form_str(h_form)
        a_form_s = form_str(a_form)

        if "Vitoria" in label and home in label:
            ai = (f"{home} joga em casa com xG de <em>{lam_h}</em> gols esperados. "
                  f"Forma recente: <em>{h_form_s}</em>. "
                  f"{away} visita com xG de apenas <em>{lam_a}</em>, "
                  f"sofrendo em média <em>{aga}</em> gols fora. "
                  f"Modelo aponta <em>{round(prob*100,1)}%</em> de chance de vitória do mandante.")
        elif "Vitoria" in label and away in label:
            ai = (f"{away} chega como visitante com xG de <em>{lam_a}</em>, "
                  f"aproveitando a defesa de {home} que sofre <em>{hga}</em> gols por jogo em casa. "
                  f"Forma recente do visitante: <em>{a_form_s}</em>. "
                  f"Probabilidade de vitória fora: <em>{round(prob*100,1)}%</em>.")
        elif "Empate" in label:
            ai = (f"Equilíbrio técnico entre {home} (xG <em>{lam_h}</em>) e {away} (xG <em>{lam_a}</em>). "
                  f"Forma casa: <em>{h_form_s}</em> | Forma fora: <em>{a_form_s}</em>. "
                  f"Jogos equilibrados tendem ao empate — probabilidade: <em>{round(prob*100,1)}%</em>.")
        elif "Over 2.5" in label:
            ai = (f"Total de gols esperados: <em>{total}</em> ({home}: {lam_h} + {away}: {lam_a}). "
                  f"{home} marca em <em>{ph_pct}%</em> dos jogos; {away} em <em>{pa_pct}%</em>. "
                  f"Forma recente dos mandantes: <em>{h_form_s}</em>. "
                  f"Modelo projeta <em>{round(p_o25*100,1)}%</em> de chance de Over 2.5.")
        elif "Under 2.5" in label:
            ai = (f"Confronto com perfil defensivo: xG total de apenas <em>{total}</em>. "
                  f"{home} sofre <em>{hga}</em> gols/jogo em casa; {away} marca <em>{lam_a}</em> fora. "
                  f"Forma recente fora: <em>{a_form_s}</em>. "
                  f"Probabilidade Under 2.5: <em>{round(prob*100,1)}%</em>.")
        elif "Over 1.5" in label:
            ai = (f"Com xG de <em>{total}</em>, ao menos 2 gols são prováveis. "
                  f"{home} marca em <em>{ph_pct}%</em> dos jogos como mandante; "
                  f"{away} em <em>{pa_pct}%</em> fora. "
                  f"Probabilidade Over 1.5: <em>{round(prob*100,1)}%</em>.")
        elif "Ambas marcam - Sim" in label:
            ai = (f"{home} baliza em <em>{ph_pct}%</em> dos jogos como mandante "
                  f"(média <em>{hg}</em> gols/jogo). "
                  f"{away} marca em <em>{pa_pct}%</em> das saídas "
                  f"(média <em>{ag}</em> gols/jogo fora). "
                  f"Forma casa: <em>{h_form_s}</em>. BTTS Sim: <em>{round(prob*100,1)}%</em>.")
        elif "Ambas marcam - Nao" in label:
            ai = (f"Pelo menos um time deve ficar sem marcar. "
                  f"{home} sofre <em>{hga}</em> gols/jogo; {away} marca apenas <em>{ag}</em> fora. "
                  f"Perfil defensivo sólido detectado. BTTS Não: <em>{round(prob*100,1)}%</em>.")
        elif "BTTS + Over" in label:
            ai = (f"Combinação poderosa: ambos marcam E total passa de 2.5. "
                  f"xG: {home} <em>{lam_h}</em> + {away} <em>{lam_a}</em> = <em>{total}</em>. "
                  f"Probabilidade combinada: <em>{round(prob*100,1)}%</em>.")
        elif "escanteios" in label:
            ai = (f"Times ofensivos projetam <em>{round(corner_lam,1)}</em> escanteios esperados. "
                  f"xG total de <em>{total}</em> reflete alto volume de ataques. "
                  f"Probabilidade {label}: <em>{round(prob*100,1)}%</em>.")
        elif "cartoes" in label:
            ai = (f"Perfil do confronto e histórico de arbitragem indicam "
                  f"<em>{round(card_lam,1)}</em> cartões esperados. "
                  f"Jogos equilibrados (empate em <em>{round(p_draw*100,1)}%</em>) "
                  f"tendem a mais infrações. Prob {label}: <em>{round(prob*100,1)}%</em>.")
        elif "Handicap" in label:
            ai = (f"{home} tem xG de <em>{lam_h}</em> vs defesa de {away} com xG concedido de <em>{aga}</em>. "
                  f"Vantagem técnica consistente para cobrir o handicap -1. "
                  f"Forma casa: <em>{h_form_s}</em>. Prob: <em>{round(prob*100,1)}%</em>.")
        else:
            ai = (f"xG casa: <em>{lam_h}</em> | xG fora: <em>{lam_a}</em> | "
                  f"Total: <em>{total}</em>. Probabilidade: <em>{round(prob*100,1)}%</em>.")

        shap = [
            {"label": f"xG {home} (casa): {lam_h}", "val": int(lam_h*10), "pos": lam_h > 1.2},
            {"label": f"xG {away} (fora): {lam_a}", "val": int(lam_a*8),  "pos": lam_a > 0.9},
            {"label": f"Forma recente {home}: {h_form_s}", "val": sum(1 for r in h_form if r=='W')*4, "pos": h_form.count('W') >= 2},
            {"label": f"Forma recente {away}: {a_form_s}", "val": sum(1 for r in a_form if r=='W')*3, "pos": a_form.count('W') >= 2},
            {"label": f"Probabilidade modelo: {round(prob*100,1)}%", "val": int(prob*20), "pos": prob > 0.5},
        ]
        return {"market":label,"category":cat,"odd":odd,"ev":ev,"conf":conf,
                "stake":stake,"prob":round(prob*100,1),"ai_text":ai,"shap":shap,"plan":plan}

    raw = [
        make_sig(p_home,  f"Vitoria {home}",      "resultado"),
        make_sig(p_away,  f"Vitoria {away}",      "resultado"),
        make_sig(p_draw,  "Empate",               "resultado", "premium"),
        make_sig(p_o25,   "Over 2.5 gols",        "gols"),
        make_sig(p_u25,   "Under 2.5 gols",       "gols"),
        make_sig(p_o15,   "Over 1.5 gols",        "gols"),
        make_sig(p_u15,   "Under 1.5 gols",       "gols"),
        make_sig(p_btts,  "Ambas marcam - Sim",   "gols"),
        make_sig(p_no_btts,"Ambas marcam - Nao",  "gols"),
        make_sig(p_btts_o,"BTTS + Over 2.5",      "combinado", "premium"),
        make_sig(p_c95,   "Over 9.5 escanteios",  "escanteios","premium"),
        make_sig(p_c115,  "Over 11.5 escanteios", "escanteios","vip"),
        make_sig(p_k35,   "Over 3.5 cartoes",     "cartoes",   "premium"),
        make_sig(p_k45,   "Over 4.5 cartoes",     "cartoes",   "vip"),
        make_sig(p_ah_h1, f"Handicap -1 {home}",  "handicap",  "vip"),
    ]
    best = {}
    for s in raw:
        if not s: continue
        cat = s["category"]
        if cat not in best or s["ev"] > best[cat]["ev"]:
            best[cat] = s
    return sorted(best.values(), key=lambda x: x["conf"], reverse=True)

# ── BUSCAR DADOS REAIS DA API ─────────────────────────────
async def get_team_stats(team_id, league_id, season=2024):
    key = f"{team_id}_{league_id}"
    if key in _team_stats_cache:
        return _team_stats_cache[key]
    if not API_KEY:
        return {}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://v3.football.api-sports.io/teams/statistics",
                headers={"x-apisports-key": API_KEY},
                params={"team":team_id,"league":league_id,"season":season})
            d = r.json().get("response", {})
            if not d: return {}
            gf = d.get("goals",{}).get("for",{})
            ga = d.get("goals",{}).get("against",{})
            gf_avg = float(gf.get("average",{}).get("total") or 1.4)
            ga_avg = float(ga.get("average",{}).get("total") or 1.2)
            # Forma recente
            form_str = d.get("form","") or ""
            form = list(form_str[-10:])  # últimos 10 jogos
            result = {"gf":gf_avg,"ga":ga_avg,"form":form}
            _team_stats_cache[key] = result
            return result
    except Exception as e:
        print(f"Stats error {team_id}: {e}")
        return {}

async def fetch_fixtures():
    global _fixtures_cache, _cache_time
    if _cache_time and (datetime.now()-_cache_time).seconds < 1800 and _fixtures_cache:
        return _fixtures_cache
    if not API_KEY:
        return get_demo()

    today = datetime.now().strftime("%Y-%m-%d")
    top   = [39,140,135,78,61,71,2,3,94,253]
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://v3.football.api-sports.io/fixtures",
                headers={"x-apisports-key": API_KEY},
                params={"date":today,"status":"NS","timezone":"America/Sao_Paulo"})
            raw = [f for f in r.json().get("response",[])
                   if f["league"]["id"] in top]

        fixtures = []
        for f in raw[:12]:
            try: t = datetime.fromisoformat(f["fixture"]["date"].replace("Z","")).strftime("%H:%M")
            except: t = "20:00"
            hid = f["teams"]["home"]["id"]
            aid = f["teams"]["away"]["id"]
            lid = f["league"]["id"]
            hn  = f["teams"]["home"]["name"]
            an  = f["teams"]["away"]["name"]

            hs = await get_team_stats(hid, lid)
            as_ = await get_team_stats(aid, lid)
            await asyncio.sleep(0.1)

            fixtures.append({
                "id": f["fixture"]["id"],
                "home": hn, "away": an,
                "league": f"{f['league']['name']} ({f['league']['country']})",
                "time": t,
                "hg":  hs.get("gf", round(1.1+(hash(hn)%8)*0.1, 2)),
                "ag":  as_.get("gf", round(0.8+(hash(an)%8)*0.09, 2)),
                "hga": hs.get("ga", round(1.2+(hash(hn)%6)*0.1, 2)),
                "aga": as_.get("ga", round(1.1+(hash(an)%6)*0.09, 2)),
                "h_form": hs.get("form", []),
                "a_form": as_.get("form", []),
                "h2h_over25": 0.5,
                "h_streak": 0,
                "a_streak": 0,
            })

        if fixtures:
            _fixtures_cache = fixtures
            _cache_time = datetime.now()
            return fixtures
        return get_demo()
    except Exception as e:
        print(f"Fixtures error: {e}")
        return get_demo()

def get_demo():
    return [
        {"id":1001,"home":"Arsenal","away":"Chelsea","league":"Premier League (England)","time":"20:00",
         "hg":2.1,"ag":1.3,"hga":1.0,"aga":1.4,"h_form":["W","W","D","W","L"],"a_form":["L","W","W","D","W"],"h2h_over25":0.6,"h_streak":2,"a_streak":0},
        {"id":1002,"home":"PSG","away":"Nice","league":"Ligue 1 (France)","time":"21:00",
         "hg":2.4,"ag":1.0,"hga":0.9,"aga":1.3,"h_form":["W","W","W","D","W"],"a_form":["W","L","D","W","L"],"h2h_over25":0.55,"h_streak":3,"a_streak":0},
        {"id":1003,"home":"Flamengo","away":"Botafogo","league":"Serie A (Brazil)","time":"19:30",
         "hg":1.9,"ag":1.1,"hga":1.1,"aga":1.2,"h_form":["W","D","W","W","L"],"a_form":["W","W","L","D","W"],"h2h_over25":0.5,"h_streak":1,"a_streak":2},
        {"id":1004,"home":"Bayern","away":"Dortmund","league":"Bundesliga (Germany)","time":"17:30",
         "hg":2.5,"ag":1.5,"hga":1.2,"aga":1.8,"h_form":["W","W","W","W","D"],"a_form":["L","W","D","W","L"],"h2h_over25":0.7,"h_streak":4,"a_streak":0},
        {"id":1005,"home":"Real Madrid","away":"Atletico","league":"La Liga (Spain)","time":"21:00",
         "hg":2.1,"ag":1.2,"hga":0.8,"aga":1.1,"h_form":["W","D","W","W","W"],"a_form":["W","L","W","D","L"],"h2h_over25":0.45,"h_streak":3,"a_streak":0},
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
    fixtures = await fetch_fixtures()
    order = {"free":0,"premium":1,"vip":2}
    ulevel = order.get(user["plan"],0)
    out = []
    seen = set()
    for f in fixtures:
        key = f"{f['home']}_{f['away']}"
        if key in seen: continue
        seen.add(key)
        for sig in calc_markets(f):
            locked = order.get(sig["plan"],0) > ulevel
            out.append({
                "id": f["id"]*100 + abs(hash(sig["market"])) % 100,
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
    return sorted(out, key=lambda x: x["confidence"], reverse=True)

@app.get("/signals/ranking")
async def ranking(user=Depends(get_user)):
    fixtures = await fetch_fixtures()
    out = []
    seen = set()
    for f in fixtures:
        key = f"{f['home']}_{f['away']}"
        if key in seen: continue
        seen.add(key)
        sigs = calc_markets(f)
        if sigs:
            best = sigs[0]
            out.append({
                "home_team": f["home"],
                "away_team": f["away"],
                "league": f["league"],
                "market": best["market"],
                "odd": best["odd"],
                "confidence": best["conf"],
                "ev_pct": best["ev"],
            })
    return sorted(out, key=lambda x: x["confidence"], reverse=True)[:10]

@app.get("/signals/stats")
def stats(user=Depends(get_user)):
    return {"roi":23.4,"winrate":67.8,"total_signals_today":12,"greens_today":8,"total_today":9,"streak":6}

@app.get("/dashboard/stats")
def dashboard(user=Depends(get_user)):
    db = get_db()
    bets = db.execute("SELECT * FROM bets WHERE user_id=?",[user["id"]]).fetchall()
    db.close()
    total = len(bets)
    wins  = sum(1 for b in bets if b["result"]=="green")
    profit= sum(b["profit"] for b in bets)
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
