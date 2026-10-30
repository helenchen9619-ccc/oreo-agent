"""M3.9：agents 加 allow_web_search（联网搜索/网页读取能力开关，数字人级，镜像 allow_code_exec）

Revision ID: 0010_allow_web_search
Revises: 0009_scheduled_tasks
Create Date: 2026-08-13

"""
import sqlalchemy as sa
from alembic import op

revision = "0010_allow_web_search"
down_revision = "0009_scheduled_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("allow_web_search", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("agents", "allow_web_search")
