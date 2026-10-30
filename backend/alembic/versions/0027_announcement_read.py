"""M27：users 加 last_read_announcement_id——公告未读 NEW 标签的已读游标。

存服务端（而非浏览器 localStorage）是为了跨设备同步：电脑看过公告后手机端不再亮 NEW。
NULL=从未读过；未读判定 = 存在 id 大于该游标的公告。

Revision ID: 0027_announcement_read
Revises: 0026_reasoning_content
Create Date: 2026-09-21
"""
import sqlalchemy as sa
from alembic import op

revision = "0027_announcement_read"
down_revision = "0026_reasoning_content"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("last_read_announcement_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "last_read_announcement_id")
