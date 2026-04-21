"""Dependencies reutilizáveis do FastAPI.

Aqui declaramos como cada camada é resolvida quando uma rota pede.
Isso mantém os roteadores enxutos e facilita testes (podemos sobrescrever
com ``app.dependency_overrides``).
"""

from __future__ import annotations

from typing import AsyncIterator, Optional

from fastapi import Cookie, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..db.models import Client
from ..db.session import get_session
from ..repositories.client import ClientRepository
from ..security.jwt import InvalidTokenError, decode_access_token
from ..services.instagram_client import InstagramClient, get_instagram_client as _get_ig
from ..services.oauth_instagram import OAuthInstagramService, build_oauth_service
from ..services.instagram_client import get_http_client


async def db_session() -> AsyncIterator[AsyncSession]:
    """Repassa ``get_session`` como dependency FastAPI."""
    async for session in get_session():
        yield session


def instagram_client() -> InstagramClient:
    """Fornece o :class:`InstagramClient` singleton (criado no lifespan)."""
    return _get_ig()


def oauth_instagram_service(
    settings: Settings = Depends(get_settings),
) -> OAuthInstagramService:
    """Injeta o serviço OAuth, reusando o httpx client singleton."""
    return build_oauth_service(get_http_client(), settings)


def settings_dep(settings: Settings = Depends(get_settings)) -> Settings:
    """Injeta ``Settings`` (singleton cacheado) em uma rota."""
    return settings


# --------------------------------------------------------------------------- #
# JWT / autenticação do cliente do SaaS
# --------------------------------------------------------------------------- #
# ``auto_error=False`` porque queremos controlar a mensagem de erro (401 com
# ``WWW-Authenticate: Bearer`` consistente, sem quebrar se o header faltar
# em endpoints que fizerem a auth opcional no futuro).
_bearer = HTTPBearer(auto_error=False)


def _extract_token(
    credentials: Optional[HTTPAuthorizationCredentials],
    cookie_token: Optional[str],
) -> Optional[str]:
    """Escolhe de onde pegar o JWT.

    Ordem:
        1. Cookie HttpOnly (``access_token``) — usado pelo frontend.
        2. Header ``Authorization: Bearer ...`` — fallback pra scripts/curl.

    Cookie tem prioridade porque é o canal oficial do browser; Bearer é
    suporte para ferramentas e testes.
    """
    if cookie_token:
        return cookie_token
    if credentials is not None and credentials.scheme.lower() == "bearer":
        return credentials.credentials
    return None


async def current_client(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    session: AsyncSession = Depends(db_session),
    settings: Settings = Depends(get_settings),
    access_token: Optional[str] = Cookie(default=None, alias="access_token"),
) -> Client:
    """Dependency: exige JWT válido (via cookie OU Bearer) e retorna o :class:`Client`.

    Use em qualquer rota que o cliente do SaaS precise estar autenticado.
    """
    # ``Cookie(alias="access_token")`` puxa pelo nome padrão; se a config
    # customizar ``session_cookie_name``, precisaríamos de algo mais
    # elaborado. Para MVP o nome é fixo — simplifica bastante.
    token = _extract_token(credentials, access_token)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not_authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decode_access_token(token)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    client_id = payload["sub"]
    repo = ClientRepository(session)
    client = await repo.get_by_id(client_id)
    if client is None or not client.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Client not found or inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return client
