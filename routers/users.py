"""
Usuários — perfil, banca, Telegram ID.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from database import get_db
from routers.auth import current_user

router = APIRouter()


class ProfileUpdate(BaseModel):
    name:        str   | None = None
    telegram_id: str   | None = None
    banca:       float | None = None


@router.patch("/profile")
async def update_profile(body: ProfileUpdate, user=Depends(current_user)):
    db = await get_db()
    uid = int(user["sub"])
    if body.name:
        await db.execute("UPDATE users SET name=? WHERE id=?", (body.name, uid))
    if body.telegram_id is not None:
        await db.execute("UPDATE users SET telegram_id=? WHERE id=?", (body.telegram_id, uid))
    if body.banca is not None:
        if body.banca < 0:
            raise HTTPException(400, "Banca inválida.")
        await db.execute("UPDATE users SET banca=? WHERE id=?", (body.banca, uid))
    await db.commit()
    return {"ok": True}


@router.get("/dashboard")
async def dashboard(user=Depends(current_user)):
    db  = await get_db()
    uid = int(user["sub"])
    u   = await (await db.execute(
        "SELECT name, plan, banca FROM users WHERE id=?", (uid,)
    )).fetchone()
    bets = await (await db.execute(
        "SELECT result, profit_brl FROM bets WHERE user_id=?", (uid,)
    )).fetchall()
    greens = sum(1 for b in bets if b["result"] == "green")
    reds   = sum(1 for b in bets if b["result"] == "red")
    profit = sum((b["profit_brl"] or 0) for b in bets)
    return {
        "name":    u["name"],
        "plan":    u["plan"],
        "banca":   u["banca"],
        "greens":  greens,
        "reds":    reds,
        "profit":  round(profit, 2),
        "winrate": round(greens / len(bets) * 100, 1) if bets else 0,
    }
