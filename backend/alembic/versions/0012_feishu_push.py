"""M4b：scheduled_tasks 加飞书群机器人推送字段

Revision ID: 0012_feishu_push
Revises: 0011_turn_records
Create Date: 2026-08-18

"""
import sqlalchemy as sa
from alembic import op

revision = "0012_feishu_push"
down_revision = "0011_turn_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scheduled_tasks", sa.Column("feishu_webhook", sa.String(512), nullable=True))
    op.add_column("scheduled_tasks", sa.Column("feishu_secret", sa.String(128), nullable=True))
    op.add_column("scheduled_tasks", sa.Column("last_push_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("scheduled_tasks", "last_push_error")
    op.drop_column("scheduled_tasks", "feishu_secret")
    op.drop_column("scheduled_tasks", "feishu_webhook")
