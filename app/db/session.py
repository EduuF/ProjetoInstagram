"""Engine e sessão assíncronos do SQLAlchemy.

Padrão "uma sessão por request", injetada via :func:`get_session`
(veja ``app.api.deps``). A engine é singleton do processo.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config import get_settings
from .base import Base

logger = logging.getLogger(__name__)

_settings = get_settings()


def _build_engine_kwargs(url: str) -> dict:
    """Parâmetros da engine ajustados ao backend (SQLite ou Postgres).

    SQLite aceita pool muito pequeno (geralmente 1 conn) e não tem ``max_overflow``
    configurável do mesmo jeito; já o Postgres aproveita de um pool decente
    (``pool_size=10`` + ``max_overflow=20``) para atender vários workers.
    """
    kwargs: dict = dict(echo=False, future=True, pool_pre_ping=True)
    if url.startswith("postgresql") or url.startswith("postgres"):
        kwargs.update(pool_size=10, max_overflow=20, pool_recycle=1800)
    return kwargs


# ``echo=False`` em produção. Se quiser ver SQL no console, troque para True.
engine: AsyncEngine = create_async_engine(
    _settings.database_url,
    **_build_engine_kwargs(_settings.database_url),
)

# ``expire_on_commit=False`` evita lazy-reloads após commit, que em async
# disparariam I/O implícito (e erros) fora do contexto de await.
AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Dependency FastAPI: abre uma sessão por request, commita e fecha.

    Uso::

        @app.get(...)
        async def rota(session: AsyncSession = Depends(get_session)):
            ...

    Em caso de exceção, faz rollback.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Cria todas as tabelas se não existirem (apenas para DEV).

    Em produção, isto NÃO deve ser chamado — usaremos Alembic para
    versionar o schema.

    Também roda um pequeno conjunto de "ALTER TABLE" idempotentes para
    corrigir colunas cujo tamanho mudou entre versões do modelo. Isso só
    existe pra facilitar o fluxo de desenvolvimento (``DB_AUTO_CREATE=true``),
    já que ``create_all`` **não altera** colunas de tabelas existentes.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _apply_dev_column_fixes(conn)


async def _apply_dev_column_fixes(conn) -> None:
    """Idempotent ALTERs para alinhar colunas ao schema atual do modelo.

    Só roda em Postgres (o driver SQLite ignora alguns ALTERs). Cada ALTER
    é inofensivo se a coluna já estiver no tipo correto — usamos
    ``ALTER TABLE ... ALTER COLUMN ... TYPE ...`` com ``USING`` explícito.

    Por que isso existe:
        O ``message_id`` do /me/messages da Meta pode passar de 180 chars
        (era ``VARCHAR(128)``). O INSERT falhava com StringDataRightTruncation
        e isso abortava a transação inteira do webhook (perdendo o
        CommentEvent e zerando as estatísticas da dashboard).
    """
    dialect = conn.dialect.name
    if dialect != "postgresql":
        return

    fixes = [
        (
            "auto_replies_sent.message_id -> TEXT",
            """
            ALTER TABLE auto_replies_sent
            ALTER COLUMN message_id TYPE TEXT
            USING message_id::TEXT
            """,
        ),
    ]
    for label, sql in fixes:
        try:
            await conn.execute(text(sql))
            logger.info("init_db: aplicou fix de coluna | %s", label)
        except Exception:
            logger.exception("init_db: falhou ao aplicar fix | %s", label)


async def dispose_engine() -> None:
    """Fecha o pool de conexões (chamado no shutdown da aplicação)."""
    await engine.dispose()
