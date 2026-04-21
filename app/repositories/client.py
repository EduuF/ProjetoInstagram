"""Repositório de :class:`Client`."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Client


class ClientRepository:
    """Acesso a :class:`Client`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, client_id: str) -> Optional[Client]:
        """Busca um cliente pelo UUID (PK)."""
        stmt = select(Client).where(Client.id == client_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> Optional[Client]:
        """Busca um cliente pelo e-mail (único).

        Normaliza o e-mail para lowercase — no :func:`add` também fazemos
        isso, garantindo que o índice UNIQUE case.
        """
        stmt = select(Client).where(Client.email == email.lower().strip())
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def add(self, client: Client) -> Client:
        """Persiste um novo cliente. Normaliza o e-mail."""
        client.email = client.email.lower().strip()
        self._session.add(client)
        await self._session.flush()
        return client
