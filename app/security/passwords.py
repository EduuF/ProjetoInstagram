"""Hash e verificação de senhas (``bcrypt``).

Por que bcrypt?
    - É "slow by design": custoso para brute force mesmo em GPU.
    - Tem salt embutido no hash (não precisamos armazenar separado).
    - Formato estável e reconhecido por qualquer banco.

Observações de segurança:
    - Bcrypt trunca entradas após 72 bytes. Para permitir senhas longas de
      forma segura, aplicamos SHA-256 antes de bcrypt (prática recomendada,
      usada por exemplo pelo Django). Assim, qualquer senha vira 32 bytes
      de entrada para o bcrypt — sem truncamento silencioso.
    - ``verify_password`` usa ``bcrypt.checkpw`` que é timing-safe.
"""

from __future__ import annotations

import hashlib

import bcrypt

# Cost factor. 12 é o default da indústria em 2025. Aumentar custa mais CPU
# em cada login/signup (mas dificulta ataques offline). Manter em 12.
_BCRYPT_ROUNDS = 12


def _prehash(password: str) -> bytes:
    """SHA-256 da senha, para contornar o limite de 72 bytes do bcrypt."""
    return hashlib.sha256(password.encode("utf-8")).digest()


def hash_password(password: str) -> str:
    """Gera hash bcrypt (ASCII, guardar em coluna TEXT)."""
    salt = bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    digest = bcrypt.hashpw(_prehash(password), salt)
    return digest.decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """Compara senha em plaintext com hash bcrypt. Retorna False em erro."""
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode("ascii"))
    except ValueError:
        # Hash mal formatado (ex.: migração, dados corrompidos): tratamos
        # como senha inválida, sem levantar exceção para o caller.
        return False


__all__ = ["hash_password", "verify_password"]
