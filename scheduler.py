"""
Scheduler — executa análise de IA a cada 30 minutos.
"""

import asyncio, logging
from datetime import datetime
from database import get_db
from ai_engine import run_analysis, seed_demo_signals

logger = logging.getLogger("oddsx.scheduler")


async def save_signals(signals: list[dict]):
    if not signals:
        return
    db = await get_db()
    for s in signals:
        # Evita duplicatas: mesma partida + mercado nas últimas 2h
        exists = await (await db.execute(
            """SELECT id FROM signals
               WHERE home_team=? AND away_team=? AND market=?
               AND datetime(created_at) > datetime('now', '-2 hours')""",
            (s["home_team"], s["away_team"], s["market"]),
        )).fetchone()
        if exists:
            continue
        await db.execute(
            """INSERT INTO signals
               (league,home_team,away_team,match_time,sport,market,odd,confidence,
                ev_pct,stake_pct,risk,ai_reason,shap_json,plan_req)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(s[k] for k in [
                "league","home_team","away_team","match_time","sport","market","odd",
                "confidence","ev_pct","stake_pct","risk","ai_reason","shap_json","plan_req"
            ]),
        )
    await db.commit()
    logger.info(f"{len(signals)} novos sinais salvos.")


async def start_scheduler():
    """Loop principal: popula dados demo e agenda análises."""
    db = await get_db()
    await seed_demo_signals(db)

    while True:
        try:
            logger.info(f"[{datetime.now():%H:%M}] Iniciando análise de IA...")
            signals = await run_analysis()
            await save_signals(signals)
            if signals:
                logger.info(f"Análise concluída: {len(signals)} sinais gerados.")
            else:
                logger.info("Análise concluída: nenhum value bet encontrado (ou API keys não configuradas).")
        except Exception as e:
            logger.error(f"Erro no scheduler: {e}")
        await asyncio.sleep(1800)  # 30 minutos
