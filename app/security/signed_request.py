"""Validação do ``signed_request`` enviado pela Meta nos callbacks de
**Deauthorize** e **Data Deletion**.

Documentação oficial:
https://developers.facebook.com/docs/facebook-login/guides/advanced/oidc-token

Formato do parâmetro ``signed_request`` (POST form-urlencoded):

    <base64url(signature)>.<base64url(payload_json)>

Onde:
    - ``signature`` = HMAC-SHA256(app_secret, payload_b64) — bytes brutos.
    - ``payload_json`` é um JSON com pelo menos::

        {
            "algorithm": "HMAC-SHA256",
            "issued_at": 1710000000,
            "user_id": "..."
        }

Importante:
    - A base64 usada é "urlsafe **sem padding**" (pode não ter ``=`` no
      final). Precisamos re-adicionar padding antes de decodificar.
    - Comparação da assinatura deve usar ``hmac.compare_digest`` para evitar
      timing attacks.
    - Verificamos também ``algorithm == HMAC-SHA256`` para bloquear
      tentativa de downgrade caso a Meta (teoricamente) suporte outros.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any


class InvalidSignedRequestError(ValueError):
    """signed_request malformado, assinatura errada ou algoritmo inesperado."""


def _b64url_decode(data: str) -> bytes:
    """Decodifica base64 urlsafe, adicionando padding se necessário."""
    # Meta remove ``=`` de padding; base64 exige que len % 4 == 0.
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def parse_signed_request(signed_request: str, app_secret: str) -> dict[str, Any]:
    """Valida a assinatura HMAC e retorna o payload decodificado."""
    if not signed_request or "." not in signed_request:
        raise InvalidSignedRequestError("formato inválido (esperado 'sig.payload')")

    encoded_sig, payload_b64 = signed_request.split(".", 1)

    try:
        sig = _b64url_decode(encoded_sig)
        payload_bytes = _b64url_decode(payload_b64)
    except Exception as exc:  # noqa: BLE001
        raise InvalidSignedRequestError("base64 inválido") from exc

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise InvalidSignedRequestError("JSON inválido no payload") from exc

    if payload.get("algorithm", "").upper() != "HMAC-SHA256":
        raise InvalidSignedRequestError("algoritmo não suportado")

    expected = hmac.new(
        key=app_secret.encode("utf-8"),
        msg=payload_b64.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()

    if not hmac.compare_digest(expected, sig):
        raise InvalidSignedRequestError("assinatura inválida")

    return payload


__all__ = ["parse_signed_request", "InvalidSignedRequestError"]
