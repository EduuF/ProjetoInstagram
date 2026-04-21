"""Cria (ou atualiza) um cliente + conta IG + uma regra de auto-resposta.

Executar do diretório ``ProjetoInstagram``::

    python -m app.scripts.seed

Este script usa os IDs e tokens que estão no ``.env`` (provavelmente a
sua conta de testes), para você poder continuar validando o fluxo com
webhooks reais enquanto desenvolve. É **idempotente** — rodar várias
vezes não duplica nada.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import select

from ..db.models import AutoReplyRule, Client, InstagramAccount
from ..db.session import AsyncSessionLocal, dispose_engine, init_db
from ..logging_config import configure_logging
from ..security.passwords import hash_password

# Carrega variáveis do .env antes de qualquer os.getenv abaixo.
# ``get_settings()`` do app usa pydantic-settings (que lê o mesmo arquivo),
# mas neste script lemos algumas variáveis "brutas" (os IDs que você colou
# no .env para testar), então o load_dotenv explícito facilita.
_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH)

configure_logging()
logger = logging.getLogger(__name__)


# ---- Valores de semente (podem ser ajustados ou movidos para o .env) -------
SEED_CLIENT_EMAIL = os.getenv("SEED_CLIENT_EMAIL", "teste@projetoinstagram.local")
SEED_CLIENT_NAME = os.getenv("SEED_CLIENT_NAME", "Cliente de Teste")
SEED_CLIENT_PASSWORD = os.getenv("SEED_CLIENT_PASSWORD", "SenhaDev123!")
SEED_TRIGGER_WORD = os.getenv("SEED_TRIGGER_WORD", "quero")
SEED_MESSAGE_TEMPLATE = os.getenv(
    "SEED_MESSAGE_TEMPLATE",
    "Oi {username}! Aqui está o link do manual como prometido: "
    "https://exemplo.com/manual.pdf",
)


async def seed() -> None:
    """Popula o banco com dados mínimos para testar o webhook."""
    # Garante que as tabelas existem (caso o server ainda não tenha rodado).
    await init_db()

    ig_business_account_id = os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID")
    access_token = os.getenv("INSTAGRAM_LONG_ACCESS_TOKEN")

    if not ig_business_account_id or not access_token:
        raise SystemExit(
            "Defina INSTAGRAM_BUSINESS_ACCOUNT_ID e INSTAGRAM_LONG_ACCESS_TOKEN no .env."
        )

    async with AsyncSessionLocal() as session:
        # -------- Client --------
        client = await _get_or_create_client(session)

        # -------- InstagramAccount --------
        account = await _get_or_create_account(
            session,
            client_id=client.id,
            ig_business_account_id=ig_business_account_id,
            access_token=access_token,
        )

        # -------- AutoReplyRule --------
        await _get_or_create_rule(
            session,
            instagram_account_id=account.id,
            trigger_word=SEED_TRIGGER_WORD,
            message_template=SEED_MESSAGE_TEMPLATE,
        )

        await session.commit()

    # Fecha o pool de conexões para o processo encerrar limpo.
    await dispose_engine()
    logger.info("Seed concluído.")


async def _get_or_create_client(session) -> Client:
    stmt = select(Client).where(Client.email == SEED_CLIENT_EMAIL)
    existing: Optional[Client] = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        # Garante que o cliente de seed sempre tenha uma senha utilizável
        # (para você poder logar via /auth/login mesmo depois de rodar o
        # seed antes de termos passwords).
        if not existing.password_hash:
            existing.password_hash = hash_password(SEED_CLIENT_PASSWORD)
            logger.info("Client já existe, definindo password_hash (email=%s)", SEED_CLIENT_EMAIL)
        else:
            logger.info("Client já existe (email=%s)", SEED_CLIENT_EMAIL)
        return existing

    client = Client(
        email=SEED_CLIENT_EMAIL,
        name=SEED_CLIENT_NAME,
        password_hash=hash_password(SEED_CLIENT_PASSWORD),
        is_active=True,
    )
    session.add(client)
    await session.flush()
    logger.info(
        "Client criado | id=%s email=%s (senha dev: %s)",
        client.id, client.email, SEED_CLIENT_PASSWORD,
    )
    return client


async def _get_or_create_account(
    session,
    *,
    client_id: str,
    ig_business_account_id: str,
    access_token: str,
) -> InstagramAccount:
    stmt = select(InstagramAccount).where(
        InstagramAccount.ig_business_account_id == ig_business_account_id
    )
    existing: Optional[InstagramAccount] = (
        await session.execute(stmt)
    ).scalar_one_or_none()

    if existing is not None:
        # Atualiza o token caso tenha mudado (rotação de token).
        if existing.access_token != access_token:
            logger.info("Atualizando access_token da conta %s", existing.id)
            existing.access_token = access_token
        return existing

    account = InstagramAccount(
        client_id=client_id,
        ig_business_account_id=ig_business_account_id,
        access_token=access_token,
        is_active=True,
    )
    session.add(account)
    await session.flush()
    logger.info(
        "InstagramAccount criada | id=%s ig_id=%s",
        account.id,
        account.ig_business_account_id,
    )
    return account


async def _get_or_create_rule(
    session,
    *,
    instagram_account_id: str,
    trigger_word: str,
    message_template: str,
) -> AutoReplyRule:
    stmt = (
        select(AutoReplyRule)
        .where(AutoReplyRule.instagram_account_id == instagram_account_id)
        .where(AutoReplyRule.trigger_word == trigger_word)
    )
    existing: Optional[AutoReplyRule] = (
        await session.execute(stmt)
    ).scalar_one_or_none()

    if existing is not None:
        # Atualiza o template/ativação caso tenham mudado.
        existing.message_template = message_template
        existing.is_active = True
        logger.info("AutoReplyRule existente atualizada | trigger=%s", trigger_word)
        return existing

    rule = AutoReplyRule(
        instagram_account_id=instagram_account_id,
        trigger_word=trigger_word,
        message_template=message_template,
        priority=100,
        is_active=True,
    )
    session.add(rule)
    await session.flush()
    logger.info("AutoReplyRule criada | id=%s trigger=%s", rule.id, trigger_word)
    return rule


if __name__ == "__main__":
    asyncio.run(seed())
