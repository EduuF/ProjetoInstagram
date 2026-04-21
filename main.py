"""Entrypoint FastAPI.

Tudo que é composição de componentes (lifespan, routers) fica aqui. A
lógica propriamente dita está em ``app/``.

Execução::

    uvicorn main:app --reload --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.accounts import router as accounts_router
from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.oauth import router as oauth_router
from app.api.overview import router as overview_router
from app.api.rules import router as rules_router
from app.api.webhook import router as webhook_router
from app.config import get_settings
from app.db.session import dispose_engine, init_db
from app.logging_config import configure_logging
from app.services.instagram_client import (
    shutdown_instagram_client,
    startup_instagram_client,
)
from app.services.token_refresher import (
    start_token_refresher,
    stop_token_refresher,
)

configure_logging()
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Inicializa recursos de I/O compartilhados no processo.

    - Se ``DB_AUTO_CREATE=true``, cria tabelas (dev).
    - Inicializa o :class:`httpx.AsyncClient` singleton do Instagram.
    - Inicia o job em background que renova tokens de acesso.
    - No shutdown, cancela o refresher, fecha pool HTTP e pool do banco.
    """
    # Log sem credenciais: a DATABASE_URL pode conter senha.
    logger.info(
        "Iniciando aplicação | environment=%s | is_production=%s",
        settings.environment,
        settings.is_production,
    )

    if settings.db_auto_create and not settings.is_production:
        # Guardrail duplo: ``DB_AUTO_CREATE`` só é honrado fora de prod.
        # Em prod o schema é gerenciado por Alembic (rodado no entrypoint).
        logger.info("DB_AUTO_CREATE=true -> criando tabelas (modo DEV).")
        await init_db()
    elif settings.db_auto_create and settings.is_production:
        logger.warning(
            "DB_AUTO_CREATE=true ignorado em produção. Use Alembic."
        )

    await startup_instagram_client(settings)
    logger.info("Instagram HTTP client inicializado.")

    await start_token_refresher()

    try:
        yield
    finally:
        logger.info("Encerrando aplicação.")
        await stop_token_refresher()
        await shutdown_instagram_client()
        await dispose_engine()


app = FastAPI(
    title="Instagram Auto-Reply (multi-tenant)",
    description=(
        "Recebe webhooks de comentários do Instagram (várias contas de "
        "diversos clientes) e dispara Private Replies conforme as regras "
        "configuradas por cada cliente no banco."
    ),
    version="0.4.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
# Em dev o frontend Next.js vai rodar em http://localhost:3000. Usamos
# ``allow_credentials=True`` porque o login seta cookie HttpOnly e o browser
# só envia o cookie em requests cross-origin quando esta flag está ativa.
# Observe que com ``allow_credentials=True`` NÃO é permitido usar ``*`` em
# ``allow_origins``; por isso a origem é explícita. Em produção, essa
# necessidade some se usarmos o rewrite do Next.js (same-origin), mas
# deixar o CORS configurado não atrapalha.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# Ordem de include_router não importa para o roteamento em si (FastAPI
# resolve pelo path), mas agrupar por "domínio" facilita na hora de ler
# a doc interativa em /docs.
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(oauth_router)
app.include_router(accounts_router)
app.include_router(overview_router)
app.include_router(rules_router)
app.include_router(webhook_router)
