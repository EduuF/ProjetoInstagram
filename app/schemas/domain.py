"""DTOs Pydantic do domínio (não ligados à persistência).

Estes são os modelos que a API expõe ao frontend no futuro. Separados
dos modelos ORM (que ficam em ``app.db.models``) porque:

- O formato da API é público/estável; o do banco evolui por dentro.
- Permitem validação forte de entrada vinda do frontend.
- Evitam vazar campos sensíveis (ex.: ``access_token``, ``password_hash``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class ClientRead(BaseModel):
    """Representação pública de um cliente."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    email: EmailStr
    name: Optional[str] = None
    is_active: bool
    created_at: datetime


# --------------------------------------------------------------------------- #
# InstagramAccount
# --------------------------------------------------------------------------- #
class InstagramAccountRead(BaseModel):
    """Dados públicos de uma conta IG conectada (sem o access_token)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    client_id: str
    ig_business_account_id: str
    username: Optional[str] = None
    is_active: bool
    created_at: datetime


# --------------------------------------------------------------------------- #
# AutoReplyRule (input + output)
# --------------------------------------------------------------------------- #
class AutoReplyRuleCreate(BaseModel):
    """Payload de criação/edição de regra vindo do frontend."""

    trigger_word: str = Field(..., min_length=1, max_length=100)
    message_template: str = Field(..., min_length=1, max_length=2000)
    priority: int = 100
    is_active: bool = True


class AutoReplyRuleRead(AutoReplyRuleCreate):
    """Representação pública de uma regra."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    instagram_account_id: str
    created_at: datetime
    updated_at: datetime
