"""Orquestrador que processa um payload de webhook end-to-end.

Fluxo (por ``entry[]`` → por ``changes[] com field='comments'``):

    1.  Busca a conta IG (tenant) pelo ``entry.id``. Se não houver, pula.
    2.  Tenta criar ``CommentEvent`` (INSERT idempotente por ``comment_id``).
        Se já existia, grava ``AutoReplySent.SKIPPED_DUPLICATE`` e pula.
    3.  Lista regras ativas da conta (ordenadas por priority).
    4.  Procura regra cujo trigger case com o texto do comentário.
        Se nenhuma casar, grava ``SKIPPED_NO_RULE`` e pula.
    5.  Renderiza a mensagem e chama ``InstagramClient.send_private_reply``
        usando o ``access_token`` da conta (multi-tenant).
    6.  Grava ``AutoReplySent`` com status ``SENT`` ou ``FAILED`` + detalhes.

O método principal ``process_payload`` é *exception-safe*: erros em uma
entrada são logados mas não abortam as demais entradas do payload.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (
    AutoReplyRule,
    AutoReplySent,
    AutoReplyStatus,
    CommentEvent,
    InstagramAccount,
)
from ..repositories.auto_reply_rule import AutoReplyRuleRepository
from ..repositories.auto_reply_sent import AutoReplySentRepository
from ..repositories.comment_event import CommentEventRepository
from ..repositories.instagram_account import InstagramAccountRepository
from ..schemas.webhook import CommentValue, WebhookPayload
from .auto_reply_engine import find_matching_rule, render_message
from .instagram_client import InstagramAPIError, InstagramClient

logger = logging.getLogger(__name__)


class WebhookService:
    """Aplica a lógica de auto-resposta a um payload recebido da Meta.

    Uma instância por processamento (stateless). A sessão do banco é
    passada no construtor; o ciclo commit/rollback é de quem a criou
    (normalmente o ``BackgroundTasks`` via ``AsyncSessionLocal``).
    """

    def __init__(
        self,
        session: AsyncSession,
        instagram_client: InstagramClient,
    ) -> None:
        self._session = session
        self._ig = instagram_client

        # Repositórios instanciados sobre a mesma sessão → transação única.
        self._accounts = InstagramAccountRepository(session)
        self._rules = AutoReplyRuleRepository(session)
        self._comments = CommentEventRepository(session)
        self._sent = AutoReplySentRepository(session)

    # ------------------------------------------------------------------ #
    # Entry-point
    # ------------------------------------------------------------------ #
    async def process_payload(self, payload: WebhookPayload) -> None:
        """Processa um payload parseado. Nunca levanta."""
        if payload.object != "instagram":
            logger.debug("Payload ignorado (object=%r)", payload.object)
            return

        for entry in payload.entry:
            try:
                await self._process_entry(entry_id=entry.id, changes=entry.changes or [])
            except Exception:
                logger.exception("Erro processando entry %s", entry.id)

    # ------------------------------------------------------------------ #
    # Por entry (= por conta/tenant)
    # ------------------------------------------------------------------ #
    async def _process_entry(self, *, entry_id: str, changes: list) -> None:
        account = await self._accounts.get_by_ig_business_account_id(entry_id)
        if account is None:
            logger.warning(
                "Webhook para conta IG não cadastrada (entry.id=%s). Ignorando.",
                entry_id,
            )
            return

        for change in changes:
            if change.field != "comments":
                logger.debug("Ignorando field='%s' (conta=%s)", change.field, account.id)
                continue
            await self._process_comment(account=account, value=change.value)

    # ------------------------------------------------------------------ #
    # Um comentário
    # ------------------------------------------------------------------ #
    async def _process_comment(
        self,
        *,
        account: InstagramAccount,
        value: dict,
    ) -> None:
        try:
            comment = CommentValue.model_validate(value)
        except Exception:
            logger.exception("Value inválido para field='comments': %s", value)
            return

        # 1) Persiste o evento (idempotência por UNIQUE comment_id).
        event_row = CommentEvent(
            instagram_account_id=account.id,
            comment_id=comment.id,
            media_id=(comment.media.id if comment.media else None),
            parent_id=comment.parent_id,
            commenter_igsid=(comment.author.id if comment.author else None),
            commenter_username=(
                comment.author.username if comment.author else None
            ),
            text=comment.text,
        )
        event, created = await self._comments.create_if_absent(event_row)

        logger.info(
            "Comentário recebido | account=%s comment_id=%s author=%s text=%r novo=%s",
            account.ig_business_account_id,
            comment.id,
            comment.author.username if comment.author else None,
            comment.text,
            created,
        )

        if not created:
            # Reentrega: já processamos antes. Grava log informativo e pula.
            await self._sent.add(
                AutoReplySent(
                    comment_event_id=event.id,
                    status=AutoReplyStatus.SKIPPED_DUPLICATE,
                    rendered_text=None,
                )
            )
            return

        # 2) Busca regras ativas (já ordenadas por priority) e procura match.
        rules = await self._rules.list_active_for_account(account.id)
        rule = find_matching_rule(rules, comment.text)

        if rule is None:
            logger.info("Nenhuma regra casou (comment_id=%s)", comment.id)
            await self._sent.add(
                AutoReplySent(
                    comment_event_id=event.id,
                    status=AutoReplyStatus.SKIPPED_NO_RULE,
                    rendered_text=None,
                )
            )
            return

        # 3) Envia a Private Reply.
        message_text = render_message(
            rule.message_template,
            username=(comment.author.username if comment.author else None),
        )
        await self._send_private_reply(
            account=account,
            event=event,
            rule=rule,
            message_text=message_text,
        )

    # ------------------------------------------------------------------ #
    # Envio efetivo
    # ------------------------------------------------------------------ #
    async def _send_private_reply(
        self,
        *,
        account: InstagramAccount,
        event: CommentEvent,
        rule: AutoReplyRule,
        message_text: str,
    ) -> None:
        try:
            result = await self._ig.send_private_reply(
                access_token=account.access_token,
                comment_id=event.comment_id,
                text=message_text,
            )
        except InstagramAPIError as exc:
            logger.error(
                "Falha na Private Reply | account=%s comment=%s err=%s",
                account.id,
                event.comment_id,
                exc,
            )
            await self._sent.add(
                AutoReplySent(
                    comment_event_id=event.id,
                    rule_id=rule.id,
                    status=AutoReplyStatus.FAILED,
                    rendered_text=message_text,
                    error_code=exc.code,
                    error_subcode=exc.subcode,
                    error_message=exc.message,
                )
            )
            return
        except Exception as exc:
            logger.exception(
                "Erro inesperado enviando Private Reply | account=%s comment=%s",
                account.id,
                event.comment_id,
            )
            await self._sent.add(
                AutoReplySent(
                    comment_event_id=event.id,
                    rule_id=rule.id,
                    status=AutoReplyStatus.FAILED,
                    rendered_text=message_text,
                    error_message=str(exc),
                )
            )
            return

        await self._sent.add(
            AutoReplySent(
                comment_event_id=event.id,
                rule_id=rule.id,
                status=AutoReplyStatus.SENT,
                rendered_text=message_text,
                recipient_igsid=result.get("recipient_id"),
                message_id=result.get("message_id"),
            )
        )
