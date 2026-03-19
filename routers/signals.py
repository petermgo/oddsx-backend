"""
Sinais — listagem, detalhe e registro de aposta.
"""

import json
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from database import get_db
from routers.auth import current_user

router = APIRouter()

PLAN_ORDER = {"free": 0, "premium": 1, "vip": 2, "agency": 3}


def _mask(signal: dict, user_plan: str) -> dict:
    """Oculta campos sensíveis de sinais que exigem plano maior."""
    req = signal.get("plan_req", "free")
    if PLAN_ORDER.get(user_plan, 0) < PLAN_ORDER.get(req, 0):
        signal["locked"] = True
        signal["ai_reason"] = "🔒 Upgrade necessário para ver esta análise."
        signal["shap_json"] = "[]"
        signal["odd"] = None
        signal["ev_pct"] = None
    else:
        signal["locked"] = False
    signal["shap"] = json.loads(signal.get("shap_json", "[]"))
    return signal


@router.get("/")
async def list_signals(
    sport:  str | None = Query(None),
    status: str | None = Query(None),
    risk:   str | None = Query(None),
    limit:  int        = Query(50, le=100),
    user=Depends(current_user),
):
    db = await get_db()
    query = "SELECT * FROM signals WHERE 1=1"
    params: list = []
    if sport:  query += " AND sport=?";  params.append(sport)
    if status: query += " AND status=?"; params.append(status)
    if risk:   query += " AND risk=?";   params.append(risk)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    rows = await (await db.execute(query, params)).fetchall()
    return [_mask(dict(r), user["plan"]) for r in rows]


@router.get("/{signal_id}")
async def get_signal(signal_id: int, user=Depends(current_user)):
    db = await get_db()
    row = await (await db.execute("SELECT * FROM signals WHERE id=?", (signal_id,))).fetchone()
    if not row:
        raise HTTPException(404, "Sinal não encontrado.")
    return _mask(dict(row), user["plan"])


@router.get("/stats/summary")
async def stats_summary(user=Depends(current_user)):
    db = await get_db()
    total  = (await (await db.execute("SELECT COUNT(*) FROM signals")).fetchone())[0]
    greens = (await (await db.execute("SELECT COUNT(*) FROM signals WHERE status='green'")).fetchone())[0]
    reds   = (await (await db.execute("SELECT COUNT(*) FROM signals WHERE status='red'")).fetchone())[0]
    today  = (await (await db.execute(
        "SELECT COUNT(*) FROM signals WHERE date(created_at)=date('now')"
    )).fetchone())[0]
    winrate = round((greens / (greens + reds) * 100), 1) if (greens + reds) > 0 else 0
    return {
        "total": total, "greens": greens, "reds": reds,
        "today": today, "winrate": winrate,
    }


# ── Registrar aposta ──────────────────────────────────

class BetIn(BaseModel):
    signal_id: int | None = None
    market: str
    odd: float
    stake_brl: float


@router.post("/bet")
async def register_bet(body: BetIn, user=Depends(current_user)):
    db = await get_db()
    user_row = await (await db.execute(
        "SELECT banca FROM users WHERE id=?", (int(user["sub"]),)
    )).fetchone()
    if not user_row:
        raise HTTPException(404, "Usuário não encontrado.")
    if body.stake_brl > user_row["banca"]:
        raise HTTPException(400, "Stake maior que a banca disponível.")

    await db.execute(
        "INSERT INTO bets (user_id, signal_id, market, odd, stake_brl) VALUES (?,?,?,?,?)",
        (int(user["sub"]), body.signal_id, body.market, body.odd, body.stake_brl),
    )
    await db.execute(
        "UPDATE users SET banca = banca - ? WHERE id=?",
        (body.stake_brl, int(user["sub"])),
    )
    await db.commit()
    return {"ok": True, "message": "Aposta registrada."}


@router.get("/my/bets")
async def my_bets(user=Depends(current_user)):
    db = await get_db()
    rows = await (await db.execute(
        """SELECT b.*, s.home_team, s.away_team, s.league
           FROM bets b LEFT JOIN signals s ON b.signal_id=s.id
           WHERE b.user_id=? ORDER BY b.created_at DESC LIMIT 50""",
        (int(user["sub"]),),
    )).fetchall()
    return [dict(r) for r in rows]


@router.get("/my/stats")
async def my_stats(user=Depends(current_user)):
    db = await get_db()
    uid = int(user["sub"])
    total  = (await (await db.execute("SELECT COUNT(*) FROM bets WHERE user_id=?", (uid,))).fetchone())[0]
    greens = (await (await db.execute("SELECT COUNT(*) FROM bets WHERE user_id=? AND result='green'", (uid,))).fetchone())[0]
    profit = (await (await db.execute("SELECT COALESCE(SUM(profit_brl),0) FROM bets WHERE user_id=?", (uid,))).fetchone())[0]
    banca  = (await (await db.execute("SELECT banca FROM users WHERE id=?", (uid,))).fetchone())[0]
    winrate = round(greens / total * 100, 1) if total > 0 else 0
    return {
        "total_bets": total, "greens": greens,
        "profit_brl": round(profit, 2), "banca": banca,
        "winrate": winrate,
    }
