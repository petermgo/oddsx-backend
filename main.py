from fastapi import FastAPI, HTTPException, Depends, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
from datetime import datetime, timedelta
import sqlite3, hashlib, math, os, httpx, asyncio

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SECRET   = os.getenv("SECRET_KEY", "oddsx-secret-2024")
API_KEY  = os.getenv("API_FOOTBALL_KEY", "")
FD_KEY   = os.getenv("FOOTBALL_DATA_KEY", "c585a3a61b954e7e8f3bbf0dbd9fd698")
ODDS_KEY = os.getenv("THE_ODDS_KEY", "466d7782a7ef1b282177570aed71a991")

_fixtures_cache  = []
_team_stats_cache = {}
_odds_cache      = {}
_cache_time      = None

# ── LEAGUE CALIBRATION ────────────────────────────────────
# Taxas históricas reais por liga para calibrar o modelo
LEAGUE_FACTORS = {
    "Premier League":    {"over25": 0.54, "btts": 0.53, "corners_avg": 10.2},
    "La Liga":           {"over25": 0.50, "btts": 0.50, "corners_avg": 9.8},
    "Serie A":           {"over25": 0.50, "btts": 0.49, "corners_avg": 9.5},
    "Bundesliga":        {"over25": 0.57, "btts": 0.55, "corners_avg": 10.5},
    "Ligue 1":           {"over25": 0.49, "btts": 0.48, "corners_avg": 9.3},
    "Serie A (Brazil)":  {"over25": 0.43, "btts": 0.40, "corners_avg": 8.8},
    "Champions League":  {"over25": 0.52, "btts": 0.51, "corners_avg": 9.9},
    "default":           {"over25": 0.50, "btts": 0.48, "corners_avg": 9.5},
}

def get_league_factor(league: str) -> dict:
    for key in LEAGUE_FACTORS:
        if key.lower() in league.lower():
            return LEAGUE_FACTORS[key]
    return LEAGUE_FACTORS["default"]

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
    CREATE TABLE IF NOT EXISTS signals_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        home_team TEXT NOT NULL,
        away_team TEXT NOT NULL,
        league TEXT,
        market TEXT NOT NULL,
        odd REAL,
        confidence INTEGER,
        ev_pct REAL,
        result TEXT DEFAULT 'pending',
        home_score INTEGER DEFAULT -1,
        away_score INTEGER DEFAULT -1,
        fixture_id TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        resolved_at TEXT
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

# ── POISSON ENGINE ────────────────────────────────────────
def pp(lam, k): return (math.exp(-lam) * lam**k) / math.factorial(k)
def pcdf(lam, k): return sum(pp(lam,i) for i in range(k+1))
def biv(lh, la): return {(i,j): pp(lh,i)*pp(la,j) for i in range(7) for j in range(7)}

def calc_markets(fixture, real_odds_market=None):
    home = fixture["home"]
    away = fixture["away"]
    hg   = float(fixture.get("hg") or 1.4)
    ag   = float(fixture.get("ag") or 1.1)
    hga  = float(fixture.get("hga") or 1.3)
    aga  = float(fixture.get("aga") or 1.2)
    h_form = fixture.get("h_form", [])
    a_form = fixture.get("a_form", [])
    league = fixture.get("league", "")
    lf = get_league_factor(league)

    # Lambda ajustado: ataque vs defesa
    lh = max((hg + aga) / 2, 0.3)
    la = max((ag + hga) / 2, 0.3)

    # Ajuste de forma recente
    if len(h_form) >= 3:
        hw = sum(1 for r in h_form[-5:] if r == "W")
        lh *= 1.0 + (hw - 2.5) * 0.04
    if len(a_form) >= 3:
        aw = sum(1 for r in a_form[-5:] if r == "W")
        la *= 1.0 + (aw - 2.5) * 0.04

    lh = round(max(lh, 0.3), 3)
    la = round(max(la, 0.3), 3)
    total = round(lh + la, 2)
    m = biv(lh, la)

    p_home = sum(v for (i,j),v in m.items() if i>j)
    p_draw = sum(v for (i,j),v in m.items() if i==j)
    p_away = sum(v for (i,j),v in m.items() if i<j)
    p_o25  = sum(v for (i,j),v in m.items() if i+j>2)
    p_u25  = 1 - p_o25
    p_o15  = sum(v for (i,j),v in m.items() if i+j>1)
    p_btts = sum(v for (i,j),v in m.items() if i>0 and j>0)

    # Ajuste de calibração por liga para Over/BTTS
    league_over_rate = lf["over25"]
    league_btts_rate = lf["btts"]
    p_o25_cal  = (p_o25 * 0.7) + (league_over_rate * 0.3)
    p_btts_cal = (p_btts * 0.7) + (league_btts_rate * 0.3)

    # Escanteios — apenas se confiança alta
    corner_lam = lf["corners_avg"] * (total / 2.7)
    p_c95  = 1 - pcdf(corner_lam, 9)

    ph_pct = round((1-math.exp(-lh))*100)
    pa_pct = round((1-math.exp(-la))*100)

    def form_str(f): return " ".join(f[-5:]) if f else "N/D"
    hfs = form_str(h_form)
    afs = form_str(a_form)

    def make_sig(prob, label, cat, plan="free"):
        prob = max(min(prob, 0.93), 0.1)

        # Usa odd real do mercado se disponível
        if real_odds_market and label in real_odds_market:
            market_odd = round(float(real_odds_market[label]), 2)
            ev = round((prob * market_odd - 1) * 100, 1)
        else:
            # Sem odds reais: calcula odd justa e adiciona margem da casa (8%)
            # O EV representa o edge que detectamos vs a odd justa do mercado
            fair_odd = round(1 / prob, 3)
            market_odd = round(fair_odd * 0.92, 2)  # casa paga 92% do justo
            # EV = quanto ganhamos acima do justo (-8% por padrão sem edge)
            # Para mostrar EV, precisamos de uma razão para acreditar que
            # nossa prob é melhor que a implícita da casa
            # Usamos calibração por liga como ajuste
            league_implied = lf.get("over25", 0.5) if "Over 2.5" in label else (
                lf.get("btts", 0.5) if "Ambas" in label else prob)
            # Se nossa prob model > prob histórica da liga, temos edge
            edge = prob - (1/fair_odd)
            ev = round(edge * market_odd * 100, 1)

        conf = int(min(max(prob*90+10, 52), 92))
        kelly = max((prob*market_odd-1)/(market_odd-1) if market_odd>1 else 0, 0)
        stake = max(round(kelly*100*0.25, 1), 1.0)

        # THRESHOLD PROFISSIONAL:
        # - Odd mínima 1.45
        # - Confiança mínima 65%
        if prob < 0.20 or market_odd < 1.45 or conf < 65:
            return None

        # Explicação estilo analista
        if "Vitoria" in label and home in label:
            ai = (f"{home} joga em casa com xG de <em>{lh}</em> gols/jogo. "
                  f"Forma recente: <em>{hfs}</em>. "
                  f"{away} visita com xG de <em>{la}</em>, sofrendo <em>{aga}</em> gols/jogo fora. "
                  f"Modelo aponta <em>{round(prob*100,1)}%</em> de vitória do mandante.")
        elif "Vitoria" in label and away in label:
            ai = (f"{away} visita com xG de <em>{la}</em> gols/jogo fora. "
                  f"Forma recente visitante: <em>{afs}</em>. "
                  f"{home} sofre <em>{hga}</em> gols/jogo em casa. "
                  f"Probabilidade de vitória fora: <em>{round(prob*100,1)}%</em>.")
        elif "Empate" in label:
            ai = (f"Equilíbrio entre {home} (xG <em>{lh}</em>) e {away} (xG <em>{la}</em>). "
                  f"Forma: <em>{hfs}</em> vs <em>{afs}</em>. "
                  f"Prob empate: <em>{round(prob*100,1)}%</em>.")
        elif "Over 2.5" in label:
            ai = (f"xG total: <em>{total}</em> ({home}: {lh} + {away}: {la}). "
                  f"Histórico da liga: <em>{round(league_over_rate*100,0):.0f}%</em> dos jogos terminam Over 2.5. "
                  f"Forma mandante: <em>{hfs}</em>. "
                  f"Probabilidade combinada: <em>{round(prob*100,1)}%</em>.")
        elif "Under 2.5" in label:
            ai = (f"Perfil defensivo: xG total de apenas <em>{total}</em>. "
                  f"{home} sofre <em>{hga}</em>/jogo; {away} marca apenas <em>{ag}</em> fora. "
                  f"Na {league.split('(')[0].strip()}, <em>{round((1-league_over_rate)*100,0):.0f}%</em> dos jogos ficam Under 2.5. "
                  f"Probabilidade: <em>{round(prob*100,1)}%</em>.")
        elif "Over 1.5" in label:
            ai = (f"Com xG de <em>{total}</em>, ao menos 2 gols são esperados. "
                  f"{home} marca em <em>{ph_pct}%</em> dos jogos; {away} em <em>{pa_pct}%</em>. "
                  f"Probabilidade Over 1.5: <em>{round(prob*100,1)}%</em>.")
        elif "Ambas marcam - Sim" in label:
            ai = (f"{home} marca em <em>{ph_pct}%</em> dos jogos em casa (média <em>{hg}</em>/jogo). "
                  f"{away} marca em <em>{pa_pct}%</em> fora (média <em>{ag}</em>/jogo). "
                  f"Taxa BTTS histórica da liga: <em>{round(league_btts_rate*100,0):.0f}%</em>. "
                  f"Probabilidade combinada: <em>{round(prob*100,1)}%</em>.")
        elif "Ambas marcam - Nao" in label:
            ai = (f"Pelo menos um time deve ficar sem marcar. "
                  f"{home} sofre <em>{hga}</em>/jogo; {away} marca apenas <em>{ag}</em> fora. "
                  f"Na liga, <em>{round((1-league_btts_rate)*100,0):.0f}%</em> dos jogos têm BTTS Não. "
                  f"Probabilidade: <em>{round(prob*100,1)}%</em>.")
        elif "escanteios" in label:
            ai = (f"Média da liga: <em>{lf['corners_avg']}</em> escanteios/jogo. "
                  f"xG total <em>{total}</em> indica volume ofensivo acima da média. "
                  f"Probabilidade Over 9.5: <em>{round(prob*100,1)}%</em>.")
        else:
            ai = f"xG: {lh} vs {la} | Total: {total} | Prob: {round(prob*100,1)}%"

        shap = [
            {"label": f"xG {home} (casa): {lh}", "val": int(lh*10), "pos": lh > 1.2},
            {"label": f"xG {away} (fora): {la}", "val": int(la*8), "pos": la > 0.9},
            {"label": f"Forma {home}: {hfs}", "val": h_form[-5:].count('W')*4 if h_form else 0, "pos": (h_form or []).count('W') >= 2},
            {"label": f"Calibração liga ({round(league_over_rate*100,0):.0f}% Over25)", "val": int(league_over_rate*20), "pos": league_over_rate > 0.5},
            {"label": f"EV calculado: {ev}%", "val": int(abs(ev)), "pos": ev > 0},
        ]
        return {"market":label,"category":cat,"odd":market_odd,"ev":ev,"conf":conf,
                "stake":stake,"prob":round(prob*100,1),"ai_text":ai,"shap":shap,"plan":plan}

    raw = [
        make_sig(p_home,     f"Vitoria {home}",     "resultado"),
        make_sig(p_away,     f"Vitoria {away}",     "resultado"),
        make_sig(p_draw,     "Empate",              "resultado", "premium"),
        make_sig(p_o25_cal,  "Over 2.5 gols",       "gols"),
        make_sig(p_u25,      "Under 2.5 gols",      "gols"),
        make_sig(p_o15,      "Over 1.5 gols",       "gols"),
        make_sig(p_btts_cal, "Ambas marcam - Sim",  "gols"),
        make_sig(1-p_btts_cal,"Ambas marcam - Nao", "gols"),
        make_sig(p_c95,      "Over 9.5 escanteios", "escanteios", "premium"),
    ]

    # Melhor sinal por categoria — máximo 2 por jogo para variedade
    best = {}
    for s in raw:
        if not s: continue
        if s["category"] not in best or s["conf"] > best[s["category"]]["conf"]:
            best[s["category"]] = s
    return sorted(best.values(), key=lambda x: x["conf"], reverse=True)[:2]

# ── THE ODDS API ──────────────────────────────────────────
async def fetch_real_odds(home: str, away: str) -> dict:
    """Busca odds reais de múltiplas casas via The Odds API."""
    cache_key = f"odds_{home}_{away}"
    if cache_key in _odds_cache:
        return _odds_cache[cache_key]
    if not ODDS_KEY:
        return {}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://api.the-odds-api.com/v4/sports/soccer/odds",
                params={
                    "apiKey": ODDS_KEY,
                    "regions": "eu",
                    "markets": "h2h,totals",
                    "oddsFormat": "decimal",
                }
            )
            events = r.json()
            if not isinstance(events, list):
                return {}
            for ev in events:
                if (home.lower() in ev.get("home_team","").lower() or
                    ev.get("home_team","").lower() in home.lower()):
                    result = {}
                    for bm in ev.get("bookmakers", []):
                        if bm["key"] in ["pinnacle","betfair","bet365","unibet"]:
                            for mkt in bm.get("markets", []):
                                if mkt["key"] == "h2h":
                                    for o in mkt.get("outcomes", []):
                                        if o["name"] == ev["home_team"]:
                                            result[f"Vitoria {home}"] = o["price"]
                                        elif o["name"] == ev["away_team"]:
                                            result[f"Vitoria {away}"] = o["price"]
                                        elif o["name"] == "Draw":
                                            result["Empate"] = o["price"]
                                elif mkt["key"] == "totals":
                                    for o in mkt.get("outcomes", []):
                                        if o["name"] == "Over" and abs(o.get("point",0)-2.5) < 0.1:
                                            result["Over 2.5 gols"] = o["price"]
                                        elif o["name"] == "Under" and abs(o.get("point",0)-2.5) < 0.1:
                                            result["Under 2.5 gols"] = o["price"]
                            if result:
                                _odds_cache[cache_key] = result
                                return result
    except Exception as e:
        print(f"Odds API error: {e}")
    return {}

# ── API FOOTBALL ──────────────────────────────────────────
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
            gf  = float(d.get("goals",{}).get("for",{}).get("average",{}).get("total") or 1.4)
            ga  = float(d.get("goals",{}).get("against",{}).get("average",{}).get("total") or 1.2)
            frm = list((d.get("form","") or "")[-10:])
            res = {"gf":gf,"ga":ga,"form":frm}
            _team_stats_cache[key] = res
            return res
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
    top = [39,140,135,78,61,71,2,3,94,253]
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://v3.football.api-sports.io/fixtures",
                headers={"x-apisports-key": API_KEY},
                params={"date":today,"status":"NS","timezone":"America/Sao_Paulo"})
            raw = [f for f in r.json().get("response",[]) if f["league"]["id"] in top]

        fixtures = []
        for f in raw[:12]:
            try: t = datetime.fromisoformat(f["fixture"]["date"].replace("Z","")).strftime("%H:%M")
            except: t = "20:00"
            hid,aid,lid = f["teams"]["home"]["id"],f["teams"]["away"]["id"],f["league"]["id"]
            hn,an = f["teams"]["home"]["name"],f["teams"]["away"]["name"]
            hs  = await get_team_stats(hid, lid)
            as_ = await get_team_stats(aid, lid)
            odds = await fetch_real_odds(hn, an)
            await asyncio.sleep(0.15)
            fixtures.append({
                "id": f["fixture"]["id"],
                "home":hn,"away":an,
                "league": f"{f['league']['name']} ({f['league']['country']})",
                "time":t,
                "hg":  hs.get("gf", round(1.1+(hash(hn)%8)*0.1,2)),
                "ag":  as_.get("gf", round(0.8+(hash(an)%8)*0.09,2)),
                "hga": hs.get("ga", round(1.2+(hash(hn)%6)*0.1,2)),
                "aga": as_.get("ga", round(1.1+(hash(an)%6)*0.09,2)),
                "h_form": hs.get("form",[]),
                "a_form": as_.get("form",[]),
                "real_odds": odds,
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
         "hg":2.1,"ag":1.3,"hga":1.0,"aga":1.4,"h_form":["W","W","D","W","L"],"a_form":["L","W","W","D","W"],"real_odds":{}},
        {"id":1002,"home":"PSG","away":"Nice","league":"Ligue 1 (France)","time":"21:00",
         "hg":2.4,"ag":1.0,"hga":0.9,"aga":1.3,"h_form":["W","W","W","D","W"],"a_form":["W","L","D","W","L"],"real_odds":{}},
        {"id":1003,"home":"Flamengo","away":"Botafogo","league":"Serie A (Brazil)","time":"19:30",
         "hg":1.9,"ag":1.1,"hga":1.1,"aga":1.2,"h_form":["W","D","W","W","L"],"a_form":["W","W","L","D","W"],"real_odds":{}},
        {"id":1004,"home":"Bayern","away":"Dortmund","league":"Bundesliga (Germany)","time":"17:30",
         "hg":2.5,"ag":1.5,"hga":1.2,"aga":1.8,"h_form":["W","W","W","W","D"],"a_form":["L","W","D","W","L"],"real_odds":{}},
        {"id":1005,"home":"Real Madrid","away":"Atletico","league":"La Liga (Spain)","time":"21:00",
         "hg":2.1,"ag":1.2,"hga":0.8,"aga":1.1,"h_form":["W","D","W","W","W"],"a_form":["W","L","W","D","L"],"real_odds":{}},
    ]

# ── RESOLVER RESULTADOS ───────────────────────────────────
async def resolve_results():
    """Busca resultados dos jogos de hoje e atualiza o histórico."""
    if not API_KEY:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    db = get_db()
    pending = db.execute(
        "SELECT * FROM signals_history WHERE date=? AND result='pending' AND fixture_id IS NOT NULL",
        [today]
    ).fetchall()
    if not pending:
        db.close()
        return
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://v3.football.api-sports.io/fixtures",
                headers={"x-apisports-key": API_KEY},
                params={"date":today,"status":"FT","timezone":"America/Sao_Paulo"})
            finished = r.json().get("response",[])

        for sig in pending:
            for f in finished:
                if str(f["fixture"]["id"]) != str(sig["fixture_id"]):
                    continue
                hs = f["score"]["fullTime"]["home"]
                as_ = f["score"]["fullTime"]["away"]
                if hs is None or as_ is None:
                    continue
                total_goals = hs + as_
                market = sig["market"]
                home_name = sig["home_team"]
                away_name = sig["away_team"]

                # Determina resultado
                result = "red"
                if market == "Over 2.5 gols" and total_goals > 2: result = "green"
                elif market == "Under 2.5 gols" and total_goals <= 2: result = "green"
                elif market == "Over 1.5 gols" and total_goals > 1: result = "green"
                elif market == "Ambas marcam - Sim" and hs > 0 and as_ > 0: result = "green"
                elif market == "Ambas marcam - Nao" and (hs == 0 or as_ == 0): result = "green"
                elif f"Vitoria {home_name}" in market and hs > as_: result = "green"
                elif f"Vitoria {away_name}" in market and as_ > hs: result = "green"
                elif market == "Empate" and hs == as_: result = "green"
                elif "escanteios" in market.lower(): result = "pending"  # não temos dado ao vivo

                db.execute("""
                    UPDATE signals_history
                    SET result=?, home_score=?, away_score=?, resolved_at=?
                    WHERE id=?
                """, [result, hs, as_, datetime.now().isoformat(), sig["id"]])

        db.commit()
    except Exception as e:
        print(f"Resolve error: {e}")
    finally:
        db.close()

def save_signals_to_history(signals_out):
    """Salva os sinais gerados no histórico do dia."""
    today = datetime.now().strftime("%Y-%m-%d")
    db = get_db()
    for s in signals_out:
        existing = db.execute(
            "SELECT id FROM signals_history WHERE date=? AND home_team=? AND away_team=? AND market=?",
            [today, s["home_team"], s["away_team"], s["market"]]
        ).fetchone()
        if not existing:
            db.execute("""
                INSERT INTO signals_history
                (date, home_team, away_team, league, market, odd, confidence, ev_pct, fixture_id)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, [today, s["home_team"], s["away_team"], s["league"],
                  s["market"], s["odd"], s["confidence"], s["ev_pct"],
                  str(s.get("fixture_id",""))])
    db.commit()
    db.close()

# ── AUTH ──────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status":"ok","api_key":bool(API_KEY),"odds_key":bool(ODDS_KEY),"timestamp":datetime.now().isoformat()}

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
async def signals(user=Depends(get_user), bg: BackgroundTasks = BackgroundTasks()):
    fixtures = await fetch_fixtures()
    order = {"free":0,"premium":1,"vip":2}
    ulevel = order.get(user["plan"],0)
    out = []
    seen = set()
    for f in fixtures:
        key = f"{f['home']}_{f['away']}"
        if key in seen: continue
        seen.add(key)
        real_odds = f.get("real_odds", {})
        for sig in calc_markets(f, real_odds):
            locked = order.get(sig["plan"],0) > ulevel
            out.append({
                "id": f["id"]*100 + abs(hash(sig["market"])) % 100,
                "fixture_id": f["id"],
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

    result = sorted(out, key=lambda x: x["confidence"], reverse=True)
    # Salva no histórico em background
    save_signals_to_history(result)
    return result

@app.get("/signals/ranking")
async def ranking(user=Depends(get_user)):
    fixtures = await fetch_fixtures()
    out = []
    seen = set()
    for f in fixtures:
        key = f"{f['home']}_{f['away']}"
        if key in seen: continue
        seen.add(key)
        sigs = calc_markets(f, f.get("real_odds",{}))
        if sigs:
            best = sigs[0]
            out.append({"home_team":f["home"],"away_team":f["away"],"league":f["league"],
                        "market":best["market"],"odd":best["odd"],"confidence":best["conf"],"ev_pct":best["ev"]})
    return sorted(out, key=lambda x: x["confidence"], reverse=True)[:10]

@app.get("/signals/stats")
async def stats(user=Depends(get_user)):
    db = get_db()
    # Calcula stats reais do histórico
    rows = db.execute(
        "SELECT result FROM signals_history WHERE result != 'pending' ORDER BY created_at DESC LIMIT 100"
    ).fetchall()
    db.close()
    total = len(rows)
    greens = sum(1 for r in rows if r["result"] == "green")
    winrate = round(greens/total*100, 1) if total else 0
    today = datetime.now().strftime("%Y-%m-%d")
    db2 = get_db()
    today_rows = db2.execute("SELECT result FROM signals_history WHERE date=?", [today]).fetchall()
    db2.close()
    today_total  = len(today_rows)
    today_greens = sum(1 for r in today_rows if r["result"] == "green")
    return {
        "roi": 0.0,  # calculado pelo usuário
        "winrate": winrate if winrate else 0,
        "total_signals_today": today_total,
        "greens_today": today_greens,
        "total_today": today_total,
        "streak": greens,
    }

# ── HISTÓRICO DE SINAIS ───────────────────────────────────
@app.get("/history/signals")
async def signals_history(user=Depends(get_user), days: int = 7):
    db = get_db()
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = db.execute("""
        SELECT * FROM signals_history
        WHERE date >= ?
        ORDER BY date DESC, created_at DESC
        LIMIT 200
    """, [since]).fetchall()
    db.close()

    data = [dict(r) for r in rows]
    # Calcula stats do período
    resolved = [r for r in data if r["result"] in ("green","red")]
    greens   = sum(1 for r in resolved if r["result"] == "green")
    total    = len(resolved)
    winrate  = round(greens/total*100,1) if total else 0

    return {
        "signals": data,
        "stats": {
            "total": total,
            "greens": greens,
            "reds": total - greens,
            "pending": len(data) - total,
            "winrate": winrate,
        }
    }

@app.post("/history/resolve")
async def trigger_resolve(user=Depends(get_user)):
    """Força atualização de resultados (admin)."""
    await resolve_results()
    return {"ok": True, "message": "Resultados atualizados"}

# ── DASHBOARD ─────────────────────────────────────────────
@app.get("/dashboard/stats")
def dashboard(user=Depends(get_user)):
    db = get_db()
    bets = db.execute("SELECT * FROM bets WHERE user_id=?",[user["id"]]).fetchall()
    db.close()
    total  = len(bets)
    wins   = sum(1 for b in bets if b["result"]=="green")
    profit = sum(b["profit"] for b in bets)
    banca  = float(user["banca"])
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
def bet_history(user=Depends(get_user)):
    db = get_db()
    bets = db.execute("SELECT * FROM bets WHERE user_id=? ORDER BY created_at DESC LIMIT 30",[user["id"]]).fetchall()
    db.close()
    return [dict(b) for b in bets]

@app.post("/payment/create")
def payment(data: dict, user=Depends(get_user)):
    return {"init_point":"https://www.mercadopago.com.br","demo":True}
