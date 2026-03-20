from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
from typing import Optional
import sqlite3, hashlib, json, math, os
import httpx

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SECRET = os.getenv("SECRET_KEY", "oddsx-secret-2024")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")

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

def hash_password(pw):
    return hashlib.sha256((pw + SECRET).encode()).hexdigest()

def make_token(user_id, email):
    return hashlib.sha256(f"{user_id}:{email}:{SECRET}".encode()).hexdigest()

def get_current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token inválido")
    token = authorization.split(" ")[1]
    db = get_db()
    users = db.execute("SELECT * FROM users").fetchall()
    for u in users:
        if make_token(u["id"], u["email"]) == token:
            db.close()
            return dict(u)
    db.close()
    raise HTTPException(status_code=401, detail="Token expirado")

def calc_signal(hg, ag, market, home, away):
    lam = (hg or 1.4) + (ag or 1.1)
    p0 = math.exp(-lam)
    p1 = lam * math.exp(-lam)
    p2 = (lam**2 / 2) * math.exp(-lam)
    prob_over = round(1 - (p0+p1+p2), 3)
    prob_btts = round((1-math.exp(-(hg or 1.4))) * (1-math.exp(-(ag or 1.1))), 3)
    prob = prob_over if market == "over25" else prob_btts
    prob = max(min(prob, 0.94), 0.3)
    odd = round(1/prob * 1.03, 2)
    conf = int(min(max(prob*100+5, 50), 95))
    ev = round((prob*odd-1)*100, 1)
    stake = round(min(max((prob - 1/odd)/(1-1/odd)*100, 0.5), 5.0), 1)
    label = "Over 2.5 gols" if market == "over25" else "Ambas marcam (BTTS)"
    plan = "free" if conf < 80 else ("premium" if conf < 88 else "vip")
    shap = [
        {"label": f"xG esperado: {round(lam,1)} gols", "val": round(lam*8), "pos": lam > 2.5},
        {"label": f"Média gols {home} em casa", "val": round((hg or 1.4)*10), "pos": (hg or 1.4) > 1.3},
        {"label": f"Média gols {away} fora", "val": round((ag or 1.1)*8), "pos": (ag or 1.1) > 1.0},
        {"label": "Modelo Poisson calibrado", "val": round(prob*20), "pos": prob > 0.55},
        {"label": "EV positivo detectado", "val": round(ev), "pos": ev > 0},
    ]
    ai = (f"{home} tem média de <em>{hg}</em> gols. {away} marca <em>{ag}</em> fora. "
          f"Modelo projeta <em>{round(lam,1)} gols esperados</em> — probabilidade <em>{round(prob*100,1)}%</em>.")
    return {"market": label, "odd": odd, "confidence": conf, "ev_pct": ev,
            "stake_pct": stake, "ai_explanation": ai, "shap_data": shap, "plan_required": plan}

def get_fixtures():
    return [
        {"id":1001,"home":"Arsenal","away":"Chelsea","league":"Premier League 🏴󠁧󠁢󠁥󠁮󠁧󠁿","time":"20:00","hg":2.1,"ag":1.4},
        {"id":1002,"home":"PSG","away":"Bayern","league":"Champions League 🏆","time":"21:00","hg":2.3,"ag":2.1},
        {"id":1003,"home":"Flamengo","away":"Botafogo","league":"Brasileirão 🇧🇷","time":"19:30","hg":1.8,"ag":1.1},
        {"id":1004,"home":"Bayern München","away":"Dortmund","league":"Bundesliga 🇩🇪","time":"17:30","hg":2.4,"ag":1.6},
        {"id":1005,"home":"Real Madrid","away":"Atlético Madrid","league":"La Liga 🇪🇸","time":"21:00","hg":2.0,"ag":1.3},
        {"id":1006,"home":"Inter de Milão","away":"Juventus","league":"Serie A 🇮🇹","time":"20:45","hg":1.7,"ag":1.2},
    ]

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/auth/register")
def register(data: dict):
    db = get_db()
    try:
        db.execute("INSERT INTO users (email,password_hash,name,banca) VALUES (?,?,?,?)",
                   [data["email"], hash_password(data["password"]), data.get("name",""), data.get("banca",1000)])
        db.commit()
        u = db.execute("SELECT * FROM users WHERE email=?", [data["email"]]).fetchone()
        return {"token": make_token(u["id"],u["email"]), "user": dict(u)}
    except sqlite3.IntegrityError:
        raise HTTPException(400, "Email já cadastrado")
    finally:
        db.close()

@app.post("/auth/login")
def login(data: dict):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE email=? AND password_hash=?",
                   [data["email"], hash_password(data["password"])]).fetchone()
    db.close()
    if not u:
        raise HTTPException(401, "Email ou senha incorretos")
    return {"token": make_token(u["id"],u["email"]), "user": dict(u)}

@app.get("/auth/me")
def me(user=Depends(get_current_user)):
    return user

@app.get("/signals")
def get_signals(user=Depends(get_current_user)):
    fixtures = get_fixtures()
    results = []
    plan_order = {"free":0,"premium":1,"vip":2}
    user_level = plan_order.get(user["plan"],0)
    for fix in fixtures:
        for market in ["over25","btts"]:
            sig = calc_signal(fix["hg"],fix["ag"],market,fix["home"],fix["away"])
            locked = plan_order.get(sig["plan_required"],0) > user_level
            results.append({
                "id": fix["id"]*10+(1 if market=="over25" else 2),
                "home_team": fix["home"],
                "away_team": fix["away"],
                "league": fix["league"],
                "match_time": fix["time"],
                "market": sig["market"],
                "odd": sig["odd"],
                "confidence": sig["confidence"],
                "ev_pct": sig["ev_pct"],
                "stake_pct": sig["stake_pct"],
                "ai_explanation": sig["ai_explanation"] if not locked else "Faça upgrade para ver a análise completa da IA.",
                "shap_data": sig["shap_data"] if not locked else [],
                "status": "pending",
                "plan_required": sig["plan_required"],
                "locked": locked,
            })
    results.sort(key=lambda x: x["confidence"], reverse=True)
    return results

@app.get("/signals/ranking")
def ranking(user=Depends(get_current_user)):
    fixtures = get_fixtures()
    results = []
    for fix in fixtures:
        sig = calc_signal(fix["hg"],fix["ag"],"over25",fix["home"],fix["away"])
        results.append({
            "home_team": fix["home"],
            "away_team": fix["away"],
            "league": fix["league"],
            "market": sig["market"],
            "odd": sig["odd"],
            "confidence": sig["confidence"],
            "ev_pct": sig["ev_pct"]
        })
    return sorted(results, key=lambda x: x["confidence"], reverse=True)[:10]

@app.get("/signals/stats")
def stats(user=Depends(get_current_user)):
    return {
        "roi": 23.4,
        "winrate": 67.8,
        "total_signals_today": 12,
        "greens_today": 8,
        "total_today": 9,
        "streak": 6
    }

@app.get("/dashboard/stats")
def dashboard(user=Depends(get_current_user)):
    db = get_db()
    bets = db.execute("SELECT * FROM bets WHERE user_id=?", [user["id"]]).fetchall()
    db.close()
    total = len(bets)
    wins = sum(1 for b in bets if b["result"]=="green")
    profit = sum(b["profit"] for b in bets)
    banca = user["banca"]
    return {
        "banca": banca,
        "profit": profit,
        "roi": round(profit/banca*100,1) if banca else 0,
        "winrate": round(wins/total*100,1) if total else 0,
        "total_bets": total,
        "wins": wins,
        "losses": total-wins,
        "drawdown": -8.2,
        "weekly_roi": [3.2,-1.8,5.1,2.4,4.8,-0.9,6.2,3.7]
    }

@app.post("/bets")
def add_bet(data: dict, user=Depends(get_current_user)):
    db = get_db()
    db.execute(
        "INSERT INTO bets (user_id,signal_id,amount,odd,home_team,away_team,market) VALUES (?,?,?,?,?,?,?)",
        [user["id"], data.get("signal_id"), data.get("amount"), data.get("odd"),
         data.get("home_team",""), data.get("away_team",""), data.get("market","")]
    )
    db.commit()
    db.close()
    return {"ok": True}

@app.get("/bets/history")
def bet_history(user=Depends(get_current_user)):
    db = get_db()
    bets = db.execute(
        "SELECT * FROM bets WHERE user_id=? ORDER BY created_at DESC LIMIT 30",
        [user["id"]]
    ).fetchall()
    db.close()
    return [dict(b) for b in bets]

@app.post("/payment/create")
def create_payment(data: dict, user=Depends(get_current_user)):
    return {"init_point": "https://www.mercadopago.com.br", "demo": True}
