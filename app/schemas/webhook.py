"""Modelos Pydantic para o payload do webhook da Meta.

Formato geral (eventos de conta, ex.: comentários)::

    {
      "object": "instagram",
      "entry": [
        {
          "id": "<IG_BUSINESS_ACCOUNT_ID>",   # chave de tenant
          "time": 1700000000,
          "changes": [
            {
              "field": "comments",
              "value": {
                "id": "<COMMENT_ID>",
                "text": "quero",
                "from":  {"id": "<IGSID>", "username": "..."},
                "media": {"id": "...", "media_product_type": "FEED"}
              }
            }
          ]
        }
      ]
    }

Usamos ``extra="allow"`` em todos os modelos para que campos novos
adicionados pela Meta no futuro não quebrem a validação.
"""

from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------- #
# Sub-estruturas do evento de comentário
# --------------------------------------------------------------------------- #
class CommentAuthor(BaseModel):
    """Autor de um comentário."""

    model_config = ConfigDict(extra="allow")

    id: str
    username: Optional[str] = None


class CommentMedia(BaseModel):
    """Mídia (post/reel/story) onde o comentário foi feito."""

    model_config = ConfigDict(extra="allow")

    id: str
    media_product_type: Optional[str] = None  # FEED, REELS, STORY, ...


class CommentValue(BaseModel):
    """Conteúdo de ``changes[].value`` quando ``field == "comments"``."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str  # ID do comentário — usado na Private Reply
    text: Optional[str] = None
    parent_id: Optional[str] = None

    # ``from`` é keyword em Python: renomeamos para ``author`` mas
    # aceitamos o alias original na desserialização.
    author: Optional[CommentAuthor] = Field(default=None, alias="from")
    media: Optional[CommentMedia] = None


# --------------------------------------------------------------------------- #
# Envelope
# --------------------------------------------------------------------------- #
class ChangeEntry(BaseModel):
    """Um item dentro de ``entry[].changes``."""

    model_config = ConfigDict(extra="allow")

    field: str                        # "comments", "mentions", "live_comments", ...
    value: dict[str, Any]             # formato varia por ``field``


class WebhookEntry(BaseModel):
    """Um item de ``entry[]``. O ``id`` aqui é a **chave de tenant**."""

    model_config = ConfigDict(extra="allow")

    id: str                           # IG Business Account ID
    time: int
    changes: Optional[List[ChangeEntry]] = None
    messaging: Optional[List[dict[str, Any]]] = None  # reservado p/ DMs futuros


class WebhookPayload(BaseModel):
    """Payload completo recebido em POST /webhook."""

    model_config = ConfigDict(extra="allow")

    object: str                       # esperado: "instagram"
    entry: List[WebhookEntry]
