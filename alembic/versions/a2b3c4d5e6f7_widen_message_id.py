"""widen auto_replies_sent.message_id to Text

Revision ID: a2b3c4d5e6f7
Revises: 2581d5990449
Create Date: 2026-04-21 20:15:00.000000

O ``message_id`` retornado por ``/me/messages`` é um identificador base64
que pode passar de 180 chars. A coluna original foi criada como
``VARCHAR(128)`` e isso causava ``StringDataRightTruncationError`` no
INSERT, abortando a transação inteira do webhook (inclusive perdendo o
``CommentEvent``, o que zerava as estatísticas da dashboard).

Esta migration apenas alarga a coluna para ``TEXT`` (sem limite prático).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, None] = "2581d5990449"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "auto_replies_sent",
        "message_id",
        existing_type=sa.String(length=128),
        type_=sa.Text(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "auto_replies_sent",
        "message_id",
        existing_type=sa.Text(),
        type_=sa.String(length=128),
        existing_nullable=True,
    )
