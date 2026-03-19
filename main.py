"""
OddsX — Backend Principal (FastAPI)
Rode com: uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio, logging

from database import init_db
from routers import signals, auth, users, webhook
from scheduler import start_scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("oddsx")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    asyncio.create_task(start_scheduler())
    logger.info("OddsX backend iniciado.")
    yield

app = FastAPI(title="OddsX API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router,    prefix="/api/auth",    tags=["Auth"])
app.include_router(users.router,   prefix="/api/users",   tags=["Users"])
app.include_router(signals.router, prefix="/api/signals", tags=["Signals"])
app.include_router(webhook.router, prefix="/api/webhook", tags=["Webhooks"])

@app.get("/")
async def root():
    return {"status": "OddsX online", "version": "1.0.0"}

@app.get("/health")
async def health():
    return {"status": "ok"}
