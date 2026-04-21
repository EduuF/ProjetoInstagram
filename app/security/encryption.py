"""Criptografia simétrica em repouso para dados sensíveis (tokens do IG).

Usamos `Fernet <https://cryptography.io/en/latest/fernet/>`_ (AES-128-CBC +
HMAC-SHA256) — primitiva de alto nível, difícil de usar errado:

- Gera um nonce aleatório por chamada (ciphertexts nunca colidem).
- Já autentica o conteúdo (detecta tampering).
- A chave tem um formato bem definido (``Fernet.generate_key()``).

Por que criptografar tokens no DB?
    Um dump do banco (backup vazado, acesso read-only indevido, SQL injection
    em outra parte da stack) não deve ser suficiente para sequestrar contas
    Instagram dos nossos clientes. Com criptografia em repouso, o atacante
    precisaria também do ``ENCRYPTION_KEY`` (que fica só no ambiente,
    idealmente no AWS Secrets Manager em produção).

Este módulo é **stateful**: o singleton ``_fernet`` é construído uma vez a
partir de ``Settings.encryption_key`` e reutilizado. Isso evita pagar a
inicialização do Fernet em cada INSERT/UPDATE.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    """Constrói (ou retorna) o cifrador global."""
    settings = get_settings()
    return Fernet(settings.encryption_key.encode("utf-8"))


def encrypt_str(plaintext: str) -> str:
    """Cifra uma string e retorna texto ASCII (seguro para coluna TEXT)."""
    if plaintext is None:  # type: ignore[unreachable]
        raise ValueError("encrypt_str não aceita None")
    token = _fernet().encrypt(plaintext.encode("utf-8"))
    return token.decode("ascii")


def decrypt_str(ciphertext: str) -> str:
    """Decifra uma string. Lança ``InvalidToken`` se tampered/errado."""
    return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")


__all__ = ["encrypt_str", "decrypt_str", "InvalidToken"]
