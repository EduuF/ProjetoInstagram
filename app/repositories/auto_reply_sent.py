"""Repositório de :class:`AutoReplySent` (log de DMs enviadas/tentadas)."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional, Sequence

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db.models import AutoReplyRule, AutoReplySent, AutoReplyStatus, CommentEvent


class AutoReplySentRepository:
    """Acesso a :class:`AutoReplySent`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, record: AutoReplySent) -> AutoReplySent:
        """Persiste um novo registro de tentativa de resposta."""
        self._session.add(record)
        await self._session.flush()
        return record

    async def list_for_account(
        self,
        instagram_account_id: str,
        *,
        limit: int = 50,
        before: Optional[datetime] = None,
        status: Optional[AutoReplyStatus] = None,
        rule_id: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> Sequence[AutoReplySent]:
        """Lista respostas enviadas (ou tentadas) de uma conta, paginado.

        Junta com ``CommentEvent`` para filtrar por ``instagram_account_id``
        e trazer o texto do comentário junto (``selectinload``).

        Filtros opcionais:
            - ``status``: só envios de determinado status (ex.: ``FAILED``).
            - ``rule_id``: só envios de uma regra específica.
            - ``since``: só envios a partir de uma data (drill-down "7d").
        """
        stmt = (
            select(AutoReplySent)
            .join(CommentEvent, AutoReplySent.comment_event_id == CommentEvent.id)
            .where(CommentEvent.instagram_account_id == instagram_account_id)
            .options(selectinload(AutoReplySent.comment_event))
            .order_by(AutoReplySent.created_at.desc())
            .limit(limit)
        )
        if before is not None:
            stmt = stmt.where(AutoReplySent.created_at < before)
        if status is not None:
            stmt = stmt.where(AutoReplySent.status == status)
        if rule_id is not None:
            stmt = stmt.where(AutoReplySent.rule_id == rule_id)
        if since is not None:
            stmt = stmt.where(AutoReplySent.created_at >= since)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def count_by_status(
        self,
        instagram_account_id: str,
        *,
        status: AutoReplyStatus,
        since: datetime,
    ) -> int:
        """Conta registros por status em um intervalo."""
        stmt = (
            select(func.count(AutoReplySent.id))
            .join(CommentEvent, AutoReplySent.comment_event_id == CommentEvent.id)
            .where(CommentEvent.instagram_account_id == instagram_account_id)
            .where(AutoReplySent.status == status)
            .where(AutoReplySent.created_at >= since)
        )
        return int((await self._session.execute(stmt)).scalar_one())

    # --------------------------------------------------------------- #
    # Agregações multi-conta (usadas na Visão Geral do dashboard)
    # --------------------------------------------------------------- #
    async def aggregate_status_counts(
        self,
        account_ids: Iterable[str],
        *,
        since: datetime,
    ) -> dict[AutoReplyStatus, int]:
        """Conta por status em várias contas num só round-trip.

        Retorna um dict com todos os status existentes (zerado se não houver).
        Mais eficiente que chamar ``count_by_status`` uma vez por status +
        uma vez por conta.
        """
        ids = list(account_ids)
        result_map: dict[AutoReplyStatus, int] = {s: 0 for s in AutoReplyStatus}
        if not ids:
            return result_map

        stmt = (
            select(AutoReplySent.status, func.count(AutoReplySent.id))
            .join(CommentEvent, AutoReplySent.comment_event_id == CommentEvent.id)
            .where(CommentEvent.instagram_account_id.in_(ids))
            .where(AutoReplySent.created_at >= since)
            .group_by(AutoReplySent.status)
        )
        rows = (await self._session.execute(stmt)).all()
        for status_val, count_val in rows:
            result_map[status_val] = int(count_val)
        return result_map

    async def timeseries_by_day(
        self,
        account_ids: Iterable[str],
        *,
        since: datetime,
    ) -> list[tuple[datetime, AutoReplyStatus, int]]:
        """Série diária de respostas agrupadas por (dia_utc, status).

        O caller decide como juntar na UI (ex.: duas linhas: sent e failed).
        """
        ids = list(account_ids)
        if not ids:
            return []
        bucket = cast(AutoReplySent.created_at, Date).label("bucket")
        stmt = (
            select(bucket, AutoReplySent.status, func.count(AutoReplySent.id))
            .join(CommentEvent, AutoReplySent.comment_event_id == CommentEvent.id)
            .where(CommentEvent.instagram_account_id.in_(ids))
            .where(AutoReplySent.created_at >= since)
            .group_by(bucket, AutoReplySent.status)
            .order_by(bucket.asc())
        )
        rows = (await self._session.execute(stmt)).all()
        return [(row[0], row[1], int(row[2])) for row in rows]

    async def list_for_accounts(
        self,
        account_ids: Iterable[str],
        *,
        limit: int = 50,
        before: Optional[datetime] = None,
        status: Optional[AutoReplyStatus] = None,
        since: Optional[datetime] = None,
    ) -> Sequence[AutoReplySent]:
        """Drill-down agregado ao "Todas as contas" da Visão Geral."""
        ids = list(account_ids)
        if not ids:
            return []
        stmt = (
            select(AutoReplySent)
            .join(CommentEvent, AutoReplySent.comment_event_id == CommentEvent.id)
            .where(CommentEvent.instagram_account_id.in_(ids))
            .options(selectinload(AutoReplySent.comment_event))
            .order_by(AutoReplySent.created_at.desc())
            .limit(limit)
        )
        if before is not None:
            stmt = stmt.where(AutoReplySent.created_at < before)
        if status is not None:
            stmt = stmt.where(AutoReplySent.status == status)
        if since is not None:
            stmt = stmt.where(AutoReplySent.created_at >= since)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def per_rule_stats(
        self,
        instagram_account_id: str,
        *,
        since: datetime,
    ) -> list[dict]:
        """Desempenho agregado por regra dentro de uma conta.

        Retorna uma lista de dicts::

            [
                {
                    "rule_id": str,
                    "sent": int,
                    "failed": int,
                    "matched": int,          # sent + failed
                    "last_sent_at": datetime | None,
                },
                ...
            ]

        "matched" é o número de vezes que a regra de fato foi escolhida
        (ou seja, quando ``rule_id`` está preenchido no AutoReplySent). Não
        inclui ``SKIPPED_NO_RULE`` — esse é contabilizado em outro card.
        """
        stmt = (
            select(
                AutoReplySent.rule_id,
                AutoReplySent.status,
                func.count(AutoReplySent.id),
                func.max(AutoReplySent.created_at),
            )
            .join(CommentEvent, AutoReplySent.comment_event_id == CommentEvent.id)
            .where(CommentEvent.instagram_account_id == instagram_account_id)
            .where(AutoReplySent.rule_id.is_not(None))
            .where(AutoReplySent.created_at >= since)
            .group_by(AutoReplySent.rule_id, AutoReplySent.status)
        )
        rows = (await self._session.execute(stmt)).all()

        buckets: dict[str, dict] = {}
        for rule_id, status_val, count_val, last_at in rows:
            b = buckets.setdefault(
                rule_id,
                {"rule_id": rule_id, "sent": 0, "failed": 0, "matched": 0, "last_sent_at": None},
            )
            if status_val == AutoReplyStatus.SENT:
                b["sent"] += int(count_val)
            elif status_val == AutoReplyStatus.FAILED:
                b["failed"] += int(count_val)
            b["matched"] = b["sent"] + b["failed"]
            # Último envio (SENT apenas) para coluna "Última vez usada".
            if status_val == AutoReplyStatus.SENT:
                if b["last_sent_at"] is None or (last_at and last_at > b["last_sent_at"]):
                    b["last_sent_at"] = last_at

        return list(buckets.values())
