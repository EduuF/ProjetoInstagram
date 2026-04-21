"""Autenticação do SaaS (nosso próprio login, não o do Instagram).

Contém signup e login dos **clientes** (quem usa nossa ferramenta).
Retorna JWTs assinados para serem enviados no header ``Authorization:
Bearer <jwt>`` em chamadas subsequentes.

Erros expostos intencionalmente genéricos:
    - Para não vazar "e-mail existe / não existe / senha errada", usamos
      :class:`InvalidCredentials` tanto quando o usuário não existe quanto
      quando a senha está errada. Isso dificulta enumeração de contas.
"""

from __future__ import annotations

import logging
from typing import Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Client
from ..repositories.client import ClientRepository
from ..security.jwt import create_access_token
from ..security.passwords import hash_password, verify_password

logger = logging.getLogger(__name__)


class EmailAlreadyUsed(Exception):
    """Tentativa de signup com e-mail já cadastrado."""


class InvalidCredentials(Exception):
    """E-mail/senha inválidos (intencionalmente genérico)."""


class InactiveClient(Exception):
    """Cliente existe mas está inativo (ex.: pós-data-deletion)."""


class ClientAuthService:
    """Orquestra signup/login + geração de JWT."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._clients = ClientRepository(session)

    async def signup(
        self,
        *,
        email: str,
        password: str,
        name: str | None = None,
    ) -> Tuple[Client, str]:
        """Cria um novo cliente e devolve (client, jwt)."""
        email_norm = email.lower().strip()
        existing = await self._clients.get_by_email(email_norm)
        if existing is not None:
            # Caller deve retornar 409 (ou 200 genérico, se priorizar privacidade).
            raise EmailAlreadyUsed(email_norm)

        client = Client(
            email=email_norm,
            name=name,
            password_hash=hash_password(password),
            is_active=True,
        )
        await self._clients.add(client)
        # Não commit aqui — deixamos para o dependency ``get_session`` do
        # FastAPI (commit on success, rollback on exception).

        token = create_access_token(client_id=client.id, email=client.email)
        logger.info("Signup: client_id=%s email=%s", client.id, client.email)
        return client, token

    async def login(self, *, email: str, password: str) -> Tuple[Client, str]:
        """Autentica e devolve (client, jwt). Lança :class:`InvalidCredentials`."""
        email_norm = email.lower().strip()
        client = await self._clients.get_by_email(email_norm)
        if client is None or client.password_hash is None:
            # Mantém o tempo de resposta semelhante rodando um hash fake.
            # (bcrypt é intencionalmente lento — comparar evita side-channel
            # fácil de distinguir "usuário não existe" vs "senha errada".)
            verify_password(password, "$2b$12$invalidinvalidinvalidinvalidinvalidinvalidinvalidinv")
            raise InvalidCredentials()

        if not verify_password(password, client.password_hash):
            raise InvalidCredentials()

        if not client.is_active:
            raise InactiveClient(client.id)

        token = create_access_token(client_id=client.id, email=client.email)
        logger.info("Login ok | client_id=%s", client.id)
        return client, token
