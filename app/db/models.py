"""Modelos SQLAlchemy (ORM) da aplicação.

Visão geral do schema (multi-tenant):

::

    clients                       (cliente = usuário do nosso SaaS)
      └── instagram_accounts      (contas IG conectadas pelo cliente)
            ├── auto_reply_rules  (regras palavra-chave -> mensagem)
            ├── comment_events    (log de TODO comentário recebido)
            └── auto_replies_sent (log de TODA DM enviada — OK ou falha)

Decisões importantes:

- PKs são ``str`` (UUID4) — funcionam sem extensão no SQLite e no Postgres.
- ``comment_events.comment_id`` é ``UNIQUE`` → idempotência natural.
- Todos os logs têm timestamp timezone-aware (UTC).
- ``ondelete="CASCADE"`` garante que apagar um cliente apaga tudo dele.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import List, Optional
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, utcnow
from .types import EncryptedString


def _uuid() -> str:
    """Gera um UUID4 em string (formato canônico)."""
    return str(uuid4())


# =============================================================================
# Clients — os usuários do nosso produto (quem assinou o SaaS)
# =============================================================================
class Client(Base, TimestampMixin):
    """Um cliente/empresa que usa nossa plataforma de auto-resposta.

    No futuro, quando o frontend existir, este é o cadastro que o
    usuário cria ao logar. Por isso já tem ``email`` e ``password_hash``.
    """

    __tablename__ = "clients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Campo preparado para o fluxo de autenticação do frontend. Fica
    # nullable por enquanto (pode haver cliente criado via seed/admin).
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    instagram_accounts: Mapped[List["InstagramAccount"]] = relationship(
        back_populates="client",
        cascade="all, delete-orphan",
    )


# =============================================================================
# InstagramAccount — a conta IG profissional conectada pelo cliente
# =============================================================================
class InstagramAccount(Base, TimestampMixin):
    """Uma conta profissional do Instagram conectada por um cliente.

    Um cliente pode conectar várias contas (ex.: agências com N contas).

    O campo ``ig_business_account_id`` é nossa **chave de tenant para o
    webhook**: é o valor que a Meta envia em ``entry[].id``.
    """

    __tablename__ = "instagram_accounts"
    __table_args__ = (
        Index("ix_ig_accounts_client_active", "client_id", "is_active"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    client_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ID numérico da conta profissional do Instagram (``entry[].id`` no webhook).
    ig_business_account_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )

    # ID App-Scoped retornado por ``/me`` (pode ser útil em outros endpoints).
    ig_user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Criptografado em repouso (Fernet). No código aparece como ``str`` normal;
    # no banco, só ciphertext. Ver ``app/db/types.py::EncryptedString``.
    access_token: Mapped[str] = mapped_column(EncryptedString, nullable=False)

    token_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_refreshed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    client: Mapped[Client] = relationship(back_populates="instagram_accounts")
    auto_reply_rules: Mapped[List["AutoReplyRule"]] = relationship(
        back_populates="instagram_account",
        cascade="all, delete-orphan",
    )
    comment_events: Mapped[List["CommentEvent"]] = relationship(
        back_populates="instagram_account",
        cascade="all, delete-orphan",
    )


# =============================================================================
# AutoReplyRule — regra palavra-chave -> mensagem (por conta IG)
# =============================================================================
class AutoReplyRule(Base, TimestampMixin):
    """Regra configurada pelo cliente: quando alguém comenta ``X``, mandar ``Y``."""

    __tablename__ = "auto_reply_rules"
    __table_args__ = (
        # Impede duas regras com mesma palavra-chave na mesma conta.
        UniqueConstraint(
            "instagram_account_id",
            "trigger_word",
            name="uq_auto_reply_rules_account_trigger",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    instagram_account_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("instagram_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Palavra-chave (case-insensitive, match por palavra inteira).
    trigger_word: Mapped[str] = mapped_column(String(100), nullable=False)

    # Template da mensagem do DM. Suporta placeholders ``{username}``.
    message_template: Mapped[str] = mapped_column(Text, nullable=False)

    # Ordem de avaliação (menor = mais prioritário). Útil quando duas
    # regras poderiam casar (ex.: "livro" e "livro grátis").
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    instagram_account: Mapped[InstagramAccount] = relationship(
        back_populates="auto_reply_rules"
    )


# =============================================================================
# CommentEvent — log de comentários recebidos via webhook
# =============================================================================
class CommentEvent(Base):
    """Um comentário recebido em uma conta IG (qualquer coment., não só os que casam regra).

    Serve para:

    - **idempotência**: ``comment_id`` é UNIQUE; se a Meta reentregar o
      webhook, o INSERT falha e a gente pula.
    - **histórico/estatística**: base do dashboard futuro (quantos
      comentários por dia, taxa de conversão de regra, etc.).
    """

    __tablename__ = "comment_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    instagram_account_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("instagram_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ID do comentário no Instagram (``value.id`` no webhook). UNIQUE → idempotência.
    comment_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )

    media_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    parent_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    commenter_igsid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    commenter_username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    instagram_account: Mapped[InstagramAccount] = relationship(
        back_populates="comment_events"
    )
    replies_sent: Mapped[List["AutoReplySent"]] = relationship(
        back_populates="comment_event",
        cascade="all, delete-orphan",
    )


# =============================================================================
# AutoReplySent — log de DMs enviadas (ou tentadas)
# =============================================================================
class AutoReplyStatus(str, enum.Enum):
    """Resultado de uma tentativa de resposta automática."""

    SENT = "sent"
    FAILED = "failed"
    SKIPPED_NO_RULE = "skipped_no_rule"
    SKIPPED_DUPLICATE = "skipped_duplicate"


class AutoReplySent(Base):
    """Log de cada tentativa de envio de Private Reply."""

    __tablename__ = "auto_replies_sent"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    comment_event_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("comment_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ``rule_id`` pode ser null quando o status for ``SKIPPED_NO_RULE``.
    rule_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("auto_reply_rules.id", ondelete="SET NULL"),
        nullable=True,
    )

    status: Mapped[AutoReplyStatus] = mapped_column(
        SAEnum(AutoReplyStatus, name="auto_reply_status"),
        nullable=False,
    )

    recipient_igsid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # ATENÇÃO: o ``message_id`` retornado pela Meta em /me/messages é um blob
    # base64 que pode passar de 180 caracteres (e não há limite documentado).
    # Usamos Text para não ter nenhum risco de StringDataRightTruncationError,
    # que derruba a transação inteira e faz perder o CommentEvent também.
    message_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rendered_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Detalhes de erro quando ``status == FAILED``.
    error_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_subcode: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    comment_event: Mapped[CommentEvent] = relationship(back_populates="replies_sent")


# =============================================================================
# OAuthState — tokens CSRF one-shot para o fluxo OAuth do Instagram
# =============================================================================
class OAuthState(Base):
    """Nonce persistido por ``/auth/instagram/start``, consumido no callback.

    Por que persistir?
        - Assim sobrevivemos a restart do processo.
        - Funciona multi-worker (N processos atrás de um ALB compartilham o DB).
        - Facilita auditoria (sabemos quando o cliente iniciou o fluxo).

    Garantias:
        - ``state`` é UNIQUE (gerado com ``secrets.token_urlsafe(32)``).
        - ``expires_at`` é checado no consumo.
        - ``consumed_at`` marca uso único: se alguém replayar o callback
          com o mesmo state, recusamos.
    """

    __tablename__ = "oauth_states"
    __table_args__ = (
        Index("ix_oauth_states_client_expires", "client_id", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)

    # Cliente que iniciou o fluxo (extraído do JWT no /auth/instagram/start).
    client_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Valor enviado à Meta no parâmetro ``state`` da URL de authorize. UNIQUE.
    state: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )

    # Para onde redirecionar o usuário depois do callback (frontend).
    # Opcional: se None, vai para uma página default do backend.
    redirect_after: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
