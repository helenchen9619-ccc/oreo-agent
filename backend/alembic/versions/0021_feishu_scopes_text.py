"""M9b.2：users.feishu_scopes 从 VARCHAR(512) 加宽为 TEXT。
scope 列表随 M7→M8→M9→M9c→M9b.2 一路增，加 mail:user_mailbox.message.address:read 后
总长撑破 512，登录回调写库报 StringDataRightTruncationError → 500。

Revision ID: 0021_feishu_scopes_text
Revises: 0020_zhishu_user_id
Create Date: 2026-09-01
"""
import sqlalchemy as sa
from alembic import op

revision = "0021_feishu_scopes_text"
down_revision = "0020_zhishu_user_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("users", "feishu_scopes", type_=sa.Text(), existing_type=sa.String(512), nullable=True)


def downgrade() -> None:
    op.alter_column("users", "feishu_scopes", type_=sa.String(512), existing_type=sa.Text(), nullable=True)
