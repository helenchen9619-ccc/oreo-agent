"""M31.1：企微存档消息查看授权 + 告警接收人配置 + 消息表 msgtime 索引。

Revision ID: 0030_wecom_viewers_receivers
Revises: 0029_wecom_archive
Create Date: 2026-09-23
"""
import sqlalchemy as sa
from alembic import op

revision = "0030_wecom_viewers_receivers"
down_revision = "0029_wecom_archive"
branch_labels = None
depends_on = None

_TS = dict(nullable=False)


def upgrade() -> None:
    # 授权查看人：非 admin 也能看存档消息（进不了管理区）
    op.create_table(
        "wecom_viewers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), **_TS),
        sa.Column("updated_at", sa.DateTime(timezone=True), **_TS),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", name="uq_wecom_viewers_user_id"),
    )

    # 告警接收人：空表时回落全体绑定飞书的 admin（兼容 M31 现状）
    op.create_table(
        "wecom_alert_receivers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), **_TS),
        sa.Column("updated_at", sa.DateTime(timezone=True), **_TS),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", name="uq_wecom_alert_receivers_user_id"),
    )

    # 消息列表主排序/筛选列（M31.1 消息查看界面；PostgreSQL 建索引短暂锁表，表小无感）
    op.create_index("ix_wecom_messages_msgtime", "wecom_messages", ["msgtime"])


def downgrade() -> None:
    op.drop_index("ix_wecom_messages_msgtime", table_name="wecom_messages")
    op.drop_table("wecom_alert_receivers")
    op.drop_table("wecom_viewers")
