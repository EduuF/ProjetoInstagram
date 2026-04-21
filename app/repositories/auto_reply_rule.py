"""Repositório de :class:`AutoReplyRule`."""

from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AutoReplyRule


class AutoReplyRuleRepository:
    """Acesso a :class:`AutoReplyRule`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, rule_id: str) -> Optional[AutoReplyRule]:
        """Busca por PK. Útil para edições e deleções pontuais."""
        stmt = select(AutoReplyRule).where(AutoReplyRule.id == rule_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_account(
        self,
        instagram_account_id: str,
        *,
        only_active: bool = False,
    ) -> Sequence[AutoReplyRule]:
        """Lista regras de uma conta em ordem de prioridade.

        Se ``only_active`` for True, retorna só as regras com
        ``is_active=True`` (usado pelo engine de resposta). Para o dashboard,
        queremos **todas** (incluindo desativadas, para que o cliente possa
        reativá-las).
        """
        stmt = (
            select(AutoReplyRule)
            .where(AutoReplyRule.instagram_account_id == instagram_account_id)
            .order_by(AutoReplyRule.priority.asc(), AutoReplyRule.created_at.asc())
        )
        if only_active:
            stmt = stmt.where(AutoReplyRule.is_active.is_(True))
        result = await self._session.execute(stmt)
        return result.scalars().all()

    # Alias de compatibilidade — código legado (webhook_service) usa este nome.
    async def list_active_for_account(
        self, instagram_account_id: str
    ) -> Sequence[AutoReplyRule]:
        return await self.list_for_account(instagram_account_id, only_active=True)

    async def add(self, rule: AutoReplyRule) -> AutoReplyRule:
        """Persiste uma nova regra."""
        self._session.add(rule)
        await self._session.flush()
        return rule

    async def delete(self, rule: AutoReplyRule) -> None:
        """Deleta fisicamente (usamos cascade SET NULL em auto_replies_sent)."""
        await self._session.delete(rule)
        await self._session.flush()
