"""M8 每日飞书动态：scheduled_tasks 加 push_to_me（应用机器人私聊推本人，替代群 webhook）

Revision ID: 0018_task_push_to_me
Revises: 0017_feishu_read
Create Date: 2026-08-28
"""
import sqlalchemy as sa
from alembic import op

revision = "0018_task_push_to_me"
down_revision = "0017_feishu_read"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scheduled_tasks", sa.Column("push_to_me", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("scheduled_tasks", "push_to_me")
