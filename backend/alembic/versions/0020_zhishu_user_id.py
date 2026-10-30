"""M9c 智书合同：users 存 per-user 智书 user_id（8 位字符串，智书自己的 id 空间，与飞书 id 不通）；
agents 加 allow_zhishu 开关

Revision ID: 0020_zhishu_user_id
Revises: 0019_skill_deleted_rename
Create Date: 2026-09-01
"""
import sqlalchemy as sa
from alembic import op

revision = "0020_zhishu_user_id"
down_revision = "0019_skill_deleted_rename"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("zhishu_user_id", sa.String(64), nullable=True))
    op.add_column("agents", sa.Column("allow_zhishu", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("agents", "allow_zhishu")
    op.drop_column("users", "zhishu_user_id")
