"""Rotas de gerenciamento de contas IG pelo dashboard.

Todas as rotas exigem autenticação do cliente do SaaS (via ``current_client``)
e autorização horizontal (``require_account_owned``). Um cliente nunca
consegue ver/editar contas ou eventos de outro.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (
    AutoReplySent,
    AutoReplyStatus,
    Client,
    CommentEvent,
    InstagramAccount,
)
from ..repositories.auto_reply_rule import AutoReplyRuleRepository
from ..repositories.auto_reply_sent import AutoReplySentRepository
from ..repositories.comment_event import CommentEventRepository
from ..repositories.instagram_account import InstagramAccountRepository
from .authz import require_account_owned
from .deps import current_client, db_session

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


# --------------------------------------------------------------------------- #
# Schemas de saída
# --------------------------------------------------------------------------- #
class AccountRead(BaseModel):
    """Representação pública de :class:`InstagramAccount`.

    Não expõe ``access_token`` nem qualquer campo criptografado.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    ig_business_account_id: str
    ig_user_id: Optional[str]
    username: Optional[str]
    is_active: bool
    token_expires_at: Optional[datetime]
    last_refreshed_at: Optional[datetime]
    created_at: datetime


class AccountStats(BaseModel):
    """Agregados para os cards do dashboard."""

    range_days: int
    comments_received: int
    replies_sent: int
    replies_failed: int
    replies_skipped_no_rule: int
    success_rate: float  # sent / (sent + failed), em [0,1]


class CommentEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    comment_id: str
    media_id: Optional[str]
    commenter_username: Optional[str]
    text: Optional[str]
    received_at: datetime


class AutoReplySentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: AutoReplyStatus
    recipient_igsid: Optional[str]
    message_id: Optional[str]
    rendered_text: Optional[str]
    error_code: Optional[int]
    error_message: Optional[str]
    created_at: datetime
    # Contexto do comentário, pra UI não precisar buscar separado.
    comment_text: Optional[str] = None
    comment_username: Optional[str] = None


class Paginated(BaseModel):
    """Envelope de paginação cursor-based."""

    items: list  # type: ignore[type-arg]
    next_cursor: Optional[str] = None


class RulePerformance(BaseModel):
    """Desempenho agregado de uma regra no período analisado."""

    rule_id: str
    trigger_word: str
    is_active: bool
    priority: int
    matched: int            # quantas vezes a regra foi escolhida (sent + failed)
    sent: int               # respostas com status=SENT
    failed: int             # respostas com status=FAILED
    success_rate: float     # sent / (sent + failed), em [0,1]
    last_sent_at: Optional[datetime]


# --------------------------------------------------------------------------- #
# Rotas
# --------------------------------------------------------------------------- #
@router.get(
    "",
    response_model=list[AccountRead],
    summary="Lista as contas IG do cliente autenticado.",
)
async def list_accounts(
    client: Client = Depends(current_client),
    session: AsyncSession = Depends(db_session),
) -> list[AccountRead]:
    repo = InstagramAccountRepository(session)
    rows = await repo.list_for_client(client.id)
    return [AccountRead.model_validate(r) for r in rows]


@router.get(
    "/{account_id}",
    response_model=AccountRead,
    summary="Detalhe de uma conta IG.",
)
async def get_account(
    account: InstagramAccount = Depends(require_account_owned),
) -> AccountRead:
    return AccountRead.model_validate(account)


@router.delete(
    "/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Desconecta uma conta IG (soft delete: marca is_active=false).",
)
async def disconnect_account(
    account: InstagramAccount = Depends(require_account_owned),
) -> None:
    # Soft-delete: preserva histórico/auditoria. Um reconnect via OAuth
    # reativa a mesma linha (upsert_from_oauth).
    account.is_active = False


@router.get(
    "/{account_id}/stats",
    response_model=AccountStats,
    summary="Agregados dos últimos N dias para o dashboard.",
)
async def account_stats(
    range_days: int = Query(7, ge=1, le=90),
    account: InstagramAccount = Depends(require_account_owned),
    session: AsyncSession = Depends(db_session),
) -> AccountStats:
    since = datetime.now(timezone.utc) - timedelta(days=range_days)
    comments_repo = CommentEventRepository(session)
    replies_repo = AutoReplySentRepository(session)

    comments = await comments_repo.count_since(account.id, since=since)
    sent = await replies_repo.count_by_status(
        account.id, status=AutoReplyStatus.SENT, since=since
    )
    failed = await replies_repo.count_by_status(
        account.id, status=AutoReplyStatus.FAILED, since=since
    )
    skipped = await replies_repo.count_by_status(
        account.id, status=AutoReplyStatus.SKIPPED_NO_RULE, since=since
    )

    attempts = sent + failed
    success_rate = (sent / attempts) if attempts else 0.0

    return AccountStats(
        range_days=range_days,
        comments_received=comments,
        replies_sent=sent,
        replies_failed=failed,
        replies_skipped_no_rule=skipped,
        success_rate=success_rate,
    )


@router.get(
    "/{account_id}/events",
    summary="Histórico de comentários recebidos (paginado).",
)
async def list_events(
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None, description="ISO timestamp do último item (received_at)"),
    range_days: Optional[int] = Query(
        None, ge=1, le=365, description="Se informado, limita aos últimos N dias."
    ),
    account: InstagramAccount = Depends(require_account_owned),
    session: AsyncSession = Depends(db_session),
) -> dict:
    before = _parse_cursor(cursor)
    since = (
        datetime.now(timezone.utc) - timedelta(days=range_days)
        if range_days is not None
        else None
    )
    repo = CommentEventRepository(session)
    # Reusa o mesmo método multi-conta com uma lista de um só item.
    rows = list(
        await repo.list_for_accounts(
            [account.id], limit=limit, before=before, since=since
        )
    )

    items = [CommentEventRead.model_validate(r) for r in rows]
    next_cursor = _next_cursor(rows, limit, key=lambda r: r.received_at)
    return {"items": [i.model_dump(mode="json") for i in items], "next_cursor": next_cursor}


@router.get(
    "/{account_id}/replies",
    summary="Histórico de respostas enviadas/tentadas (paginado).",
)
async def list_replies(
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None, description="ISO timestamp do último item (created_at)"),
    status_filter: Optional[AutoReplyStatus] = Query(
        None,
        alias="status",
        description="Filtra por status: sent, failed, skipped_no_rule, skipped_duplicate",
    ),
    rule_id: Optional[str] = Query(
        None, description="Se informado, só respostas dessa regra."
    ),
    range_days: Optional[int] = Query(
        None, ge=1, le=365, description="Se informado, limita aos últimos N dias."
    ),
    account: InstagramAccount = Depends(require_account_owned),
    session: AsyncSession = Depends(db_session),
) -> dict:
    before = _parse_cursor(cursor)
    since = (
        datetime.now(timezone.utc) - timedelta(days=range_days)
        if range_days is not None
        else None
    )

    repo = AutoReplySentRepository(session)
    rows = list(
        await repo.list_for_account(
            account.id,
            limit=limit,
            before=before,
            status=status_filter,
            rule_id=rule_id,
            since=since,
        )
    )

    items: list[AutoReplySentRead] = []
    for r in rows:
        item = AutoReplySentRead.model_validate(r)
        # ``comment_event`` vem por ``selectinload``.
        if r.comment_event is not None:
            item.comment_text = r.comment_event.text
            item.comment_username = r.comment_event.commenter_username
        items.append(item)

    next_cursor = _next_cursor(rows, limit, key=lambda r: r.created_at)
    return {"items": [i.model_dump(mode="json") for i in items], "next_cursor": next_cursor}


@router.get(
    "/{account_id}/rules/stats",
    response_model=list[RulePerformance],
    summary="Desempenho por regra no período (matches, sent, failed, última execução).",
)
async def account_rules_stats(
    range_days: int = Query(30, ge=1, le=365),
    account: InstagramAccount = Depends(require_account_owned),
    session: AsyncSession = Depends(db_session),
) -> list[RulePerformance]:
    """Retorna métricas por regra para a conta autorizada.

    Inclui **todas** as regras da conta (mesmo as que não rodaram no período)
    para que o cliente veja regras "ociosas" na tabela. Regras sem atividade
    têm ``matched=0``, ``sent=0``, etc.
    """
    rules_repo = AutoReplyRuleRepository(session)
    replies_repo = AutoReplySentRepository(session)

    since = datetime.now(timezone.utc) - timedelta(days=range_days)
    rules = list(await rules_repo.list_for_account(account.id))
    per_rule = await replies_repo.per_rule_stats(account.id, since=since)
    bucket_by_id = {row["rule_id"]: row for row in per_rule}

    out: list[RulePerformance] = []
    for r in rules:
        bucket = bucket_by_id.get(r.id)
        sent = bucket["sent"] if bucket else 0
        failed = bucket["failed"] if bucket else 0
        attempts = sent + failed
        success_rate = (sent / attempts) if attempts else 0.0
        out.append(
            RulePerformance(
                rule_id=r.id,
                trigger_word=r.trigger_word,
                is_active=r.is_active,
                priority=r.priority,
                matched=attempts,
                sent=sent,
                failed=failed,
                success_rate=success_rate,
                last_sent_at=bucket["last_sent_at"] if bucket else None,
            )
        )
    # Ordena por "mais ativas primeiro", desempatando por prioridade.
    out.sort(key=lambda x: (-x.matched, x.priority))
    return out


# --------------------------------------------------------------------------- #
# Helpers de cursor
# --------------------------------------------------------------------------- #
def _parse_cursor(cursor: Optional[str]) -> Optional[datetime]:
    if not cursor:
        return None
    try:
        dt = datetime.fromisoformat(cursor)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _next_cursor(rows: list, limit: int, *, key) -> Optional[str]:
    # Só devolvemos cursor se a página veio cheia (pode ter mais).
    if len(rows) < limit or not rows:
        return None
    last = key(rows[-1])
    return last.isoformat() if last else None
