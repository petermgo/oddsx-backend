"""
Webhooks — MercadoPago (pagamentos) e atualização de plano.
"""

import os, httpx, logging
from fastapi import APIRouter, Request, HTTPException
from database import get_db

router = APIRouter()
logger = logging.getLogger("oddsx.webhook")

MP_TOKEN = os.getenv("MERCADOPAGO_ACCESS_TOKEN", "")

PLAN_PRICES = {
    "premium": 97.0,
    "vip":     297.0,
    "agency":  997.0,
}


@router.post("/mercadopago")
async def mercadopago_webhook(request: Request):
    """Recebe notificação do MercadoPago e atualiza plano do usuário."""
    body = await request.json()
    logger.info(f"MP webhook recebido: {body}")

    if body.get("type") != "payment":
        return {"ok": True}

    payment_id = body.get("data", {}).get("id")
    if not payment_id or not MP_TOKEN:
        return {"ok": True}

    # Busca detalhes do pagamento na API do MP
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.mercadopago.com/v1/payments/{payment_id}",
            headers={"Authorization": f"Bearer {MP_TOKEN}"},
        )
    if resp.status_code != 200:
        logger.error(f"Erro MP: {resp.text}")
        return {"ok": False}

    data   = resp.json()
    status = data.get("status")
    if status != "approved":
        return {"ok": True}

    # metadata: {"user_id": 1, "plan": "vip"}
    meta    = data.get("metadata", {})
    user_id = meta.get("user_id")
    plan    = meta.get("plan")

    if not user_id or plan not in PLAN_PRICES:
        logger.warning(f"Metadata inválida: {meta}")
        return {"ok": False}

    db = await get_db()
    await db.execute("UPDATE users SET plan=? WHERE id=?", (plan, user_id))
    await db.execute(
        "INSERT INTO payments (user_id, plan, amount_brl, status, provider_ref) VALUES (?,?,?,?,?)",
        (user_id, plan, PLAN_PRICES[plan], "approved", str(payment_id)),
    )
    await db.commit()
    logger.info(f"Plano {plan} ativado para user {user_id}")
    return {"ok": True}


@router.post("/create-payment")
async def create_payment(request: Request):
    """Cria preferência de pagamento no MercadoPago e retorna o link."""
    from routers.auth import current_user
    from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

    body    = await request.json()
    plan    = body.get("plan")
    user_id = body.get("user_id")

    if plan not in PLAN_PRICES:
        raise HTTPException(400, "Plano inválido.")
    if not MP_TOKEN:
        raise HTTPException(500, "MERCADOPAGO_ACCESS_TOKEN não configurado.")

    preference = {
        "items": [{
            "title": f"OddsX {plan.capitalize()}",
            "quantity": 1,
            "currency_id": "BRL",
            "unit_price": PLAN_PRICES[plan],
        }],
        "metadata": {"user_id": user_id, "plan": plan},
        "back_urls": {
            "success": os.getenv("FRONTEND_URL", "http://localhost:3000") + "/sucesso",
            "failure": os.getenv("FRONTEND_URL", "http://localhost:3000") + "/falha",
        },
        "auto_return": "approved",
        "notification_url": os.getenv("BACKEND_URL", "http://localhost:8000") + "/api/webhook/mercadopago",
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.mercadopago.com/checkout/preferences",
            headers={"Authorization": f"Bearer {MP_TOKEN}", "Content-Type": "application/json"},
            json=preference,
        )
    if resp.status_code not in (200, 201):
        raise HTTPException(500, f"Erro ao criar pagamento: {resp.text}")

    data = resp.json()
    return {
        "init_point": data["init_point"],
        "sandbox_url": data.get("sandbox_init_point"),
    }
