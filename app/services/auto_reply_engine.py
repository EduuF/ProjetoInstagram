"""Motor puro de matching palavra-chave -> regra.

Essa camada **não** tem I/O. Recebe as regras já carregadas (do banco) e
responde "qual regra casa com este texto?". Mantê-la sem efeitos colaterais
é o que permite testá-la trivialmente.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, Optional

from ..db.models import AutoReplyRule

logger = logging.getLogger(__name__)


def _matches(trigger_word: str, text: str) -> bool:
    """Match *case-insensitive* e por **palavra inteira** (``\\b``).

    Ex.: trigger "quero" casa com "Quero o link" mas NÃO com "querosene".
    """
    if not text or not trigger_word:
        return False
    pattern = rf"\b{re.escape(trigger_word)}\b"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def render_message(template: str, *, username: Optional[str] = None) -> str:
    """Formata o template com os placeholders suportados."""
    # ``.format`` pode quebrar se o template não tiver o placeholder, mas o
    # nosso ``format_map`` com dicionário default evita KeyError.
    class _Defaulting(dict):
        def __missing__(self, key: str) -> str:  # noqa: D401
            return "{" + key + "}"

    return template.format_map(
        _Defaulting(username=username or "")
    )


def find_matching_rule(
    rules: Iterable[AutoReplyRule],
    text: Optional[str],
) -> Optional[AutoReplyRule]:
    """Retorna a primeira regra cujo ``trigger_word`` casa com ``text``.

    As ``rules`` devem vir **já ordenadas por prioridade** do repositório.
    """
    if not text:
        return None
    for rule in rules:
        if _matches(rule.trigger_word, text):
            return rule
    return None
