"""Repositório de :class:`InstagramAccount`.

Responde perguntas como: "dado o ``ig_business_account_id`` que veio no
webhook, qual é a conta ativa e quem é seu dono (cliente)?".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db.models import InstagramAccount


class InstagramAccountRepository:
    """Acesso a :class:`InstagramAccount`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_ig_business_account_id(
        self,
        ig_business_account_id: str,
        *,
        only_active: bool = True,
    ) -> Optional[InstagramAccount]:
        """Busca uma conta IG pelo ID da Meta (``entry[].id`` do webhook).

        Já carrega as ``auto_reply_rules`` via ``selectinload`` para evitar
        o problema clássico de N+1 quando formos iterá-las em seguida.
        """
        stmt = (
            select(InstagramAccount)
            .options(selectinload(InstagramAccount.auto_reply_rules))
            .where(InstagramAccount.ig_business_account_id == ig_business_account_id)
        )
        if only_active:
            stmt = stmt.where(InstagramAccount.is_active.is_(True))

        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(self, account_id: str) -> Optional[InstagramAccount]:
        """Busca por PK. Usado nas rotas do dashboard."""
        stmt = select(InstagramAccount).where(InstagramAccount.id == account_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_client(self, client_id: str) -> Iterable[InstagramAccount]:
        """Todas as contas de um cliente, ativas e inativas, mais recentes primeiro."""
        stmt = (
            select(InstagramAccount)
            .where(InstagramAccount.client_id == client_id)
            .order_by(InstagramAccount.created_at.desc())
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def add(self, account: InstagramAccount) -> InstagramAccount:
        """Persiste uma nova conta (flush imediato para já termos o ``id``)."""
        self._session.add(account)
        await self._session.flush()
        return account

    async def upsert_from_oauth(
        self,
        *,
        client_id: str,
        ig_business_account_id: str,
        ig_user_id: Optional[str],
        username: Optional[str],
        access_token: str,
        token_expires_at: Optional[datetime],
    ) -> InstagramAccount:
        """Cria ou atualiza uma conta IG a partir de um fluxo OAuth completado.

        Se já existe uma conta com o mesmo ``ig_business_account_id``:
            - atualizamos token, username, timestamps;
            - reativamos (``is_active=True``) caso tenha sido desativada;
            - **mantemos** ``client_id`` original. Reautorizar a mesma conta
              IG não a transfere para outro cliente — se precisarmos disso
              no futuro, fica em uma chamada explícita.

        Caso contrário, cria nova.
        """
        stmt = select(InstagramAccount).where(
            InstagramAccount.ig_business_account_id == ig_business_account_id
        )
        result = await self._session.execute(stmt)
        account = result.scalar_one_or_none()

        now = datetime.now(timezone.utc)
        if account is None:
            account = InstagramAccount(
                client_id=client_id,
                ig_business_account_id=ig_business_account_id,
                ig_user_id=ig_user_id,
                username=username,
                access_token=access_token,
                token_expires_at=token_expires_at,
                last_refreshed_at=now,
                is_active=True,
            )
            self._session.add(account)
        else:
            account.ig_user_id = ig_user_id or account.ig_user_id
            account.username = username or account.username
            account.access_token = access_token
            account.token_expires_at = token_expires_at
            account.last_refreshed_at = now
            account.is_active = True

        await self._session.flush()
        return account

    async def deactivate_by_ig_user_id(self, ig_user_id: str) -> int:
        """Marca como inativa toda conta associada a ``ig_user_id``.

        Usado no callback de Deauthorize: a Meta manda o ``user_id`` do app
        que desautorizou; procuramos contas ativas com esse ID e desligamos.
        Retorna a quantidade de linhas afetadas.

        Não apagamos dados — só desativamos. Deleção completa é feita pelo
        callback de Data Deletion (respeitando o prazo da Meta).
        """
        stmt = select(InstagramAccount).where(
            (InstagramAccount.ig_user_id == ig_user_id)
            | (InstagramAccount.ig_business_account_id == ig_user_id)
        )
        result = await self._session.execute(stmt)
        accounts = result.scalars().all()
        count = 0
        for acc in accounts:
            if acc.is_active:
                acc.is_active = False
                count += 1
        await self._session.flush()
        return count

    async def list_needing_refresh(
        self,
        *,
        before: datetime,
    ) -> Iterable[InstagramAccount]:
        """Contas ativas com ``token_expires_at <= before``.

        Usado pelo job de refresh em background.
        """
        stmt = select(InstagramAccount).where(
            InstagramAccount.is_active.is_(True),
            InstagramAccount.token_expires_at.is_not(None),
            InstagramAccount.token_expires_at <= before,
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def update_token(
        self,
        account: InstagramAccount,
        *,
        access_token: str,
        token_expires_at: Optional[datetime],
    ) -> None:
        """Atualiza token + timestamps. Não commit — quem chama decide."""
        account.access_token = access_token
        account.token_expires_at = token_expires_at
        account.last_refreshed_at = datetime.now(timezone.utc)
        await self._session.flush()
