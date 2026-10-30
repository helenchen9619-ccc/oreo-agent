"""M3.2：会话与工作区文件置顶

- conversations: +pinned（会话列表 ⋯ 菜单的"置顶"）
- workspace_files: +pinned（文件在所属文件夹内置顶）

Revision ID: 0006_pin
Revises: 0005_vision_attachments
Create Date: 2026-08-11

"""
import sqlalchemy as sa
from alembic import op

revision = "0006_pin"
down_revision = "0005_vision_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("workspace_files", sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("workspace_files", "pinned")
    op.drop_column("conversations", "pinned")
