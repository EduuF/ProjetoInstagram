"""Declarative Base comum a todos os modelos SQLAlchemy.

Fica isolado em seu próprio módulo para evitar imports circulares entre
``models`` e ``session``: ambos importam daqui, e nenhum importa do outro.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Timestamp atual em UTC, timezone-aware.

    Preferimos gerar no Python (em vez de ``server_default=func.now()``)
    para evitar divergências de timezone entre SQLite local e Postgres.
    """
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Classe base para todos os modelos ORM da aplicação."""


class TimestampMixin:
    """Mixin com ``created_at`` e ``updated_at`` preenchidos automaticamente."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )
