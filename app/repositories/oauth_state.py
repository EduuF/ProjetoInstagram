"""Repositório de :class:`OAuthState` — nonces CSRF do fluxo OAuth IG.

Padrões importantes:

- :meth:`create` sempre gera o state com :func:`secrets.token_urlsafe` (32
  bytes = 256 bits de entropia). Nunca aceita state vindo do caller — assim
  evita o bug clássico de "gerar state na camada de API" e esquecer de
  randomizar direito.

- :meth:`consume` é atômico: faz SELECT + UPDATE do ``consumed_at`` dentro
  da mesma transação, retornando ``None`` se o state não existir, já tiver
  sido consumido, ou estiver expirado. Isso garante que um callback
  replayado (mesmo state) não passe duas vezes.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db.models import OAuthState


class OAuthStateRepository:
    """Acesso a :class:`OAuthState`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        client_id: str,
        redirect_after: Optional[str] = None,
    ) -> OAuthState:
        """Cria um novo state aleatório, persiste e retorna o objeto."""
        settings = get_settings()
        now = datetime.now(timezone.utc)
        row = OAuthState(
            client_id=client_id,
            state=secrets.token_urlsafe(32),
            redirect_after=redirect_after,
            created_at=now,
            expires_at=now + timedelta(minutes=settings.oauth_state_expires_minutes),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def consume(self, state: str) -> Optional[OAuthState]:
        """Valida e consome um state. Retorna ``None`` se inválido.

        "Inválido" cobre três casos:
            - state não existe no banco;
            - state já foi consumido antes (``consumed_at`` não nulo);
            - state expirou (``expires_at`` <= agora).

        Se válido, marca ``consumed_at`` e retorna o registro com os
        dados associados (``client_id``, ``redirect_after``).
        """
        stmt = select(OAuthState).where(OAuthState.state == state)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None

        now = datetime.now(timezone.utc)
        # Como o banco pode retornar timestamps naive dependendo do driver,
        # garantimos UTC antes de comparar.
        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if row.consumed_at is not None or expires_at <= now:
            return None

        row.consumed_at = now
        await self._session.flush()
        return row
