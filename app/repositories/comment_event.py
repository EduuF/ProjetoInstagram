"""Repositório de :class:`CommentEvent` (log de comentários recebidos)."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional, Sequence

from sqlalchemy import Date, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import CommentEvent


class CommentEventRepository:
    """Acesso a :class:`CommentEvent`.

    A unicidade de ``comment_id`` é nossa garantia de idempotência: se o
    INSERT violar a UNIQUE constraint, sabemos que o evento já foi
    processado antes e devemos pular.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_comment_id(self, comment_id: str) -> Optional[CommentEvent]:
        """Retorna o evento pelo ``comment_id`` do Instagram, se existir."""
        stmt = select(CommentEvent).where(CommentEvent.comment_id == comment_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_if_absent(
        self,
        event: CommentEvent,
    ) -> tuple[CommentEvent, bool]:
        """Cria o evento, mas aceita a duplicidade graciosamente.

        Estratégia:

        1. **Fast path**: SELECT pelo ``comment_id`` (reentrega é comum).
        2. **Slow path**: se não existir, tenta INSERT dentro de um
           ``SAVEPOINT`` (``begin_nested``). Se houver race e der
           ``IntegrityError``, só o SAVEPOINT é desfeito — a transação
           externa segue íntegra (sem invalidar ``account`` & companhia).

        Returns:
            (CommentEvent, created): ``created=True`` se inseriu agora,
            ``False`` se o ``comment_id`` já existia (idempotência).
        """
        # Fast path
        existing = await self.get_by_comment_id(event.comment_id)
        if existing is not None:
            return existing, False

        # Slow path com SAVEPOINT
        try:
            async with self._session.begin_nested():
                self._session.add(event)
                await self._session.flush()
            return event, True
        except IntegrityError:
            # Outra task inseriu o mesmo comment_id entre nosso SELECT e
            # o INSERT. O SAVEPOINT já foi desfeito; recarregamos e seguimos.
            existing = await self.get_by_comment_id(event.comment_id)
            if existing is None:
                raise
            return existing, False

    async def list_for_account(
        self,
        instagram_account_id: str,
        *,
        limit: int = 50,
        before: Optional[datetime] = None,
    ) -> Sequence[CommentEvent]:
        """Lista eventos mais recentes primeiro, com paginação cursor-based.

        O "cursor" é o ``received_at`` do último item carregado; passamos-o
        como ``before`` para trazer registros mais antigos que ele. É
        estável mesmo com inserts concorrentes (diferente de OFFSET).
        """
        stmt = (
            select(CommentEvent)
            .where(CommentEvent.instagram_account_id == instagram_account_id)
            .order_by(CommentEvent.received_at.desc())
            .limit(limit)
        )
        if before is not None:
            stmt = stmt.where(CommentEvent.received_at < before)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def count_since(
        self,
        instagram_account_id: str,
        *,
        since: datetime,
    ) -> int:
        """Quantos comentários chegaram em ``[since, agora]``."""
        stmt = (
            select(func.count(CommentEvent.id))
            .where(CommentEvent.instagram_account_id == instagram_account_id)
            .where(CommentEvent.received_at >= since)
        )
        return int((await self._session.execute(stmt)).scalar_one())

    # --------------------------------------------------------------- #
    # Agregações multi-conta (usadas na Visão Geral do dashboard)
    # --------------------------------------------------------------- #
    async def count_for_accounts(
        self,
        account_ids: Iterable[str],
        *,
        since: datetime,
    ) -> int:
        """Total de comentários recebidos em um conjunto de contas desde ``since``.

        Usada quando o cliente filtra "Todas as contas" — fazer uma query
        agregada custa menos round-trips que chamar ``count_since`` por conta.
        """
        ids = list(account_ids)
        if not ids:
            return 0
        stmt = (
            select(func.count(CommentEvent.id))
            .where(CommentEvent.instagram_account_id.in_(ids))
            .where(CommentEvent.received_at >= since)
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def timeseries_by_day(
        self,
        account_ids: Iterable[str],
        *,
        since: datetime,
    ) -> list[tuple[datetime, int]]:
        """Série temporal diária: (dia_utc, total_comentarios_recebidos).

        Usa ``DATE(received_at)`` como bucket — dias em UTC. Isso evita
        dependência do timezone do banco e do front. O front pode formatar
        como preferir (``ptBR``).

        A agregação é feita no banco — mais rápida que trazer todos os
        comentários e agrupar no Python.
        """
        ids = list(account_ids)
        if not ids:
            return []
        bucket = cast(CommentEvent.received_at, Date).label("bucket")
        stmt = (
            select(bucket, func.count(CommentEvent.id))
            .where(CommentEvent.instagram_account_id.in_(ids))
            .where(CommentEvent.received_at >= since)
            .group_by(bucket)
            .order_by(bucket.asc())
        )
        rows = (await self._session.execute(stmt)).all()
        # bucket vem como ``date`` (python). Convertemos para datetime à meia-noite UTC.
        return [(row[0], int(row[1])) for row in rows]

    async def list_for_accounts(
        self,
        account_ids: Iterable[str],
        *,
        limit: int = 50,
        before: Optional[datetime] = None,
        since: Optional[datetime] = None,
    ) -> Sequence[CommentEvent]:
        """Lista eventos de várias contas (agregado do dashboard).

        ``since`` limita o começo do intervalo (usado nos drill-downs
        "Comentários dos últimos 7d").
        """
        ids = list(account_ids)
        if not ids:
            return []
        stmt = (
            select(CommentEvent)
            .where(CommentEvent.instagram_account_id.in_(ids))
            .order_by(CommentEvent.received_at.desc())
            .limit(limit)
        )
        if before is not None:
            stmt = stmt.where(CommentEvent.received_at < before)
        if since is not None:
            stmt = stmt.where(CommentEvent.received_at >= since)
        result = await self._session.execute(stmt)
        return result.scalars().all()
