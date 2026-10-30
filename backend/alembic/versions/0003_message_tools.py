"""M2 补充：messages 增加 compaction/工具调用字段

Revision ID: 0003_message_tools
Revises: 0002_agents
Create Date: 2026-08-06

"""
import sqlalchemy as sa
from alembic import op

revision = "0003_message_tools"
down_revision = "0002_agents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("compacted", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("messages", sa.Column("tool_call_id", sa.String(64), nullable=True))
    op.add_column("messages", sa.Column("name", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "name")
    op.drop_column("messages", "tool_call_id")
    op.drop_column("messages", "compacted")
