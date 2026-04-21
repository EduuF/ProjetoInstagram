"""Autorização horizontal para rotas que recebem IDs do dashboard.

O problema que este módulo resolve:
    - Rota ``GET /api/accounts/{id}/rules`` exige que o cliente autenticado
      seja dono da conta ``{id}``. Se a gente só valida "tem JWT válido",
      qualquer cliente autenticado consegue ler dados de outros passando
      IDs na URL (BOLA / Broken Object Level Authorization, OWASP #1).

    - Em vez de repetir o ``if account.client_id != current.id`` em cada
      handler e correr o risco de esquecer em uma rota nova, centralizamos
      em :func:`require_account_owned`.

Retorno do helper é o objeto ``InstagramAccount`` já carregado, o que
evita uma segunda consulta no handler.

Importante:
    - Respondemos **404** (não 403) quando o cliente tenta acessar uma
      conta que não é dele. 403 revelaria que o ID existe, o que é uma
      forma fraca de enumeração. 404 é indistinguível de "não existe".
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AutoReplyRule, Client, InstagramAccount
from ..repositories.auto_reply_rule import AutoReplyRuleRepository
from ..repositories.instagram_account import InstagramAccountRepository
from .deps import current_client, db_session


async def require_account_owned(
    account_id: str = Path(..., description="UUID da conta IG"),
    client: Client = Depends(current_client),
    session: AsyncSession = Depends(db_session),
) -> InstagramAccount:
    """Garante que a conta ``account_id`` pertence a ``current_client``.

    Use como dependency em handlers que recebem ``account_id`` no path.
    """
    repo = InstagramAccountRepository(session)
    account = await repo.get_by_id(account_id)
    if account is None or account.client_id != client.id:
        # 404 genérico: não revela existência.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account_not_found")
    return account


async def require_rule_owned(
    rule_id: str = Path(..., description="UUID da regra"),
    client: Client = Depends(current_client),
    session: AsyncSession = Depends(db_session),
) -> AutoReplyRule:
    """Garante que ``rule_id`` pertence a uma conta de ``current_client``."""
    rule_repo = AutoReplyRuleRepository(session)
    rule = await rule_repo.get_by_id(rule_id)
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="rule_not_found")

    # Uma rule pertence a uma account; uma account pertence a um client.
    # Busco a account para validar a cadeia.
    acc_repo = InstagramAccountRepository(session)
    account = await acc_repo.get_by_id(rule.instagram_account_id)
    if account is None or account.client_id != client.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="rule_not_found")
    return rule
