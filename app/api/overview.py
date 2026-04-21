"""Endpoints agregados para a Visão Geral do dashboard.

Todos os endpoints aqui respondem a agregações **multi-conta** do cliente
autenticado, opcionalmente filtradas por uma conta específica (via query
``account_id=``). A autorização horizontal é feita in-line: se o cliente
informar um ``account_id`` que não é dele, resposta 404 (mesma política do
:mod:`app.api.authz` — não vazamos existência de recursos de outros tenants).

Motivação:
    Antes, a tela ``/dashboard`` fazia N chamadas (uma por conta) no SSR
    e agregava no cliente. Isso:
      - tem overhead de round-trip,
      - dificulta filtros dinâmicos (conta/período),
      - duplica lógica no front.
    Aqui centralizamos: uma query SQL por métrica, agregando contas todas
    de uma vez.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AutoReplyStatus, Client
from ..repositories.auto_reply_sent import AutoReplySentRepository
from ..repositories.comment_event import CommentEventRepository
from ..repositories.instagram_account import InstagramAccountRepository
from .accounts import (
    AutoReplySentRead,
    CommentEventRead,
    _next_cursor,
    _parse_cursor,
)
from .deps import current_client, db_session

router = APIRouter(prefix="/api/overview", tags=["overview"])


# --------------------------------------------------------------------------- #
# Schemas de saída
# --------------------------------------------------------------------------- #
class OverviewStats(BaseModel):
    """Resumo agregado para os cards da Visão Geral.

    Quando ``account_id`` é None, os números somam todas as contas ATIVAS
    do cliente. Quando informado, cobre apenas a conta pedida (com checagem
    de propriedade).
    """

    range_days: int
    account_id: Optional[str]
    accounts_count: int
    active_accounts_count: int
    comments_received: int
    replies_sent: int
    replies_failed: int
    replies_skipped_no_rule: int
    replies_skipped_duplicate: int
    success_rate: float  # sent / (sent + failed), em [0,1]


class TimeseriesPoint(BaseModel):
    """Um ponto da série diária no gráfico de atividade."""

    date: str  # YYYY-MM-DD (UTC)
    comments: int
    sent: int
    failed: int


class OverviewTimeseries(BaseModel):
    range_days: int
    account_id: Optional[str]
    points: list[TimeseriesPoint]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _resolve_account_scope(
    *,
    client: Client,
    account_id: Optional[str],
    session: AsyncSession,
) -> tuple[list[str], int, int]:
    """Resolve a lista de ``account_ids`` a agregar, respeitando autorização.

    Retorna ``(ids, total_accounts_client, active_accounts_client)``.

    Regras:
        - Se ``account_id`` é dado: valida que pertence ao cliente; caso
          contrário, ``404`` (não vazamos existência). Usamos o ID único
          mesmo que a conta esteja inativa (cliente pode querer ver o
          histórico de uma conta desativada).
        - Se não: lista todas as contas do cliente e filtra ATIVAS para o
          scope dos agregados (histórico de contas inativas não polui o
          dashboard principal).
    """
    accounts_repo = InstagramAccountRepository(session)
    all_accounts = list(await accounts_repo.list_for_client(client.id))
    total = len(all_accounts)
    active = [a for a in all_accounts if a.is_active]

    if account_id is None:
        return [a.id for a in active], total, len(active)

    match = next((a for a in all_accounts if a.id == account_id), None)
    if match is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account_not_found")
    return [match.id], total, len(active)


def _since(range_days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=range_days)


# --------------------------------------------------------------------------- #
# Rotas
# --------------------------------------------------------------------------- #
@router.get(
    "/stats",
    response_model=OverviewStats,
    summary="Agregado geral (todas as contas do cliente) ou por conta.",
)
async def overview_stats(
    range_days: int = Query(7, ge=1, le=365),
    account_id: Optional[str] = Query(None),
    client: Client = Depends(current_client),
    session: AsyncSession = Depends(db_session),
) -> OverviewStats:
    scope_ids, total, active = await _resolve_account_scope(
        client=client, account_id=account_id, session=session
    )
    since = _since(range_days)

    comments_repo = CommentEventRepository(session)
    replies_repo = AutoReplySentRepository(session)

    comments = await comments_repo.count_for_accounts(scope_ids, since=since)
    status_map = await replies_repo.aggregate_status_counts(scope_ids, since=since)

    sent = status_map.get(AutoReplyStatus.SENT, 0)
    failed = status_map.get(AutoReplyStatus.FAILED, 0)
    skipped_no_rule = status_map.get(AutoReplyStatus.SKIPPED_NO_RULE, 0)
    skipped_duplicate = status_map.get(AutoReplyStatus.SKIPPED_DUPLICATE, 0)
    attempts = sent + failed
    success_rate = (sent / attempts) if attempts else 0.0

    return OverviewStats(
        range_days=range_days,
        account_id=account_id,
        accounts_count=total,
        active_accounts_count=active,
        comments_received=comments,
        replies_sent=sent,
        replies_failed=failed,
        replies_skipped_no_rule=skipped_no_rule,
        replies_skipped_duplicate=skipped_duplicate,
        success_rate=success_rate,
    )


@router.get(
    "/timeseries",
    response_model=OverviewTimeseries,
    summary="Série diária de comentários e respostas para o gráfico.",
)
async def overview_timeseries(
    range_days: int = Query(30, ge=1, le=365),
    account_id: Optional[str] = Query(None),
    client: Client = Depends(current_client),
    session: AsyncSession = Depends(db_session),
) -> OverviewTimeseries:
    scope_ids, _, _ = await _resolve_account_scope(
        client=client, account_id=account_id, session=session
    )
    since = _since(range_days)

    comments_repo = CommentEventRepository(session)
    replies_repo = AutoReplySentRepository(session)

    # Dois datasets: comentários por dia e respostas por (dia, status).
    comments_series = await comments_repo.timeseries_by_day(scope_ids, since=since)
    replies_series = await replies_repo.timeseries_by_day(scope_ids, since=since)

    # Consolida em um dicionário indexado por data ISO (YYYY-MM-DD).
    buckets: dict[str, TimeseriesPoint] = {}

    def _key(d) -> str:
        # ``d`` pode ser date ou datetime; normalizamos para string ISO.
        if hasattr(d, "isoformat"):
            s = d.isoformat()
            return s[:10]  # só a parte da data
        return str(d)

    for day, count in comments_series:
        k = _key(day)
        buckets.setdefault(
            k, TimeseriesPoint(date=k, comments=0, sent=0, failed=0)
        ).comments = count

    for day, status_val, count in replies_series:
        k = _key(day)
        p = buckets.setdefault(k, TimeseriesPoint(date=k, comments=0, sent=0, failed=0))
        if status_val == AutoReplyStatus.SENT:
            p.sent = count
        elif status_val == AutoReplyStatus.FAILED:
            p.failed = count

    # Preenche dias sem atividade com zeros — o gráfico fica mais honesto e
    # o cliente não "pula" dias vazios visualmente.
    now = datetime.now(timezone.utc).date()
    start = (datetime.now(timezone.utc) - timedelta(days=range_days)).date()
    cursor = start
    while cursor <= now:
        k = cursor.isoformat()
        buckets.setdefault(k, TimeseriesPoint(date=k, comments=0, sent=0, failed=0))
        cursor = cursor.fromordinal(cursor.toordinal() + 1)

    ordered = sorted(buckets.values(), key=lambda p: p.date)
    return OverviewTimeseries(
        range_days=range_days, account_id=account_id, points=ordered
    )


# --------------------------------------------------------------------------- #
# Drill-down: listagem agregada de comentários e respostas
# --------------------------------------------------------------------------- #
@router.get(
    "/events",
    summary="Drill-down: comentários recebidos (agregado de todas as contas ou por conta).",
)
async def overview_events(
    range_days: int = Query(7, ge=1, le=365),
    account_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None, description="ISO timestamp do último item"),
    client: Client = Depends(current_client),
    session: AsyncSession = Depends(db_session),
) -> dict:
    scope_ids, _, _ = await _resolve_account_scope(
        client=client, account_id=account_id, session=session
    )
    since = _since(range_days)
    before = _parse_cursor(cursor)

    repo = CommentEventRepository(session)
    rows = list(
        await repo.list_for_accounts(
            scope_ids, limit=limit, before=before, since=since
        )
    )

    items = [CommentEventRead.model_validate(r) for r in rows]
    next_cursor = _next_cursor(rows, limit, key=lambda r: r.received_at)
    return {"items": [i.model_dump(mode="json") for i in items], "next_cursor": next_cursor}


@router.get(
    "/replies",
    summary="Drill-down: respostas enviadas/falhas (agregado).",
)
async def overview_replies(
    range_days: int = Query(7, ge=1, le=365),
    account_id: Optional[str] = Query(None),
    status_filter: Optional[AutoReplyStatus] = Query(
        None, alias="status", description="Filtra por status: sent, failed, skipped_no_rule, skipped_duplicate",
    ),
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None, description="ISO timestamp do último item"),
    client: Client = Depends(current_client),
    session: AsyncSession = Depends(db_session),
) -> dict:
    scope_ids, _, _ = await _resolve_account_scope(
        client=client, account_id=account_id, session=session
    )
    since = _since(range_days)
    before = _parse_cursor(cursor)

    repo = AutoReplySentRepository(session)
    rows = list(
        await repo.list_for_accounts(
            scope_ids,
            limit=limit,
            before=before,
            status=status_filter,
            since=since,
        )
    )

    items: list[AutoReplySentRead] = []
    for r in rows:
        item = AutoReplySentRead.model_validate(r)
        if r.comment_event is not None:
            item.comment_text = r.comment_event.text
            item.comment_username = r.comment_event.commenter_username
        items.append(item)

    next_cursor = _next_cursor(rows, limit, key=lambda r: r.created_at)
    return {"items": [i.model_dump(mode="json") for i in items], "next_cursor": next_cursor}
