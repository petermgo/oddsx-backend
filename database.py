"""
Banco de dados — SQLite via aiosqlite (zero config, funciona imediatamente).
Para produção, troque DATABASE_URL por postgresql+asyncpg://...
"""

import os, aiosqlite, logging
from datetime import datetime

DATABASE_URL = os.getenv("DATABASE_URL", "oddsx.db")
logger = logging.getLogger("oddsx.db")

_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DATABASE_URL)
        _db.row_factory = aiosqlite.Row
    return _db


async def init_db():
    db = await get_db()
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            email       TEXT    UNIQUE NOT NULL,
            password    TEXT    NOT NULL,
            name        TEXT    NOT NULL,
            plan        TEXT    NOT NULL DEFAULT 'free',
            telegram_id TEXT,
            banca       REAL    NOT NULL DEFAULT 1000.0,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            league      TEXT    NOT NULL,
            home_team   TEXT    NOT NULL,
            away_team   TEXT    NOT NULL,
            match_time  TEXT    NOT NULL,
            sport       TEXT    NOT NULL DEFAULT 'football',
            market      TEXT    NOT NULL,
            odd         REAL    NOT NULL,
            confidence  INTEGER NOT NULL,
            ev_pct      REAL    NOT NULL,
            stake_pct   REAL    NOT NULL,
            risk        TEXT    NOT NULL DEFAULT 'medium',
            ai_reason   TEXT    NOT NULL,
            shap_json   TEXT    NOT NULL DEFAULT '[]',
            status      TEXT    NOT NULL DEFAULT 'pending',
            plan_req    TEXT    NOT NULL DEFAULT 'free',
            created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            resolved_at TEXT
        );

        CREATE TABLE IF NOT EXISTS bets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL REFERENCES users(id),
            signal_id   INTEGER REFERENCES signals(id),
            market      TEXT    NOT NULL,
            odd         REAL    NOT NULL,
            stake_brl   REAL    NOT NULL,
            result      TEXT,
            profit_brl  REAL,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS payments (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL REFERENCES users(id),
            plan            TEXT    NOT NULL,
            amount_brl      REAL    NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'pending',
            provider_ref    TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        );
    """)
    await db.commit()
    logger.info("Banco de dados inicializado.")
