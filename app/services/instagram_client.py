"""Cliente HTTP assíncrono para a Graph API do Instagram.

Diferente da versão monolítica anterior, aqui a classe **não carrega um
token**: ela é stateless em relação a credenciais. Cada chamada recebe o
``access_token`` do tenant (cliente dono da conta IG) — essencial para o
cenário multi-tenant.

O ``httpx.AsyncClient`` é compartilhado por todo o processo (singleton)
porque manter um único pool de conexões HTTP/2 reaproveitáveis reduz
bastante a latência quando temos muitos webhooks concorrentes.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from ..config import Settings, get_settings

logger = logging.getLogger(__name__)


class InstagramAPIError(Exception):
    """Erro estruturado devolvido pela Graph API.

    Contém ``status_code`` HTTP e os campos padrão ``code``, ``error_subcode``,
    ``message`` e ``fbtrace_id`` do envelope de erro da Meta.
    """

    def __init__(self, status_code: int, payload: Dict[str, Any]) -> None:
        self.status_code = status_code
        self.payload = payload
        err = payload.get("error", {}) if isinstance(payload, dict) else {}
        self.code: Optional[int] = err.get("code")
        self.subcode: Optional[int] = err.get("error_subcode")
        self.message: str = err.get("message") or "Erro desconhecido da Graph API"
        self.fbtrace_id: Optional[str] = err.get("fbtrace_id")
        super().__init__(
            f"[HTTP {status_code}] code={self.code} subcode={self.subcode} - {self.message}"
        )


class InstagramClient:
    """Wrapper async sobre a Graph API (endpoints que usamos)."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        api_version: str = "v25.0",
        base_url: str = "https://graph.instagram.com",
    ) -> None:
        self._http = http_client
        self._base = f"{base_url.rstrip('/')}/{api_version}"

    # ------------------------------------------------------------------ #
    # Private Reply
    # ------------------------------------------------------------------ #
    async def send_private_reply(
        self,
        *,
        access_token: str,
        comment_id: str,
        text: str,
    ) -> Dict[str, Any]:
        """Envia uma Private Reply (DM) ao autor de um comentário.

        Args:
            access_token: Token de longa duração da conta IG do cliente dono
                do comentário (multi-tenant).
            comment_id: ID do comentário (``recipient.comment_id``).
            text: Texto do DM a enviar.

        Returns:
            Dict com ``recipient_id`` e ``message_id`` em caso de sucesso.

        Raises:
            InstagramAPIError: se a API devolver 4xx/5xx.
            httpx.HTTPError: em falhas de rede / timeout.
        """
        url = f"{self._base}/me/messages"
        body = {
            "recipient": {"comment_id": comment_id},
            "message": {"text": text},
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        logger.info("Enviando Private Reply | comment_id=%s", comment_id)
        response = await self._http.post(url, json=body, headers=headers)

        try:
            data: Dict[str, Any] = response.json()
        except ValueError:
            data = {"raw": response.text}

        if response.is_error:
            logger.error(
                "Falha na Private Reply | status=%s payload=%s",
                response.status_code,
                data,
            )
            raise InstagramAPIError(response.status_code, data)

        logger.info(
            "Private Reply enviada | recipient_id=%s message_id=%s",
            data.get("recipient_id"),
            data.get("message_id"),
        )
        return data


# --------------------------------------------------------------------------- #
# Singleton ao nível de processo
# --------------------------------------------------------------------------- #
_http_client: Optional[httpx.AsyncClient] = None
_instagram: Optional[InstagramClient] = None


async def startup_instagram_client(settings: Optional[Settings] = None) -> InstagramClient:
    """Inicializa o cliente HTTP e retorna a instância singleton.

    Chamado no ``lifespan`` do FastAPI (ver ``main.py``). Criar o
    ``AsyncClient`` uma única vez mantém o pool de conexões aberto.
    """
    global _http_client, _instagram
    s = settings or get_settings()
    _http_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
    _instagram = InstagramClient(
        http_client=_http_client,
        api_version=s.graph_api_version,
        base_url=s.graph_api_base_url,
    )
    return _instagram


async def shutdown_instagram_client() -> None:
    """Fecha o ``AsyncClient`` no shutdown da aplicação."""
    global _http_client, _instagram
    if _http_client is not None:
        await _http_client.aclose()
    _http_client = None
    _instagram = None


def get_instagram_client() -> InstagramClient:
    """Retorna o singleton. Levanta se o lifespan ainda não rodou."""
    if _instagram is None:
        raise RuntimeError(
            "InstagramClient não inicializado. "
            "Certifique-se de que o lifespan do FastAPI rodou (startup)."
        )
    return _instagram


def get_http_client() -> httpx.AsyncClient:
    """Acesso ao ``httpx.AsyncClient`` compartilhado (pool de conexões único).

    Usado por outros serviços HTTP (ex.: ``oauth_instagram``) para não
    abrir um pool paralelo.
    """
    if _http_client is None:
        raise RuntimeError(
            "httpx.AsyncClient não inicializado. Rode startup_instagram_client()."
        )
    return _http_client
