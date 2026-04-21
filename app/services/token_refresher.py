"""Job em background: renova tokens IG próximos do vencimento.

Motivação:
    Tokens do Instagram Business Login vivem 60 dias. Se o cliente não
    abrir nosso dashboard nesse período, o token venceria e a
    ferramenta pararia de responder comentários silenciosamente. Esse
    loop procura contas ativas cujo ``token_expires_at`` está a menos
    de N dias e chama o refresh.

Design:
    - É uma **task asyncio** iniciada no ``lifespan`` do FastAPI.
    - Usa uma sessão dedicada por ciclo (``AsyncSessionLocal``) para não
      interferir com requests HTTP.
    - Catches amplos: qualquer exceção em um ciclo é logada e o loop
      continua dormindo. Falhar silenciosamente aqui é pior que logar.
    - Pequeno jitter para evitar que várias instâncias (multi-worker)
      executem exatamente no mesmo instante.

Limitações conhecidas:
    - Em um cluster multi-worker, **todas** as instâncias rodam o job.
      Para volume baixo isso é aceitável (chamadas idempotentes; a pior
      consequência é uma requisição de refresh extra). Para produção
      grande, colocar em um worker dedicado (ex.: ECS scheduled task).
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db.session import AsyncSessionLocal
from ..repositories.instagram_account import InstagramAccountRepository
from .instagram_client import get_http_client
from .oauth_instagram import OAuthInstagramService

logger = logging.getLogger(__name__)

_task: asyncio.Task[None] | None = None


async def _refresh_one(
    session: AsyncSession,
    oauth: OAuthInstagramService,
    account_id: str,
) -> None:
    """Faz o refresh de uma única conta dentro de sua própria transação."""
    repo = InstagramAccountRepository(session)
    # Re-busca a conta aqui para ter o objeto dentro DESTA sessão.
    account = await repo.get_by_ig_business_account_id(
        await _resolve_bizid(session, account_id),
        only_active=True,
    )
    if account is None:
        return
    try:
        new_token = await oauth.refresh(long_lived_token=account.access_token)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Falha ao renovar token | account_id=%s ig_user_id=%s",
            account.id,
            account.ig_user_id,
        )
        # Não desativamos aqui — pode ser problema transitório. Se o token
        # realmente vencer, o próximo envio vai falhar com 401 e logamos lá.
        return

    await repo.update_token(
        account,
        access_token=new_token["access_token"],
        token_expires_at=new_token["expires_at"],
    )
    logger.info(
        "Token renovado | account_id=%s novo_exp=%s",
        account.id,
        new_token["expires_at"],
    )


async def _resolve_bizid(session: AsyncSession, account_id: str) -> str:
    """Dado nosso PK de conta, devolve o ``ig_business_account_id`` dela."""
    from ..db.models import InstagramAccount
    from sqlalchemy import select

    result = await session.execute(
        select(InstagramAccount.ig_business_account_id).where(
            InstagramAccount.id == account_id
        )
    )
    return result.scalar_one()


async def _one_cycle() -> None:
    """Executa um ciclo de refresh."""
    settings = get_settings()
    before = datetime.now(timezone.utc) + timedelta(
        days=settings.token_refresh_before_days
    )
    oauth = OAuthInstagramService(get_http_client(), settings)

    async with AsyncSessionLocal() as session:
        repo = InstagramAccountRepository(session)
        # Coletamos primeiro só os IDs, e depois renovamos um por um em
        # mini-transações — isolando falhas e não segurando lock longo.
        accounts = list(await repo.list_needing_refresh(before=before))
        ids = [a.id for a in accounts]

    if not ids:
        logger.debug("Refresher: nenhuma conta precisa renovar")
        return

    logger.info("Refresher: renovando %d conta(s)", len(ids))
    for account_id in ids:
        async with AsyncSessionLocal() as session:
            try:
                await _refresh_one(session, oauth, account_id)
                await session.commit()
            except Exception:  # noqa: BLE001
                logger.exception("Refresher: erro ao renovar %s", account_id)
                await session.rollback()


async def _loop() -> None:
    """Loop infinito do refresher."""
    settings = get_settings()
    interval_s = settings.token_refresher_interval_minutes * 60

    # Jitter inicial (0–60s) para multi-worker não sincronizar.
    await asyncio.sleep(random.uniform(0, 60))

    while True:
        try:
            await _one_cycle()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Refresher: erro inesperado no ciclo")
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            raise


async def start_token_refresher() -> None:
    """Inicia a task do refresher (idempotente)."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop(), name="token_refresher")
    logger.info("Token refresher iniciado")


async def stop_token_refresher() -> None:
    """Cancela a task do refresher, se estiver rodando."""
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    _task = None
    logger.info("Token refresher parado")
