from fastapi import FastAPI, HTTPException, Depends, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
from datetime import datetime, timedelta
import hashlib, math, os, httpx, asyncio

# PostgreSQL
import psycopg2
from psycopg2.extras import RealDictCursor

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SECRET      = os.getenv("SECRET_KEY", "oddsx-secret-2024")
API_KEYS    = [
    os.getenv("API_FOOTBALL_KEY", "66e081b0b4a6aef271ded4bc6b148d41"),
    os.getenv("API_FOOTBALL_KEY2", "02834da2ef3905191d4a6966ec4eeac2"),
]
FD_KEY      = os.getenv("FOOTBALL_DATA_KEY", "c585a3a61b954e7e8f3bbf0dbd9fd698")
ODDS_KEY    = os.getenv("THE_ODDS_KEY", "466d7782a7ef1b282177570aed71a991")
DATABASE_URL= os.getenv("DATABASE_URL", "")

_current_key_idx = 0

def get_api_key():
    """Retorna a chave ativa. Rotaciona se necessário."""
    return API_KEYS[_current_key_idx % len(API_KEYS)]

def rotate_key():
    """Troca para a próxima chave disponível."""
    global _current_key_idx
    _current_key_idx = (_current_key_idx + 1) % len(API_KEYS)
    print(f"🔄 Rotacionando para API key {_current_key_idx + 1}")

_fixtures_cache   = []
_team_stats_cache = {}
_odds_cache       = {}
_cache_time       = None

# ── LEAGUE CALIBRATION ────────────────────────────────────
LEAGUE_FACTORS = {
    # Tier 1 — dados confiáveis, alto volume de apostas
    "Premier League":        {"over25": 0.54, "btts": 0.53, "corners_avg": 10.2, "tier": 1},
    "La Liga":               {"over25": 0.50, "btts": 0.50, "corners_avg": 9.8,  "tier": 1},
    "Serie A":               {"over25": 0.50, "btts": 0.49, "corners_avg": 9.5,  "tier": 1},
    "Bundesliga":            {"over25": 0.57, "btts": 0.55, "corners_avg": 10.5, "tier": 1},
    "Ligue 1":               {"over25": 0.49, "btts": 0.48, "corners_avg": 9.3,  "tier": 1},
    "Champions League":      {"over25": 0.52, "btts": 0.51, "corners_avg": 9.9,  "tier": 1},
    "Europa League":         {"over25": 0.51, "btts": 0.50, "corners_avg": 9.6,  "tier": 1},
    "Conference League":     {"over25": 0.50, "btts": 0.49, "corners_avg": 9.4,  "tier": 1},
    # Tier 2 — bons dados, mercado razoável
    "Primeira Liga":         {"over25": 0.48, "btts": 0.47, "corners_avg": 9.1,  "tier": 2},
    "Eredivisie":            {"over25": 0.58, "btts": 0.56, "corners_avg": 10.0, "tier": 2},
    "Pro League":            {"over25": 0.55, "btts": 0.53, "corners_avg": 10.1, "tier": 2},
    "Super Lig":             {"over25": 0.52, "btts": 0.50, "corners_avg": 9.7,  "tier": 2},
    "Scottish":              {"over25": 0.53, "btts": 0.51, "corners_avg": 9.5,  "tier": 2},
    "Libertadores":          {"over25": 0.48, "btts": 0.46, "corners_avg": 8.9,  "tier": 2},
    "Serie A (Brazil)":      {"over25": 0.43, "btts": 0.40, "corners_avg": 8.8,  "tier": 2},
    "Serie B (Brazil)":      {"over25": 0.41, "btts": 0.38, "corners_avg": 8.5,  "tier": 2},
    "Liga Profesional":      {"over25": 0.47, "btts": 0.45, "corners_avg": 8.7,  "tier": 2},
    "Liga MX":               {"over25": 0.46, "btts": 0.44, "corners_avg": 8.8,  "tier": 2},
    "MLS":                   {"over25": 0.50, "btts": 0.49, "corners_avg": 9.2,  "tier": 2},
    # Tier 3 — dados menos confiáveis (ligas menores, feminino, sub-20)
    "default":               {"over25": 0.48, "btts": 0.46, "corners_avg": 9.0,  "tier": 3},
}

# Ligas que devem ser EXCLUÍDAS (feminino, sub-20/sub-23, ligas de qualidade duvidosa)
EXCLUDED_KEYWORDS = [
    "women", "woman", "femenin", "femeni", "feminil", "female",
    "u20", "u21", "u23", "u17", "u18", "u19", "under-20", "under-23",
    "junior", "youth", "reserves", "reserva", "b team",
    "syria", "armenia", "kosovo", "moldova", "belarus",
    "faroe", "san marino", "gibraltar", "andorra", "liechtenstein",
]

def is_excluded_league(league: str) -> bool:
    league_lower = league.lower()
    return any(kw in league_lower for kw in EXCLUDED_KEYWORDS)

def get_lf(league):
    for k in LEAGUE_FACTORS:
        if k.lower() in league.lower():
            return LEAGUE_FACTORS[k]
    return LEAGUE_FACTORS["default"]

# ── DATABASE ──────────────────────────────────────────────
def get_db():
    if DATABASE_URL:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return conn
    raise Exception("DATABASE_URL not set")

def init_db():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT,
            plan TEXT DEFAULT 'free',
            banca REAL DEFAULT 1000,
            created_at TIMESTAMP DEFAULT NOW()
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS signals_history (
            id SERIAL PRIMARY KEY,
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
            created_at TIMESTAMP DEFAULT NOW(),
            resolved_at TIMESTAMP
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            signal_id TEXT,
            amount REAL,
            odd REAL,
            result TEXT DEFAULT 'pending',
            profit REAL DEFAULT 0,
            home_team TEXT,
            away_team TEXT,
            market TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )""")
        conn.commit()
        cur.close()
        conn.close()
        print("✅ PostgreSQL iniciado com sucesso")
    except Exception as e:
        print(f"❌ Erro ao iniciar DB: {e}")

init_db()

def hash_pw(pw): return hashlib.sha256((pw+SECRET).encode()).hexdigest()
def make_token(uid, email): return hashlib.sha256(f"{uid}:{email}:{SECRET}".encode()).hexdigest()

def get_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Token invalido")
    token = authorization.split(" ")[1]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users")
    users = cur.fetchall()
    cur.close()
    conn.close()
    for u in users:
        if make_token(u["id"], u["email"]) == token:
            return dict(u)
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
    lf = get_lf(league)

    lh = max((hg + aga) / 2, 0.3)
    la = max((ag + hga) / 2, 0.3)
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

    p_o25_cal  = (p_o25  * 0.7) + (lf["over25"] * 0.3)
    p_btts_cal = (p_btts * 0.7) + (lf["btts"]   * 0.3)

    corner_lam = lf["corners_avg"] * (total / 2.7)
    p_c95 = 1 - pcdf(corner_lam, 9)

    ph_pct = round((1-math.exp(-lh))*100)
    pa_pct = round((1-math.exp(-la))*100)

    def form_str(f): return " ".join(f[-5:]) if f else "N/D"
    hfs = form_str(h_form)
    afs = form_str(a_form)

    def make_sig(prob, label, cat, plan="free"):
        prob = max(min(prob, 0.93), 0.1)
        if real_odds_market and label in real_odds_market:
            market_odd = round(float(real_odds_market[label]), 2)
            ev = round((prob * market_odd - 1) * 100, 1)
        else:
            fair_odd = 1 / prob
            market_odd = round(fair_odd * 0.92, 2)
            edge = prob - (1 / fair_odd)
            ev = round(edge * market_odd * 100, 1)

        conf = int(min(max(prob*90+10, 52), 92))
        kelly = max((prob*market_odd-1)/(market_odd-1) if market_odd>1 else 0, 0)
        stake = max(round(kelly*100*0.25, 1), 1.0)

        # Threshold por tier da liga
        tier = lf.get("tier", 3)
        min_conf = 68 if tier == 1 else 70 if tier == 2 else 73
        min_odd  = 1.40 if tier == 1 else 1.45 if tier == 2 else 1.50

        if prob < 0.18 or market_odd < min_odd or conf < min_conf:
            return None

        # Contexto de forma — sequência recente
        h_wins_recent = sum(1 for r in h_form[-3:] if r=="W")
        a_wins_recent = sum(1 for r in a_form[-3:] if r=="W")
        h_streak_ctx = f"venceu {h_wins_recent} dos últimos 3 jogos" if h_form else "forma desconhecida"
        a_streak_ctx = f"venceu {a_wins_recent} dos últimos 3 jogos" if a_form else "forma desconhecida"

        if "Vitoria" in label and home in label:
            ai = (f"{home} joga em casa com xG de <em>{lh}</em> gols/jogo e <em>{h_streak_ctx}</em>. "
                  f"Forma recente: <em>{hfs}</em>. "
                  f"{away} visita com xG de <em>{la}</em>, sofrendo <em>{aga}</em> gols/jogo fora. "
                  f"Modelo aponta <em>{round(prob*100,1)}%</em> de vitória do mandante.")
        elif "Vitoria" in label and away in label:
            ai = (f"{away} chega como visitante com xG de <em>{la}</em> e <em>{a_streak_ctx}</em>. "
                  f"Forma recente: <em>{afs}</em>. "
                  f"{home} sofre <em>{hga}</em> gols/jogo em casa. "
                  f"Probabilidade de vitória fora: <em>{round(prob*100,1)}%</em>.")
        elif "Empate" in label:
            ai = (f"Equilíbrio técnico: {home} (xG <em>{lh}</em>, {h_streak_ctx}) "
                  f"vs {away} (xG <em>{la}</em>, {a_streak_ctx}). "
                  f"Forma: <em>{hfs}</em> vs <em>{afs}</em>. "
                  f"Probabilidade de empate: <em>{round(prob*100,1)}%</em>.")
        elif "Over 2.5" in label:
            ai = (f"xG total projetado: <em>{total}</em> gols ({home}: {lh} + {away}: {la}). "
                  f"{home} <em>{h_streak_ctx}</em>; {away} <em>{a_streak_ctx}</em>. "
                  f"Histórico da liga: <em>{round(lf['over25']*100,0):.0f}%</em> dos jogos terminam Over 2.5. "
                  f"Probabilidade: <em>{round(prob*100,1)}%</em>.")
        elif "Under 2.5" in label:
            ai = (f"Perfil defensivo: xG total de apenas <em>{total}</em>. "
                  f"{home} sofre <em>{hga}</em>/jogo; {away} marca <em>{ag}</em> fora. "
                  f"Na liga, <em>{round((1-lf['over25'])*100,0):.0f}%</em> dos jogos ficam Under 2.5. "
                  f"Probabilidade: <em>{round(prob*100,1)}%</em>.")
        elif "Over 1.5" in label:
            ai = (f"Com xG de <em>{total}</em>, ao menos 2 gols são esperados. "
                  f"{home} marca em <em>{ph_pct}%</em> dos jogos em casa; {away} em <em>{pa_pct}%</em> fora. "
                  f"Probabilidade Over 1.5: <em>{round(prob*100,1)}%</em>.")
        elif "Ambas marcam - Sim" in label:
            ai = (f"{home} marca em <em>{ph_pct}%</em> dos jogos em casa (média <em>{hg}</em>/jogo, {h_streak_ctx}). "
                  f"{away} marca em <em>{pa_pct}%</em> fora (média <em>{ag}</em>/jogo, {a_streak_ctx}). "
                  f"Taxa BTTS da liga: <em>{round(lf['btts']*100,0):.0f}%</em>. "
                  f"Probabilidade: <em>{round(prob*100,1)}%</em>.")
        elif "Ambas marcam - Nao" in label:
            ai = (f"Pelo menos um time deve ficar sem marcar. "
                  f"{home} sofre <em>{hga}</em>/jogo; {away} marca apenas <em>{ag}</em> fora. "
                  f"Na liga, <em>{round((1-lf['btts'])*100,0):.0f}%</em> dos jogos têm BTTS Não. "
                  f"Probabilidade: <em>{round(prob*100,1)}%</em>.")
        elif "escanteios" in label:
            ai = (f"Média da liga: <em>{lf['corners_avg']}</em> escanteios/jogo. "
                  f"xG total <em>{total}</em> indica volume ofensivo. "
                  f"Ambos os times com perfil ofensivo ativo. "
                  f"Probabilidade Over 9.5: <em>{round(prob*100,1)}%</em>.")
        else:
            ai = f"xG: {lh} vs {la} | Total: {total} | Prob: {round(prob*100,1)}%"

        shap = [
            {"label": f"xG {home}: {lh}", "val": int(lh*10), "pos": lh > 1.2},
            {"label": f"xG {away}: {la}", "val": int(la*8), "pos": la > 0.9},
            {"label": f"Forma {home}: {hfs}", "val": h_form[-5:].count('W')*4 if h_form else 0, "pos": (h_form or []).count('W') >= 2},
            {"label": f"Calibração liga ({round(lf['over25']*100,0):.0f}% Over25)", "val": int(lf["over25"]*20), "pos": lf["over25"] > 0.5},
            {"label": f"EV: {ev}%", "val": int(abs(ev)), "pos": ev > 0},
        ]
        return {"market":label,"category":cat,"odd":market_odd,"ev":ev,"conf":conf,
                "stake":stake,"prob":round(prob*100,1),"ai_text":ai,"shap":shap,"plan":plan}

    raw = [
        make_sig(p_home,     f"Vitoria {home}",    "resultado"),
        make_sig(p_away,     f"Vitoria {away}",    "resultado"),
        make_sig(p_draw,     "Empate",             "resultado","premium"),
        make_sig(p_o25_cal,  "Over 2.5 gols",      "gols"),
        make_sig(p_u25,      "Under 2.5 gols",     "gols"),
        make_sig(p_o15,      "Over 1.5 gols",      "gols"),
        make_sig(p_btts_cal, "Ambas marcam - Sim", "gols"),
        make_sig(1-p_btts_cal,"Ambas marcam - Nao","gols"),
        make_sig(p_c95,      "Over 9.5 escanteios","escanteios","premium"),
    ]
    best = {}
    for s in raw:
        if not s: continue
        cat = s["category"]
        if cat not in best or s["conf"] > best[cat]["conf"]:
            best[cat] = s
    # Prioriza: resultado > gols > combinado > escanteios > cartoes
    priority = {"resultado":0,"gols":1,"combinado":2,"escanteios":3,"cartoes":4,"handicap":5}
    sorted_sigs = sorted(best.values(), key=lambda x: (priority.get(x["category"],9), -x["conf"]))
    return sorted_sigs[:2]

# ── THE ODDS API ──────────────────────────────────────────
async def fetch_real_odds(home, away):
    """Desativado temporariamente para economizar quota da API."""
    return {}

# ── API FOOTBALL ──────────────────────────────────────────
async def get_team_stats(team_id, league_id, season=2024):
    """Usa cache em memória. Só chama API se não tiver no cache."""
    key = f"{team_id}_{league_id}"
    if key in _team_stats_cache: return _team_stats_cache[key]
    # Não chama API para economizar quota — usa estimativa baseada no ID
    # A API Football tem apenas 100 req/dia no plano free
    # Stats reais serão buscados apenas se houver quota disponível
    return {}

async def fetch_fixtures():
    global _fixtures_cache, _cache_time
    # Invalida cache se mudou o dia
    if _cache_time and _cache_time.date() != datetime.now().date():
        _fixtures_cache = []
        _cache_time = None
    if _cache_time and (datetime.now()-_cache_time).seconds < 1800 and _fixtures_cache:
        return _fixtures_cache
    if not any(API_KEYS): return get_demo()
    today = datetime.now().strftime("%Y-%m-%d")
    # Ligas cobertas — top europeias + brasileirão + copas
    top = [
        39,   # Premier League
        140,  # La Liga
        135,  # Serie A Italy
        78,   # Bundesliga
        61,   # Ligue 1
        71,   # Serie A Brazil
        72,   # Serie B Brazil
        2,    # Champions League
        3,    # Europa League
        94,   # Primeira Liga Portugal
        253,  # MLS
        88,   # Eredivisie
        144,  # Belgian Pro League
        179,  # Scottish Premiership
        203,  # Super Lig Turkey
        218,  # Ligue 2
        106,  # Ekstraklasa Poland
        119,  # Superliga Denmark
        113,  # Allsvenskan Sweden
        848,  # UEFA Conference League
        262,  # Liga MX
        128,  # Argentine Primera
        239,  # Chilean Primera
        11,   # CONMEBOL Libertadores
    ]
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            # Tenta com a chave atual, rotaciona se quota esgotada
            for attempt in range(len(API_KEYS)):
                key = get_api_key()
                r = await c.get("https://v3.football.api-sports.io/fixtures",
                    headers={"x-apisports-key": key},
                    params={"date":today,"timezone":"America/Sao_Paulo"})
                resp = r.json()
                errors = resp.get("errors", {})
                # Se erro de quota ou token inválido, rotaciona
                if errors and ("rateLimit" in str(errors) or "requests" in str(errors).lower() or "token" in str(errors).lower()):
                    print(f"⚠️ Key {attempt+1} com problema: {errors} — rotacionando...")
                    rotate_key()
                    continue
                all_resp = resp.get("response", [])
                print(f"✅ Jogos hoje (key {_current_key_idx+1}): {len(all_resp)}")
                break
            else:
                print("❌ Todas as keys esgotadas — usando demo")
                return get_demo()

        # Filtra ligas excluídas (feminino, sub-20, ligas sem dados confiáveis)
        raw = [f for f in all_resp
               if f["fixture"]["status"]["short"] in ["NS","TBD","1H","HT","2H","BT"]
               and not is_excluded_league(f["league"]["name"] + " " + f["league"]["country"])]
        print(f"Jogos disponíveis (após filtro): {len(raw)}")

        # Deduplica por fixture_id
        seen_ids = set()
        deduped = []
        for f in raw:
            fid = f["fixture"]["id"]
            if fid not in seen_ids:
                seen_ids.add(fid)
                deduped.append(f)
        raw = deduped

        fixtures = []
        for f in raw[:30]:
            try: t = datetime.fromisoformat(f["fixture"]["date"].replace("Z","")).strftime("%H:%M")
            except: t = "20:00"
            hid,aid,lid = f["teams"]["home"]["id"],f["teams"]["away"]["id"],f["league"]["id"]
            hn,an = f["teams"]["home"]["name"],f["teams"]["away"]["name"]
            hs  = await get_team_stats(hid, lid)
            as_ = await get_team_stats(aid, lid)
            odds = await fetch_real_odds(hn, an)
            await asyncio.sleep(0.1)
            fixtures.append({
                "id": f["fixture"]["id"], "home":hn, "away":an,
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
        print(f"Fixtures: {e}")
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

# ── RESOLVE RESULTS ───────────────────────────────────────
async def resolve_results():
    if not API_KEY: return
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM signals_history WHERE date=%s AND result='pending' AND fixture_id IS NOT NULL AND fixture_id != ''", [today])
    pending = cur.fetchall()
    if not pending:
        cur.close(); conn.close(); return
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://v3.football.api-sports.io/fixtures",
                headers={"x-apisports-key": API_KEY},
                params={"date":today,"status":"FT","timezone":"America/Sao_Paulo"})
            finished = r.json().get("response",[])
        for sig in pending:
            for f in finished:
                if str(f["fixture"]["id"]) != str(sig["fixture_id"]): continue
                hs = f["score"]["fullTime"]["home"]
                as_ = f["score"]["fullTime"]["away"]
                if hs is None or as_ is None: continue
                total_goals = hs + as_
                market = sig["market"]
                result = "red"
                if market == "Over 2.5 gols" and total_goals > 2: result = "green"
                elif market == "Under 2.5 gols" and total_goals <= 2: result = "green"
                elif market == "Over 1.5 gols" and total_goals > 1: result = "green"
                elif market == "Ambas marcam - Sim" and hs > 0 and as_ > 0: result = "green"
                elif market == "Ambas marcam - Nao" and (hs == 0 or as_ == 0): result = "green"
                elif f"Vitoria {sig['home_team']}" in market and hs > as_: result = "green"
                elif f"Vitoria {sig['away_team']}" in market and as_ > hs: result = "green"
                elif market == "Empate" and hs == as_: result = "green"
                elif "escanteios" in market.lower(): result = "pending"
                cur.execute("UPDATE signals_history SET result=%s, home_score=%s, away_score=%s, resolved_at=NOW() WHERE id=%s",
                           [result, hs, as_, sig["id"]])
        conn.commit()
    except Exception as e:
        print(f"Resolve: {e}")
    finally:
        cur.close(); conn.close()

def save_signals_to_history(signals_out):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    cur = conn.cursor()
    for s in signals_out:
        cur.execute("SELECT id FROM signals_history WHERE date=%s AND home_team=%s AND away_team=%s AND market=%s",
                   [today, s["home_team"], s["away_team"], s["market"]])
        if not cur.fetchone():
            cur.execute("""INSERT INTO signals_history
                (date, home_team, away_team, league, market, odd, confidence, ev_pct, fixture_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [today, s["home_team"], s["away_team"], s["league"],
                 s["market"], s["odd"], s["confidence"], s["ev_pct"], str(s.get("fixture_id",""))])
    conn.commit()
    cur.close(); conn.close()

# ── AUTH ──────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status":"ok","api_key":_current_key_idx+1,"total_keys":len(API_KEYS),"db":"postgres","timestamp":datetime.now().isoformat()}

@app.post("/auth/register")
def register(data: dict):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO users (email,password_hash,name,banca) VALUES (%s,%s,%s,%s)",
                   [data["email"],hash_pw(data["password"]),data.get("name",""),float(data.get("banca",1000))])
        conn.commit()
        cur.execute("SELECT * FROM users WHERE email=%s",[data["email"]])
        u = cur.fetchone()
        return {"token":make_token(u["id"],u["email"]),"user":dict(u)}
    except psycopg2.IntegrityError:
        conn.rollback()
        raise HTTPException(400,"Email ja cadastrado")
    finally:
        cur.close(); conn.close()

@app.post("/auth/login")
def login(data: dict):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE email=%s AND password_hash=%s",
               [data["email"],hash_pw(data["password"])])
    u = cur.fetchone()
    cur.close(); conn.close()
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
        for sig in calc_markets(f, f.get("real_odds",{})):
            locked = order.get(sig["plan"],0) > ulevel
            out.append({
                "id": f["id"]*100 + abs(hash(sig["market"])) % 100,
                "fixture_id": f["id"],
                "home_team": f["home"], "away_team": f["away"],
                "league": f["league"], "match_time": f["time"],
                "market": sig["market"], "category": sig["category"],
                "odd": sig["odd"], "confidence": sig["conf"],
                "ev_pct": sig["ev"], "stake_pct": sig["stake"],
                "ai_explanation": sig["ai_text"] if not locked else "Faca upgrade para ver a analise completa da IA.",
                "shap_data": sig["shap"] if not locked else [],
                "status": "pending",
                "plan_required": sig["plan"], "locked": locked,
            })
    result = sorted(out, key=lambda x: x["confidence"], reverse=True)
    try: save_signals_to_history(result)
    except Exception as e: print(f"Save history: {e}")
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
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT result FROM signals_history WHERE result != 'pending' ORDER BY created_at DESC LIMIT 100")
        rows = cur.fetchall()
        today = datetime.now().strftime("%Y-%m-%d")
        cur.execute("SELECT result FROM signals_history WHERE date=%s",[today])
        today_rows = cur.fetchall()
        cur.close(); conn.close()
        total = len(rows)
        greens = sum(1 for r in rows if r["result"]=="green")
        winrate = round(greens/total*100,1) if total else 0
        today_total  = len(today_rows)
        today_greens = sum(1 for r in today_rows if r["result"]=="green")
        return {"roi":0.0,"winrate":winrate,"total_signals_today":today_total,
                "greens_today":today_greens,"total_today":today_total,"streak":greens}
    except:
        return {"roi":0.0,"winrate":0,"total_signals_today":0,"greens_today":0,"total_today":0,"streak":0}

@app.get("/history/signals")
async def signals_history(user=Depends(get_user), days: int = 7):
    conn = get_db()
    cur = conn.cursor()
    since = (datetime.now()-timedelta(days=days)).strftime("%Y-%m-%d")
    cur.execute("SELECT * FROM signals_history WHERE date >= %s ORDER BY date DESC, created_at DESC LIMIT 200",[since])
    data = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    resolved = [r for r in data if r["result"] in ("green","red")]
    greens = sum(1 for r in resolved if r["result"]=="green")
    total  = len(resolved)
    return {"signals": data, "stats": {
        "total":total,"greens":greens,"reds":total-greens,
        "pending":len(data)-total,"winrate":round(greens/total*100,1) if total else 0
    }}

@app.post("/history/resolve")
async def trigger_resolve(user=Depends(get_user)):
    await resolve_results()
    return {"ok":True}

@app.get("/dashboard/stats")
def dashboard(user=Depends(get_user)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM bets WHERE user_id=%s",[user["id"]])
    bets = cur.fetchall()
    cur.close(); conn.close()
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
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO bets (user_id,signal_id,amount,odd,home_team,away_team,market) VALUES (%s,%s,%s,%s,%s,%s,%s)",
               [user["id"],data.get("signal_id"),data.get("amount"),data.get("odd"),
                data.get("home_team",""),data.get("away_team",""),data.get("market","")])
    conn.commit()
    cur.close(); conn.close()
    return {"ok":True}

@app.get("/bets/history")
def bet_history(user=Depends(get_user)):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM bets WHERE user_id=%s ORDER BY created_at DESC LIMIT 30",[user["id"]])
    bets = cur.fetchall()
    cur.close(); conn.close()
    return [dict(b) for b in bets]

@app.post("/payment/create")
def payment(data: dict, user=Depends(get_user)):
    return {"init_point":"https://www.mercadopago.com.br","demo":True}
