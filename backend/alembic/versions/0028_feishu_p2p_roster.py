"""M28.4：feishu_p2p_chats 私聊会话名册——发现过的 p2p 会话（chat_id↔对方姓名）按用户持久化。

/search/v2/message 索引不回溯授权前的私聊、/im/v1/chats 列表不含 p2p，
发现成果不落库就会随活跃窗口流失；名册让「找到过一次」终身可查。

Revision ID: 0028_feishu_p2p_roster
Revises: 0027_announcement_read
Create Date: 2026-09-22
"""
import sqlalchemy as sa
from alembic import op

revision = "0028_feishu_p2p_roster"
down_revision = "0027_announcement_read"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feishu_p2p_chats",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.String(length=64), nullable=False),
        sa.Column("peer_name", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "chat_id", name="uq_feishu_p2p_chats_user_chat"),
    )
    op.create_index("ix_feishu_p2p_chats_user_id", "feishu_p2p_chats", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_feishu_p2p_chats_user_id", table_name="feishu_p2p_chats")
    op.drop_table("feishu_p2p_chats")
