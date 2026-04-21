"""Rotas do webhook da Meta (verificação + recebimento de eventos).

Atenção ao fluxo em POST /webhook:

1. Lemos o body **como bytes** — precisamos dele cru para validar HMAC.
2. Se ``WEBHOOK_VERIFY_SIGNATURE`` estiver true, validamos a assinatura.
3. Retornamos **200 imediatamente** para a Meta e enfileiramos o
   processamento em :class:`BackgroundTasks`. O processamento pesado
   usa sua própria ``AsyncSession`` (a do request encerra junto com o
   response).
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from ..config import Settings, get_settings
from ..db.session import AsyncSessionLocal
from ..schemas.webhook import WebhookPayload
from ..security.webhook_signature import verify_signature
from ..services.instagram_client import InstagramClient, get_instagram_client
from ..services.webhook_service import WebhookService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])


# --------------------------------------------------------------------------- #
# GET /webhook — handshake inicial
# --------------------------------------------------------------------------- #
@router.get("", include_in_schema=True)
def webhook_verify(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
    settings: Settings = Depends(get_settings),
) -> PlainTextResponse:
    """Responde ao handshake de verificação do webhook.

    A Meta envia um GET com ``hub.mode=subscribe`` e um ``hub.verify_token``
    que deve bater com o nosso ``VERIFY_TOKEN``. Retornamos o
    ``hub.challenge`` como texto puro.
    """
    if hub_mode == "subscribe" and hub_verify_token == settings.verify_token:
        logger.info("Handshake do webhook verificado com sucesso.")
        return PlainTextResponse(content=hub_challenge or "")

    logger.warning(
        "Handshake rejeitado | mode=%s token_match=%s",
        hub_mode,
        hub_verify_token == settings.verify_token,
    )
    raise HTTPException(status_code=403, detail="Verification failed")


# --------------------------------------------------------------------------- #
# POST /webhook — recebe eventos
# --------------------------------------------------------------------------- #
@router.post("")
async def webhook_receive(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    settings: Settings = Depends(get_settings),
    ig: InstagramClient = Depends(get_instagram_client),
) -> dict:
    """Recebe eventos do Instagram e delega o processamento ao background."""
    # 1) Body cru para validação de assinatura.
    raw_body = await request.body()

    if settings.webhook_verify_signature:
        if not verify_signature(
            app_secret=settings.instagram_app_secret,
            body=raw_body,
            signature_header=x_hub_signature_256,
        ):
            # 401 também é aceito pela Meta; usamos 403 por semântica.
            raise HTTPException(status_code=403, detail="Invalid signature")

    # 2) Parse JSON. Se falhar, respondemos 200 para a Meta não reentregar
    #    infinitamente (o que nos DDoSaria) — apenas logamos.
    try:
        parsed = json.loads(raw_body or b"{}")
    except Exception:
        logger.exception("Body inválido em POST /webhook")
        return {"status": "ignored", "reason": "invalid_json"}

    try:
        payload = WebhookPayload.model_validate(parsed)
    except Exception:
        logger.exception("Payload não bate com o schema esperado: %s", parsed)
        return {"status": "ignored", "reason": "schema_mismatch"}

    # 3) Enfileira o processamento. Importante: a sessão do request
    #    encerra quando retornamos, então abrimos uma nova dentro do
    #    task de background.
    background_tasks.add_task(_process_in_background, payload, ig)

    return {"status": "received"}


async def _process_in_background(payload: WebhookPayload, ig: InstagramClient) -> None:
    """Roda o :class:`WebhookService` dentro de uma sessão de banco nova."""
    async with AsyncSessionLocal() as session:
        service = WebhookService(session=session, instagram_client=ig)
        try:
            await service.process_payload(payload)
            await session.commit()
        except Exception:
            logger.exception("Falha geral ao processar payload; fazendo rollback.")
            await session.rollback()
