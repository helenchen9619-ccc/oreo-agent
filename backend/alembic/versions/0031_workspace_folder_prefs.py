"""M36：工作区文件夹偏好表（置顶 + 手动排序，服务端存，跨设备同步）。

Revision ID: 0031_workspace_folder_prefs
Revises: 0030_wecom_viewers_receivers
Create Date: 2026-10-26
"""
import sqlalchemy as sa
from alembic import op

revision = "0031_workspace_folder_prefs"
down_revision = "0030_wecom_viewers_receivers"
branch_labels = None
depends_on = None

_TS = dict(nullable=False)


def upgrade() -> None:
    op.create_table(
        "workspace_folder_prefs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("folder_path", sa.String(256), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), **_TS),
        sa.Column("updated_at", sa.DateTime(timezone=True), **_TS),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "agent_id", "folder_path", name="uq_ws_folder_pref"),
    )


def downgrade() -> None:
    op.drop_table("workspace_folder_prefs")
