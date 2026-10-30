"""M31：企业微信会话存档 Phase 1——消息明文落库 + 姓名/群名映射 + 敏感词表 + 查询开关预留。

Revision ID: 0029_wecom_archive
Revises: 0028_feishu_p2p_roster
Create Date: 2026-09-23
"""
import sqlalchemy as sa
from alembic import op

revision = "0029_wecom_archive"
down_revision = "0028_feishu_p2p_roster"
branch_labels = None
depends_on = None

_TS = dict(nullable=False)


def upgrade() -> None:
    op.create_table(
        "wecom_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("msgid", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False, server_default="send"),
        sa.Column("sender_userid", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("receivers", sa.Text(), nullable=False, server_default=""),
        sa.Column("roomid", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("msgtype", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("msgtime", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), **_TS),
        sa.Column("updated_at", sa.DateTime(timezone=True), **_TS),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("msgid", name="uq_wecom_messages_msgid"),
    )
    op.create_index("ix_wecom_messages_seq", "wecom_messages", ["seq"])

    op.create_table(
        "wecom_keywords",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("word", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), **_TS),
        sa.Column("updated_at", sa.DateTime(timezone=True), **_TS),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("word", name="uq_wecom_keywords_word"),
    )

    op.create_table(
        "wecom_contacts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("userid", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), **_TS),
        sa.Column("updated_at", sa.DateTime(timezone=True), **_TS),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("userid", name="uq_wecom_contacts_userid"),
    )

    op.create_table(
        "wecom_rooms",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("roomid", sa.String(length=64), nullable=False),
        sa.Column("roomname", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), **_TS),
        sa.Column("updated_at", sa.DateTime(timezone=True), **_TS),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("roomid", name="uq_wecom_rooms_roomid"),
    )

    # Phase 2 查询工具权限开关预留（前端开关随 Phase 2 上）
    op.add_column("agents", sa.Column("allow_wecom_archive", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("agents", "allow_wecom_archive")
    op.drop_table("wecom_rooms")
    op.drop_table("wecom_contacts")
    op.drop_table("wecom_keywords")
    op.drop_index("ix_wecom_messages_seq", table_name="wecom_messages")
    op.drop_table("wecom_messages")
