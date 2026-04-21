"""CRUD de :class:`AutoReplyRule` (regras palavra-chave -> mensagem).

Protegido por:
    - :func:`require_account_owned` nas rotas sob ``/api/accounts/{id}/rules``
      (para validar dono da conta).
    - :func:`require_rule_owned` em ``PATCH``/``DELETE /api/rules/{id}`` (para
      validar que a regra pertence a uma conta do cliente autenticado).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AutoReplyRule, InstagramAccount
from ..repositories.auto_reply_rule import AutoReplyRuleRepository
from .authz import require_account_owned, require_rule_owned
from .deps import db_session

router = APIRouter(tags=["rules"])


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class RuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    instagram_account_id: str
    trigger_word: str
    message_template: str
    priority: int
    is_active: bool


class RuleCreate(BaseModel):
    trigger_word: str = Field(min_length=1, max_length=100)
    # Templates podem ter placeholders como ``{username}``.
    message_template: str = Field(min_length=1, max_length=1000)
    priority: int = Field(default=100, ge=0, le=10_000)
    is_active: bool = True


class RuleUpdate(BaseModel):
    trigger_word: str | None = Field(default=None, min_length=1, max_length=100)
    message_template: str | None = Field(default=None, min_length=1, max_length=1000)
    priority: int | None = Field(default=None, ge=0, le=10_000)
    is_active: bool | None = None


# --------------------------------------------------------------------------- #
# Rotas aninhadas sob /api/accounts/{account_id}/rules
# --------------------------------------------------------------------------- #
@router.get(
    "/api/accounts/{account_id}/rules",
    response_model=list[RuleRead],
    summary="Lista regras de auto-resposta da conta.",
)
async def list_rules(
    account: InstagramAccount = Depends(require_account_owned),
    session: AsyncSession = Depends(db_session),
) -> list[RuleRead]:
    repo = AutoReplyRuleRepository(session)
    rows = await repo.list_for_account(account.id)
    return [RuleRead.model_validate(r) for r in rows]


@router.post(
    "/api/accounts/{account_id}/rules",
    response_model=RuleRead,
    status_code=status.HTTP_201_CREATED,
    summary="Cria uma nova regra.",
)
async def create_rule(
    body: RuleCreate,
    account: InstagramAccount = Depends(require_account_owned),
    session: AsyncSession = Depends(db_session),
) -> RuleRead:
    repo = AutoReplyRuleRepository(session)
    rule = AutoReplyRule(
        instagram_account_id=account.id,
        trigger_word=body.trigger_word.strip(),
        message_template=body.message_template,
        priority=body.priority,
        is_active=body.is_active,
    )
    try:
        await repo.add(rule)
    except IntegrityError:
        # UniqueConstraint em (account_id, trigger_word): já existe.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="trigger_word_already_exists",
        )
    return RuleRead.model_validate(rule)


# --------------------------------------------------------------------------- #
# Rotas por ID da regra
# --------------------------------------------------------------------------- #
@router.patch(
    "/api/rules/{rule_id}",
    response_model=RuleRead,
    summary="Atualiza campos da regra.",
)
async def update_rule(
    body: RuleUpdate,
    rule: AutoReplyRule = Depends(require_rule_owned),
    session: AsyncSession = Depends(db_session),
) -> RuleRead:
    if body.trigger_word is not None:
        rule.trigger_word = body.trigger_word.strip()
    if body.message_template is not None:
        rule.message_template = body.message_template
    if body.priority is not None:
        rule.priority = body.priority
    if body.is_active is not None:
        rule.is_active = body.is_active

    try:
        await session.flush()
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="trigger_word_already_exists",
        )
    return RuleRead.model_validate(rule)


@router.delete(
    "/api/rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a regra.",
)
async def delete_rule(
    rule: AutoReplyRule = Depends(require_rule_owned),
    session: AsyncSession = Depends(db_session),
) -> None:
    repo = AutoReplyRuleRepository(session)
    await repo.delete(rule)
