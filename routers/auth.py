"""
Autenticação — JWT com bcrypt.
"""

import os, bcrypt, jwt
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from database import get_db

router = APIRouter()
security = HTTPBearer()

SECRET  = os.getenv("JWT_SECRET", "oddsx-secret-change-in-prod-2024")
ALGO    = "HS256"
EXP_H   = 72  # horas


def make_token(user_id: int, email: str, plan: str) -> str:
    payload = {
        "sub": str(user_id),
        "email": email,
        "plan": plan,
        "exp": datetime.utcnow() + timedelta(hours=EXP_H),
    }
    return jwt.encode(payload, SECRET, algorithm=ALGO)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET, algorithms=[ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expirado")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Token inválido")


async def current_user(creds: HTTPAuthorizationCredentials = Depends(security)):
    return decode_token(creds.credentials)


async def require_plan(min_plan: str):
    """Retorna um Depends que verifica se o usuário tem o plano mínimo."""
    order = ["free", "premium", "vip", "agency"]
    async def checker(user=Depends(current_user)):
        if order.index(user["plan"]) < order.index(min_plan):
            raise HTTPException(403, f"Plano {min_plan} necessário.")
        return user
    return checker


# ── Schemas ──────────────────────────────────────────

class RegisterIn(BaseModel):
    email: EmailStr
    password: str
    name: str

class LoginIn(BaseModel):
    email: EmailStr
    password: str


# ── Endpoints ────────────────────────────────────────

@router.post("/register")
async def register(body: RegisterIn):
    db = await get_db()
    row = await (await db.execute("SELECT id FROM users WHERE email=?", (body.email,))).fetchone()
    if row:
        raise HTTPException(400, "E-mail já cadastrado.")
    hashed = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    cur = await db.execute(
        "INSERT INTO users (email, password, name) VALUES (?,?,?)",
        (body.email, hashed, body.name),
    )
    await db.commit()
    token = make_token(cur.lastrowid, body.email, "free")
    return {"token": token, "plan": "free", "name": body.name}


@router.post("/login")
async def login(body: LoginIn):
    db = await get_db()
    row = await (await db.execute(
        "SELECT id, password, name, plan FROM users WHERE email=?", (body.email,)
    )).fetchone()
    if not row or not bcrypt.checkpw(body.password.encode(), row["password"].encode()):
        raise HTTPException(401, "E-mail ou senha incorretos.")
    token = make_token(row["id"], body.email, row["plan"])
    return {"token": token, "plan": row["plan"], "name": row["name"]}


@router.get("/me")
async def me(user=Depends(current_user)):
    db = await get_db()
    row = await (await db.execute(
        "SELECT id, email, name, plan, banca, telegram_id, created_at FROM users WHERE id=?",
        (int(user["sub"]),)
    )).fetchone()
    if not row:
        raise HTTPException(404, "Usuário não encontrado.")
    return dict(row)
