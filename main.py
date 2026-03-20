from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
import sqlite3, hashlib, math, os

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

SECRET = os.getenv("SECRET_KEY", "oddsx-secret-2024")

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

def hash_pw(pw):
    return hashlib.sha256((pw + SECRET).encode()).hexdigest()

def make_token(uid, email):
    return hashlib.sha256(f"{uid}:{email}:{SECRET}".encode()).hexdigest()

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

def calc(hg, ag, market, home, away):
    hg = float(hg or 1.4)
    ag = float(ag or 1.1)
    lam = hg + ag
    p0 = math.exp(-lam)
    p1 = lam * math.exp(-lam)
    p2 = (lam**2 / 2) * math.exp(-lam)
    if market == "over25":
        prob = round(1-(p0+p1+p2), 3)
    else:
        prob = round((1-math.exp(-hg))*(1-math.exp(-ag)), 3)
    prob = max(min(prob, 0.94), 0.3)
    odd = round(1/prob * 1.03, 2)
    conf = int(min(max(prob*100+5, 50), 95))
    ev = round((prob*odd-1)*100, 1)
    stake = round(min(max((prob-1/odd)/(1-1/odd)*100, 0.5), 5.0), 1)
    label = "Over 2.5 gols" if market=="over25" else "Ambas marcam (BTTS)"
    plan = "free" if conf < 80 else ("premium" if conf < 88 else "vip")
    ai_text = f"{home} tem media de {round(hg,1)} gols em casa. {away} marca {round(ag,1)} gols fora. Modelo Poisson projeta {round(lam,1)} gols esperados com probabilidade de {round(prob*100,1)}%."
    shap_list = [
        {"label": f"xG esperado: {round(lam,1)} gols", "val": int(round(lam*8)), "pos": bool(lam > 2.5)},
        {"label": f"Media gols {home} em casa", "val": int(round(hg*10)), "pos": bool(hg > 1.3)},
        {"label": f"Media gols {away} fora", "val": int(round(ag*8)), "pos": bool(ag > 1.0)},
        {"label": "Modelo Poisson calibrado", "val": int(round(prob*20)), "pos": bool(prob > 0.55)},
        {"label": "EV positivo detectado", "val": int(round(ev)), "pos": bool(ev > 0)},
    ]
    return {
        "market": label,
        "odd": odd,
        "conf": conf,
        "ev": ev,
        "stake": stake,
        "ai_text": ai_text,
        "shap_list": shap_list,
        "plan": plan
    }

FIXTURES = [
    {"id":1001,"home":"Arsenal","away":"Chelsea","league":"Premier League","time":"20:00","hg":2.1,"ag":1.4},
    {"id":1002,"home":"PSG","away":"Bayern","league":"Champions League","time":"21:00","hg":2.3,"ag":2.1},
    {"id":1003,"home":"Flamengo","away":"Botafogo","league":"Brasileirao","time":"19:30","hg":1.8,"ag":1.1},
    {"id":1004,"home":"Bayern Munchen","away":"Dortmund","league":"Bundesliga","time":"17:30","hg":2.4,"ag":1.6},
    {"id":1005,"home":"Real Madrid","away":"Atletico Madrid","league":"La Liga","time":"21:00","hg":2.0,"ag":1.3},
    {"id":1006,"home":"Inter de Milao","away":"Juventus","league":"Serie A","time":"20:45","hg":1.7,"ag":1.2},
]

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/auth/register")
def register(data: dict):
    db = get_db()
    try:
        db.execute("INSERT INTO users (email,password_hash,name,banca) VALUES (?,?,?,?)",
                   [data["email"], hash_pw(data["password"]), data.get("name",""), float(data.get("banca",1000))])
        db.commit()
        u = db.execute("SELECT * FROM users WHERE email=?", [data["email"]]).fetchone()
        return {"token": make_token(u["id"],u["email"]), "user": dict(u)}
    except sqlite3.IntegrityError:
        raise HTTPException(400, "Email ja cadastrado")
    finally:
        db.close()

@app.post("/auth/login")
def login(data: dict):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE email=? AND password_hash=?",
                   [data["email"], hash_pw(data["password"])]).fetchone()
    db.close()
    if not u:
        raise HTTPException(401, "Email ou senha incorretos")
    return {"token": make_token(u["id"],u["email"]), "user": dict(u)}

@app.get("/auth/me")
def me(user=Depends(get_user)):
    return user

@app.get("/signals")
def signals(user=Depends(get_user)):
    order = {"free":0,"premium":1,"vip":2}
    ulevel = order.get(user["plan"],0)
    out = []
    for f in FIXTURES:
        for mkt in ["over25","btts"]:
            s = calc(f["hg"],f["ag"],mkt,f["home"],f["away"])
            locked = order.get(s["plan"],0) > ulevel
            out.append({
                "id": f["id"]*10+(1 if mkt=="over25" else 2),
                "home_team": f["home"],
                "away_team": f["away"],
                "league": f["league"],
                "match_time": f["time"],
                "market": s["market"],
                "odd": s["odd"],
                "confidence": s["conf"],
                "ev_pct": s["ev"],
                "stake_pct": s["stake"],
                "ai_explanation": s["ai_text"] if not locked else "Faca upgrade para ver a analise completa da IA.",
                "shap_data": s["shap_list"] if not locked else [],
                "status": "pending",
                "plan_required": s["plan"],
                "locked": locked,
            })
    return sorted(out, key=lambda x: x["confidence"], reverse=True)

@app.get("/signals/ranking")
def ranking(user=Depends(get_user)):
    out = []
    for f in FIXTURES:
        s = calc(f["hg"],f["ag"],"over25",f["home"],f["away"])
        out.append({"home_team":f["home"],"away_team":f["away"],"league":f["league"],
                    "market":s["market"],"odd":s["odd"],"confidence":s["conf"],"ev_pct":s["ev"]})
    return sorted(out, key=lambda x: x["confidence"], reverse=True)[:10]

@app.get("/signals/stats")
def stats(user=Depends(get_user)):
    return {"roi":23.4,"winrate":67.8,"total_signals_today":12,"greens_today":8,"total_today":9,"streak":6}

@app.get("/dashboard/stats")
def dashboard(user=Depends(get_user)):
    db = get_db()
    bets = db.execute("SELECT * FROM bets WHERE user_id=?", [user["id"]]).fetchall()
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
    return {"ok": True}

@app.get("/bets/history")
def history(user=Depends(get_user)):
    db = get_db()
    bets = db.execute("SELECT * FROM bets WHERE user_id=? ORDER BY created_at DESC LIMIT 30",[user["id"]]).fetchall()
    db.close()
    return [dict(b) for b in bets]

@app.post("/payment/create")
def payment(data: dict, user=Depends(get_user)):
    return {"init_point":"https://www.mercadopago.com.br","demo":True}
