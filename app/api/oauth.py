"""Rotas do fluxo OAuth do Instagram + callbacks de compliance da Meta.

Visão geral:

::

    Frontend (logado, com JWT)
        │  GET /auth/instagram/start
        │      Authorization: Bearer <jwt>
        ▼
    Backend
        - Gera state (random, uso único, TTL) e persiste em oauth_states.
        - Monta URL de authorize do Instagram.
        - Retorna {"authorize_url": "..."} (200 JSON)
        ▼
    Frontend
        window.location = authorize_url
        ▼
    Instagram (tela oficial de autorização)
        ▼
    User clica "Allow"
        ▼
    Instagram -> GET {OAUTH_REDIRECT_URI}?code=...&state=...
        ▼
    Backend /auth/instagram/callback
        - Consome state (valida e marca consumido).
        - Troca code -> short-lived -> long-lived (60d).
        - Chama /me -> descobre ig_user_id, username.
        - Upsert InstagramAccount (token cifrado).
        - POST /{ig_user_id}/subscribed_apps -> ativa webhooks.
        - Redirect 302 para frontend (ou página default).

Callbacks da Meta (cadastrados no painel):
    - /auth/instagram/deauthorize  (signed_request)
    - /auth/instagram/data-deletion (signed_request, retorna JSON com url)
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..db.models import Client
from ..repositories.instagram_account import InstagramAccountRepository
from ..repositories.oauth_state import OAuthStateRepository
from ..security.signed_request import (
    InvalidSignedRequestError,
    parse_signed_request,
)
from ..services.oauth_instagram import OAuthInstagramService
from .deps import current_client, db_session, oauth_instagram_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/instagram", tags=["oauth"])


# --------------------------------------------------------------------------- #
# 1. START — cliente autenticado pede a URL do Instagram para autorizar
# --------------------------------------------------------------------------- #
class StartRequest(BaseModel):
    # Para onde redirecionar o usuário depois do callback (ex.: dashboard).
    # Se None, o backend escolhe uma página default.
    redirect_after: str | None = None


class StartResponse(BaseModel):
    authorize_url: str


@router.post(
    "/start",
    response_model=StartResponse,
    summary="Gera state CSRF e devolve a URL de authorize do Instagram.",
)
async def start(
    body: StartRequest,
    client: Client = Depends(current_client),
    session: AsyncSession = Depends(db_session),
    oauth: OAuthInstagramService = Depends(oauth_instagram_service),
) -> StartResponse:
    # Persistimos o state com client_id atrelado ao JWT atual. Assim, quando
    # o callback chegar, sabemos exatamente QUEM autorizou (o header
    # Authorization NÃO viaja no redirect do Instagram).
    state_row = await OAuthStateRepository(session).create(
        client_id=client.id,
        redirect_after=body.redirect_after,
    )
    return StartResponse(authorize_url=oauth.build_authorize_url(state_row.state))


# --------------------------------------------------------------------------- #
# 2. CALLBACK — Instagram redireciona aqui com code+state
# --------------------------------------------------------------------------- #
@router.get(
    "/callback",
    summary="Callback OAuth do Instagram. Troca code por token e assina webhooks.",
)
async def callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_reason: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
    session: AsyncSession = Depends(db_session),
    oauth: OAuthInstagramService = Depends(oauth_instagram_service),
    settings: Settings = Depends(get_settings),
) -> Any:
    # Caso 1: usuário clicou em "Cancel" na tela do Instagram.
    if error:
        logger.warning(
            "OAuth cancelado pelo usuário | error=%s reason=%s desc=%s",
            error, error_reason, error_description,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": error, "reason": error_reason, "description": error_description},
        )

    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing_code_or_state",
        )

    # Consome o state (one-shot + TTL). Se inválido, alguma das três coisas
    # aconteceu: nunca existiu, já foi usado (replay), ou expirou (>10min).
    state_row = await OAuthStateRepository(session).consume(state)
    if state_row is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_or_expired_state",
        )

    # Fluxo OAuth: code -> short-lived -> long-lived.
    short = await oauth.exchange_code(code=code)
    long_lived = await oauth.exchange_long_lived(short_lived_token=short["access_token"])

    # Descobre qual conta IG a gente acabou de conectar.
    me = await oauth.get_me(access_token=long_lived["access_token"])

    # Upsert: se o cliente reconectar, atualizamos o token em vez de duplicar.
    repo = InstagramAccountRepository(session)
    account = await repo.upsert_from_oauth(
        client_id=state_row.client_id,
        ig_business_account_id=me["ig_user_id"],
        ig_user_id=me["app_scoped_id"],
        username=me["username"],
        access_token=long_lived["access_token"],
        token_expires_at=long_lived["expires_at"],
    )

    # Ativa o webhook para ``comments`` nesta conta. Sem isso, o produto
    # fica silenciosamente inativo.
    try:
        await oauth.subscribe_app(
            ig_user_id=me["ig_user_id"],
            access_token=long_lived["access_token"],
        )
    except Exception:  # noqa: BLE001
        # Se falhar, a conta já está salva (token + relação); sinalizamos
        # pro cliente via query param para o frontend mostrar um aviso.
        logger.exception("subscribe_app falhou | account_id=%s", account.id)
        target = (state_row.redirect_after or "/") + "?instagram=partial&warning=webhook_subscription_failed"
        return RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)

    logger.info(
        "OAuth concluído | client_id=%s account_id=%s ig_user_id=%s username=%s",
        state_row.client_id, account.id, me["ig_user_id"], me["username"],
    )

    target = state_row.redirect_after or "/"
    if "?" in target:
        target += "&instagram=connected"
    else:
        target += "?instagram=connected"
    return RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)


# --------------------------------------------------------------------------- #
# 3. DEAUTHORIZE — Meta avisa que o usuário revogou acesso ao nosso app
# --------------------------------------------------------------------------- #
@router.post(
    "/deauthorize",
    summary="Callback oficial da Meta: usuário revogou acesso (Deauthorize Callback URL).",
)
async def deauthorize(
    signed_request: str = Form(...),
    session: AsyncSession = Depends(db_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    try:
        payload = parse_signed_request(signed_request, settings.instagram_app_secret)
    except InvalidSignedRequestError as exc:
        logger.warning("signed_request inválido em /deauthorize: %s", exc)
        raise HTTPException(status_code=400, detail="invalid_signed_request")

    user_id = str(payload.get("user_id") or "")
    if not user_id:
        raise HTTPException(status_code=400, detail="missing_user_id")

    repo = InstagramAccountRepository(session)
    deactivated = await repo.deactivate_by_ig_user_id(user_id)
    logger.info("Deauthorize | user_id=%s contas_desativadas=%d", user_id, deactivated)
    return {"ok": True, "deactivated": deactivated}


# --------------------------------------------------------------------------- #
# 4. DATA DELETION — Meta exige URL pra usuário pedir deleção de dados
# --------------------------------------------------------------------------- #
@router.post(
    "/data-deletion",
    summary="Callback oficial da Meta: pedido de deleção de dados do usuário.",
)
async def data_deletion(
    signed_request: str = Form(...),
    session: AsyncSession = Depends(db_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    try:
        payload = parse_signed_request(signed_request, settings.instagram_app_secret)
    except InvalidSignedRequestError as exc:
        logger.warning("signed_request inválido em /data-deletion: %s", exc)
        raise HTTPException(status_code=400, detail="invalid_signed_request")

    user_id = str(payload.get("user_id") or "")
    if not user_id:
        raise HTTPException(status_code=400, detail="missing_user_id")

    # Nesta primeira versão, apenas desativamos a conta (evitamos DELETE
    # imediato de comment_events/auto_replies_sent, que podem ser úteis
    # para auditoria/faturamento e devem ser purgados por um job
    # posterior, dentro do prazo de 30 dias exigido pela Meta).
    repo = InstagramAccountRepository(session)
    await repo.deactivate_by_ig_user_id(user_id)

    # A Meta exige que a gente devolva um ``confirmation_code`` e uma
    # ``url`` onde o usuário possa checar o progresso da deleção.
    confirmation_code = secrets.token_urlsafe(16)
    status_url = (
        f"{settings.oauth_redirect_uri.rsplit('/auth/instagram/callback', 1)[0]}"
        f"/auth/instagram/data-deletion/status?code={confirmation_code}"
    )

    logger.info(
        "Data deletion | user_id=%s confirmation_code=%s",
        user_id, confirmation_code,
    )
    # TODO: persistir o confirmation_code + status para responder a consulta
    # em /data-deletion/status. Por ora, é stub.
    return {"url": status_url, "confirmation_code": confirmation_code}


@router.get(
    "/data-deletion/status",
    summary="Consulta o status de um pedido de deleção (stub).",
)
async def data_deletion_status(code: str = Query(...)) -> dict:
    return {"code": code, "status": "pending"}
