"""workspace_shares 工作区文件夹跨用户只读共享

Revision ID: 0014_workspace_shares
Revises: 0013_api_hosts
Create Date: 2026-08-21
"""
import sqlalchemy as sa
from alembic import op

revision = "0014_workspace_shares"
down_revision = "0013_api_hosts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_shares",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("folder", sa.String(256), nullable=False, server_default=""),
        sa.Column("grantee_type", sa.String(16), nullable=False, server_default="user"),
        sa.Column("grantee_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_workspace_shares_owner", "workspace_shares", ["owner_user_id", "agent_id"])


def downgrade() -> None:
    op.drop_index("ix_workspace_shares_owner", table_name="workspace_shares")
    op.drop_table("workspace_shares")
