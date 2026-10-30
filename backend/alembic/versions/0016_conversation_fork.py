"""conversations 加 forked_from_id（M6a：会话分叉）

Revision ID: 0016_conversation_fork
Revises: 0015_share_paths
Create Date: 2026-08-26
"""
import sqlalchemy as sa
from alembic import op

revision = "0016_conversation_fork"
down_revision = "0015_share_paths"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("forked_from_id", sa.Integer(), sa.ForeignKey("conversations.id"), nullable=True))


def downgrade() -> None:
    op.drop_column("conversations", "forked_from_id")
