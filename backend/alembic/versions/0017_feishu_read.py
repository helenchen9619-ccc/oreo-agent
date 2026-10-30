"""M7 飞书内容阅读：users 存 per-user 飞书 token 三元组；agents 加 allow_feishu 开关

Revision ID: 0017_feishu_read
Revises: 0016_conversation_fork
Create Date: 2026-08-26
"""
import sqlalchemy as sa
from alembic import op

revision = "0017_feishu_read"
down_revision = "0016_conversation_fork"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("feishu_user_token_enc", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("feishu_refresh_token_enc", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("feishu_token_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("feishu_refresh_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("feishu_scopes", sa.String(512), nullable=True))
    op.add_column("agents", sa.Column("allow_feishu", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("agents", "allow_feishu")
    op.drop_column("users", "feishu_scopes")
    op.drop_column("users", "feishu_refresh_expires_at")
    op.drop_column("users", "feishu_token_expires_at")
    op.drop_column("users", "feishu_refresh_token_enc")
    op.drop_column("users", "feishu_user_token_enc")
