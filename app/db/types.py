"""Tipos customizados do SQLAlchemy.

``EncryptedString`` é um ``TypeDecorator`` que cifra/decifra transparentemente
na hora de gravar e ler uma coluna. O resto do código trabalha com
``str`` puro; no banco fica só ciphertext Fernet (ASCII).

Usar ``TypeDecorator`` (em vez de, por exemplo, um property no modelo) tem
a vantagem de funcionar também em:
    - bulk inserts via ``insert()``/``update()``;
    - queries com ``where(col == "valor")`` (o valor é cifrado antes de
      comparar; como Fernet usa nonce aleatório, comparação por igualdade
      **não** funciona — isso é aceitável porque tokens são lidos, não
      filtrados por valor).

Cuidado conhecido:
    - Não dá para fazer ``WHERE access_token = '...'``: a busca falhará
      silenciosamente (dois ciphertexts do mesmo plaintext são diferentes).
      Isso é **intencional** — não queremos acostumar o código a consultar
      tokens por valor.
"""

from __future__ import annotations

from typing import Any, Optional

import logging

from cryptography.fernet import InvalidToken
from sqlalchemy.types import Text, TypeDecorator

from app.security.encryption import decrypt_str, encrypt_str

logger = logging.getLogger(__name__)

# Prefixo dos tokens Fernet após base64-url-safe: eles começam sempre com
# "gAAAAA" porque o primeiro byte é 0x80 (versão do token). Usamos isso para
# detectar valores herdados em plaintext e não quebrar bancos pré-migração.
_FERNET_PREFIX = "gAAAAA"


class EncryptedString(TypeDecorator):  # type: ignore[type-arg]
    """Coluna TEXT cifrada com Fernet (ASCII base64).

    Escrita é sempre cifrada. Leitura:
        - Se começa com ``gAAAAA`` tentamos Fernet.decrypt.
        - Caso contrário (legacy plaintext), retornamos o valor como está e
          logamos um WARNING uma única vez por valor — assim você vê o que
          precisa ser re-escrito mas nada quebra.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(
        self, value: Optional[str], dialect: Any
    ) -> Optional[str]:
        if value is None:
            return None
        return encrypt_str(value)

    def process_result_value(
        self, value: Optional[str], dialect: Any
    ) -> Optional[str]:
        if value is None:
            return None
        if not value.startswith(_FERNET_PREFIX):
            logger.warning(
                "EncryptedString: valor em plaintext detectado (legado). "
                "Re-salve o registro para criptografar."
            )
            return value
        try:
            return decrypt_str(value)
        except InvalidToken:
            # Ciphertext corrompido ou chave errada. Melhor falhar do que
            # vazar dados ambíguos. Não usamos os.getenv para nem tentar
            # "decodificar" — se chegou aqui é um problema operacional.
            logger.error("EncryptedString: InvalidToken ao decifrar valor")
            raise


__all__ = ["EncryptedString"]
