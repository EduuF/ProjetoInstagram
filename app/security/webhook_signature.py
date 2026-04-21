"""Validação do header ``X-Hub-Signature-256`` enviado pela Meta.

A Meta assina cada POST de webhook com::

    X-Hub-Signature-256: sha256=<HEX(HMAC_SHA256(APP_SECRET, request_body))>

Sem validar essa assinatura, qualquer terceiro que descubra a URL do
webhook pode disparar nossa aplicação. É obrigatório em produção.

Detalhes importantes:

- O HMAC é computado sobre o **corpo cru** (bytes) da requisição, não
  sobre o JSON re-serializado. Por isso o endpoint precisa ler o body
  como ``bytes`` antes de fazer qualquer parsing.
- Usamos :func:`hmac.compare_digest` para comparação *timing-safe*
  (evita ataques side-channel).
"""

from __future__ import annotations

import hashlib
import hmac
import logging

logger = logging.getLogger(__name__)


_SIGNATURE_PREFIX = "sha256="


def verify_signature(*, app_secret: str, body: bytes, signature_header: str | None) -> bool:
    """Valida o header ``X-Hub-Signature-256``.

    Args:
        app_secret: O App Secret do app Meta (``INSTAGRAM_APP_SECRET``).
        body: Corpo cru (bytes) recebido na requisição.
        signature_header: Valor do header ``X-Hub-Signature-256`` (pode ser ``None``).

    Returns:
        ``True`` se a assinatura é válida; ``False`` caso contrário.
    """
    if not signature_header or not signature_header.startswith(_SIGNATURE_PREFIX):
        logger.warning("Header X-Hub-Signature-256 ausente ou malformado.")
        return False

    provided_hex = signature_header[len(_SIGNATURE_PREFIX):].strip()
    expected_hex = hmac.new(
        key=app_secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    is_valid = hmac.compare_digest(provided_hex, expected_hex)
    if not is_valid:
        logger.warning("Assinatura HMAC inválida no webhook.")
    return is_valid
