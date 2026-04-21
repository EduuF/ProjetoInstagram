"""Cliente do fluxo OAuth 2.0 do Instagram Business Login.

Escopo deste módulo:
    - Montar a URL de ``authorize`` (cliente será redirecionado para lá).
    - Trocar ``code`` -> short-lived token.
    - Trocar short-lived -> long-lived (60 dias).
    - Fazer refresh do long-lived (prorroga por mais 60 dias).
    - Chamar ``/me`` para descobrir ``ig_user_id`` (tenant ID que virá no
      ``entry[].id`` dos webhooks) e ``username``.
    - Assinar o app para receber webhooks de ``comments`` na conta.

Endpoints e contratos baseados em:
    https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login

Diferenças para a Graph API "normal":
    - ``POST /oauth/access_token`` fica em ``api.instagram.com`` (sem versão).
    - ``GET /access_token`` e ``GET /refresh_access_token`` ficam em
      ``graph.instagram.com`` **sem prefixo de versão** — diferente dos
      endpoints de produto (ex.: ``v25.0/me``).

Por que este serviço não compartilha o singleton ``InstagramClient``?
    - O ``InstagramClient`` é específico para endpoints de produto
      (``me/messages`` etc.) e autentica via Bearer header.
    - O fluxo OAuth tem contratos distintos (form-url-encoded no POST,
      query string com ``access_token`` nos GETs) — misturar na mesma
      classe deixaria tudo mais confuso.
    - **Reaproveitamos** o mesmo ``httpx.AsyncClient`` (pool de conexões)
      via :func:`get_http_client` para não abrir outro pool.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, TypedDict
from urllib.parse import urlencode

import httpx

from ..config import Settings, get_settings
from .instagram_client import InstagramAPIError

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# DTOs retornados pelo serviço (isolam o formato da Meta do resto do código)
# --------------------------------------------------------------------------- #
class OAuthToken(TypedDict):
    access_token: str
    # Vence em ``expires_at`` (UTC). None para short-lived ambíguo.
    expires_at: Optional[datetime]
    token_type: Optional[str]
    permissions: Optional[List[str]]
    # ``user_id`` devolvido no short-lived exchange (útil como dica).
    user_id: Optional[str]


class InstagramUserInfo(TypedDict):
    ig_user_id: str  # ID numérico usado em entry[].id do webhook
    app_scoped_id: Optional[str]  # retornado como ``id`` em /me
    username: Optional[str]
    account_type: Optional[str]


class OAuthInstagramService:
    """Encapsula as chamadas HTTP do fluxo OAuth do Instagram."""

    # Endpoints oficiais. Centralizar aqui evita typos espalhados.
    AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
    SHORT_LIVED_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
    LONG_LIVED_TOKEN_URL = "https://graph.instagram.com/access_token"
    REFRESH_TOKEN_URL = "https://graph.instagram.com/refresh_access_token"

    def __init__(self, http_client: httpx.AsyncClient, settings: Settings) -> None:
        self._http = http_client
        self._s = settings
        # Base para endpoints de produto (``/me``, ``/{id}/subscribed_apps``).
        self._base_versioned = (
            f"{settings.graph_api_base_url.rstrip('/')}/{settings.graph_api_version}"
        )

    # ------------------------------------------------------------------ #
    # 1. URL de authorize (frontend redireciona para cá)
    # ------------------------------------------------------------------ #
    def build_authorize_url(self, state: str) -> str:
        """Monta a URL que o navegador do cliente deve abrir."""
        params = {
            "enable_fb_login": "0",
            "force_authentication": "1",
            "client_id": self._s.instagram_app_id,
            "redirect_uri": self._s.oauth_redirect_uri,
            "response_type": "code",
            "scope": ",".join(self._s.oauth_scopes_list),
            "state": state,
        }
        return f"{self.AUTHORIZE_URL}?{urlencode(params)}"

    # ------------------------------------------------------------------ #
    # 2. code -> short-lived token
    # ------------------------------------------------------------------ #
    async def exchange_code(self, *, code: str) -> OAuthToken:
        """Troca o ``code`` do callback por um token de curta duração (~1h)."""
        data = {
            "client_id": self._s.instagram_app_id,
            "client_secret": self._s.instagram_app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": self._s.oauth_redirect_uri,
            "code": code,
        }
        logger.info("OAuth: trocando code por short-lived token")
        resp = await self._http.post(
            self.SHORT_LIVED_TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        payload = _safe_json(resp)
        if resp.is_error:
            raise InstagramAPIError(resp.status_code, payload)

        # Short-lived não traz expires_in explícito; assumimos 1h para
        # termos um limite defensivo caso não consigamos trocar já.
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        return OAuthToken(
            access_token=payload["access_token"],
            expires_at=expires_at,
            token_type=payload.get("token_type"),
            permissions=payload.get("permissions"),
            user_id=str(payload["user_id"]) if payload.get("user_id") else None,
        )

    # ------------------------------------------------------------------ #
    # 3. short-lived -> long-lived (60 dias)
    # ------------------------------------------------------------------ #
    async def exchange_long_lived(self, *, short_lived_token: str) -> OAuthToken:
        """Troca um token de 1h por um de 60 dias."""
        params = {
            "grant_type": "ig_exchange_token",
            "client_secret": self._s.instagram_app_secret,
            "access_token": short_lived_token,
        }
        logger.info("OAuth: trocando short-lived por long-lived token")
        resp = await self._http.get(self.LONG_LIVED_TOKEN_URL, params=params)
        payload = _safe_json(resp)
        if resp.is_error:
            raise InstagramAPIError(resp.status_code, payload)

        expires_at = _expires_at_from(payload.get("expires_in"))
        return OAuthToken(
            access_token=payload["access_token"],
            expires_at=expires_at,
            token_type=payload.get("token_type"),
            permissions=None,
            user_id=None,
        )

    # ------------------------------------------------------------------ #
    # 4. Refresh do long-lived (válido entre 24h e 60d após emissão)
    # ------------------------------------------------------------------ #
    async def refresh(self, *, long_lived_token: str) -> OAuthToken:
        """Prorroga um long-lived por mais 60 dias.

        A Meta exige que o token tenha pelo menos 24h e no máximo 60 dias.
        Se chamarmos fora da janela, a resposta vem com erro — deixamos
        bubble up para o caller.
        """
        params = {
            "grant_type": "ig_refresh_token",
            "access_token": long_lived_token,
        }
        logger.info("OAuth: refresh do long-lived token")
        resp = await self._http.get(self.REFRESH_TOKEN_URL, params=params)
        payload = _safe_json(resp)
        if resp.is_error:
            raise InstagramAPIError(resp.status_code, payload)

        expires_at = _expires_at_from(payload.get("expires_in"))
        return OAuthToken(
            access_token=payload["access_token"],
            expires_at=expires_at,
            token_type=payload.get("token_type"),
            permissions=None,
            user_id=None,
        )

    # ------------------------------------------------------------------ #
    # 5. /me — descobre ig_user_id (Business Account ID) e username
    # ------------------------------------------------------------------ #
    async def get_me(self, *, access_token: str) -> InstagramUserInfo:
        """Lê identidades da conta IG profissional."""
        params = {
            "fields": "id,user_id,username,account_type",
            "access_token": access_token,
        }
        resp = await self._http.get(f"{self._base_versioned}/me", params=params)
        payload = _safe_json(resp)
        if resp.is_error:
            raise InstagramAPIError(resp.status_code, payload)

        return InstagramUserInfo(
            # ``user_id`` é o Business Account ID (o que aparece em entry[].id
            # dos webhooks). Se por algum motivo vier vazio (contas muito
            # novas), caímos para ``id`` como fallback — evita KeyError.
            ig_user_id=str(payload.get("user_id") or payload["id"]),
            app_scoped_id=str(payload["id"]) if payload.get("id") else None,
            username=payload.get("username"),
            account_type=payload.get("account_type"),
        )

    # ------------------------------------------------------------------ #
    # 6. Assina o app para receber webhooks de comments
    # ------------------------------------------------------------------ #
    async def subscribe_app(
        self,
        *,
        ig_user_id: str,
        access_token: str,
        fields: str = "comments",
    ) -> Dict[str, Any]:
        """Chama ``POST /{ig_user_id}/subscribed_apps`` para ativar webhooks.

        Sem este passo, **nenhum** comentário chega no nosso endpoint —
        o cliente terminaria o OAuth e o produto pareceria quebrado.
        """
        url = f"{self._base_versioned}/{ig_user_id}/subscribed_apps"
        params = {"subscribed_fields": fields, "access_token": access_token}
        logger.info("OAuth: assinando webhooks | ig_user_id=%s fields=%s", ig_user_id, fields)
        resp = await self._http.post(url, params=params)
        payload = _safe_json(resp)
        if resp.is_error:
            raise InstagramAPIError(resp.status_code, payload)
        return payload


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _safe_json(resp: httpx.Response) -> Dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text}
    return data if isinstance(data, dict) else {"raw": data}


def _expires_at_from(expires_in: Any) -> Optional[datetime]:
    """Converte ``expires_in`` (segundos) em timestamp UTC absoluto."""
    try:
        seconds = int(expires_in) if expires_in is not None else None
    except (TypeError, ValueError):
        return None
    if seconds is None:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


# --------------------------------------------------------------------------- #
# Factory: reaproveita o http client do InstagramClient singleton
# --------------------------------------------------------------------------- #
def build_oauth_service(
    http_client: httpx.AsyncClient,
    settings: Optional[Settings] = None,
) -> OAuthInstagramService:
    return OAuthInstagramService(http_client, settings or get_settings())
