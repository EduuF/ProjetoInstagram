"""JSON Web Tokens para autenticação de sessão dos clientes do SaaS.

Usamos HS256 (HMAC-SHA256) — perfeitamente adequado quando o emissor e o
verificador é o mesmo backend (não precisamos de chave pública). Algoritmo
simétrico, assinado com ``Settings.jwt_secret``.

Claims mínimas:
    - ``sub``: client_id (UUID em string).
    - ``email``: para evitar um lookup no banco no happy path de logs.
    - ``iat`` / ``exp``: timestamps UTC (epoch seconds).

Não armazenamos nada sensível no token porque JWTs são, por padrão, apenas
**assinados** (não criptografados) — qualquer um pode decodificar e ler.

Para revogação (quando implementarmos logout "forte" no futuro), precisamos
adicionar uma denylist de jti ou reduzir a expiração. Por ora, ``exp`` curto
(24h) é o controle principal.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from app.config import get_settings


class InvalidTokenError(Exception):
    """Erro genérico ao decodificar/validar um JWT."""


def create_access_token(client_id: str, email: str) -> str:
    """Gera um JWT de acesso para um cliente autenticado."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(client_id),
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_expires_minutes)).timestamp()),
        "typ": "access",
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """Valida assinatura + expiração e retorna o payload.

    Lança :class:`InvalidTokenError` para qualquer falha (assinatura, exp,
    formato, typ incorreto). O chamador só precisa tratar essa exceção.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidTokenError("token expirado") from exc
    except jwt.InvalidTokenError as exc:
        raise InvalidTokenError("token inválido") from exc

    if payload.get("typ") != "access":
        raise InvalidTokenError("tipo de token inesperado")
    if not payload.get("sub"):
        raise InvalidTokenError("token sem subject")

    return payload


__all__ = ["create_access_token", "decode_access_token", "InvalidTokenError"]
