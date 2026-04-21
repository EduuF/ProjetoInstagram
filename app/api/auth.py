"""Rotas de autenticação dos clientes do SaaS.

Endpoints:
    - ``POST /auth/signup`` — cria um cliente (e-mail + senha), seta cookie de sessão.
    - ``POST /auth/login``  — valida credenciais e seta cookie de sessão.
    - ``POST /auth/logout`` — limpa o cookie.
    - ``GET  /auth/me``     — retorna o cliente autenticado.

Modelo de sessão:
    O JWT é **setado em cookie HttpOnly + SameSite=Lax + Secure (prod)**, o
    que significa que JavaScript do frontend **não lê** o valor — blindagem
    natural contra roubo de token por XSS. O browser envia o cookie
    automaticamente em toda request.

    Para compatibilidade com scripts e ferramentas (curl, Postman, testes),
    a dependency ``current_client`` aceita também ``Authorization: Bearer
    <jwt>`` como fallback. O signup/login continuam devolvendo o token no
    body também — exclusivamente para esses casos não-browser.

Segurança adicional:
    - Respostas 401 são intencionalmente genéricas em falhas de login
      (``invalid_credentials``) para dificultar enumeração de contas.
    - Senha mínima: 8 caracteres (validado por Pydantic no schema).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..db.models import Client
from ..services.client_auth import (
    ClientAuthService,
    EmailAlreadyUsed,
    InactiveClient,
    InvalidCredentials,
)
from .deps import current_client, db_session

router = APIRouter(prefix="/auth", tags=["auth"])


# --------------------------------------------------------------------------- #
# Schemas de request/response
# --------------------------------------------------------------------------- #
class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    name: str | None = Field(default=None, max_length=255)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class AuthResponse(BaseModel):
    """Payload de resposta do signup/login.

    ``access_token`` é redundante quando o cliente é um browser (o cookie já
    foi setado), mas mantido para quem usa ``Authorization: Bearer``.
    """

    access_token: str
    token_type: str = "bearer"
    client_id: str
    email: str


class MeResponse(BaseModel):
    id: str
    email: str
    name: str | None
    is_active: bool


# --------------------------------------------------------------------------- #
# Cookie helpers
# --------------------------------------------------------------------------- #
def _set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    """Escreve o cookie de sessão com as flags de segurança vigentes."""
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.jwt_expires_minutes * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        domain=settings.cookie_domain,
        path="/",
    )


def _clear_session_cookie(response: Response, settings: Settings) -> None:
    """Limpa o cookie (logout)."""
    response.delete_cookie(
        key=settings.session_cookie_name,
        domain=settings.cookie_domain,
        path="/",
    )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.post(
    "/signup",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Cria conta no SaaS, seta cookie de sessão.",
)
async def signup(
    body: SignupRequest,
    response: Response,
    session: AsyncSession = Depends(db_session),
    settings: Settings = Depends(get_settings),
) -> AuthResponse:
    service = ClientAuthService(session)
    try:
        client, token = await service.signup(
            email=body.email,
            password=body.password,
            name=body.name,
        )
    except EmailAlreadyUsed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already in use",
        )

    _set_session_cookie(response, token, settings)
    return AuthResponse(
        access_token=token,
        client_id=client.id,
        email=client.email,
    )


@router.post(
    "/login",
    response_model=AuthResponse,
    summary="Autentica e seta cookie de sessão.",
)
async def login(
    body: LoginRequest,
    response: Response,
    session: AsyncSession = Depends(db_session),
    settings: Settings = Depends(get_settings),
) -> AuthResponse:
    service = ClientAuthService(session)
    try:
        client, token = await service.login(email=body.email, password=body.password)
    except InvalidCredentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_credentials",
        )
    except InactiveClient:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="client_inactive",
        )

    _set_session_cookie(response, token, settings)
    return AuthResponse(
        access_token=token,
        client_id=client.id,
        email=client.email,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Encerra sessão limpando o cookie.",
)
async def logout(
    settings: Settings = Depends(get_settings),
) -> Response:
    # Construímos a response aqui e aplicamos ``delete_cookie`` direto
    # nela — do contrário, uma ``Response`` nova substituiria as mudanças
    # que tivéssemos feito no parâmetro injetado.
    resp = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_session_cookie(resp, settings)
    return resp


@router.get(
    "/me",
    response_model=MeResponse,
    summary="Retorna o cliente autenticado.",
)
async def me(client: Client = Depends(current_client)) -> MeResponse:
    return MeResponse(
        id=client.id,
        email=client.email,
        name=client.name,
        is_active=client.is_active,
    )
