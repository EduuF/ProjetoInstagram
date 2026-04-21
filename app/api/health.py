"""Rotas de health-check e metadados públicos.

Separadas do core de negócio por dois motivos:

1. Liveness barato (``/`` e ``/live``): qualquer balanceador/ALB precisa
   saber se o processo está respondendo. NÃO depende de DB — se o DB cair
   a app ainda está viva, só degradada.

2. Readiness com ping do banco (``/health``): o Route 53 / ALB target
   group usa isso pra tirar uma instância do rotation quando o banco
   está indisponível. Se isso não existisse, o ALB ficaria mandando
   tráfego pra uma réplica que vai falhar em toda request.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/", tags=["infra"])
def root() -> dict[str, str]:
    """Liveness: a app está respondendo HTTP?"""
    return {"status": "ok"}


@router.get("/live", tags=["infra"])
def liveness() -> dict[str, str]:
    """Alias explícito do liveness — semântica clara pro ALB."""
    return {"status": "ok"}


@router.get("/health", tags=["infra"])
async def health(
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Readiness: está vivo E consegue falar com o banco?

    Retorna 200 quando tudo OK; 503 quando o banco está inalcançável.
    """
    payload: dict[str, Any] = {"status": "ok", "checks": {}}

    try:
        await session.execute(text("SELECT 1"))
        payload["checks"]["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Healthcheck: falha ao pingar DB")
        payload["status"] = "degraded"
        payload["checks"]["database"] = f"error: {type(exc).__name__}"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return payload

    return payload


@router.get("/privacy_policy", tags=["infra"])
def privacy_policy() -> dict:
    """URL exigida pela Meta na configuração do app."""
    return {
        "title": "Política de Privacidade",
        "content": (
            "Este aplicativo coleta dados necessários para integração com a "
            "API do Instagram. Nenhum dado pessoal é compartilhado com terceiros."
        ),
    }
