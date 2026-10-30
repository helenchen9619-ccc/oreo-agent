"""api_hosts 白名单表 + agents.allow_api_request

Revision ID: 0013_api_hosts
Revises: 0012_feishu_push
Create Date: 2026-08-20
"""
import sqlalchemy as sa
from alembic import op

revision = "0013_api_hosts"
down_revision = "0012_feishu_push"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_hosts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("label", sa.String(128), nullable=False, server_default=""),
        sa.Column("auth_type", sa.String(32), nullable=False, server_default="none"),
        sa.Column("auth_config_enc", sa.String(4096), nullable=False, server_default=""),
        sa.Column("allowed_methods", sa.String(64), nullable=False, server_default="GET,POST"),
        sa.Column("min_interval_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_hosts_host", "api_hosts", ["host"], unique=True)
    op.add_column("agents", sa.Column("allow_api_request", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("agents", "allow_api_request")
    op.drop_index("ix_api_hosts_host", table_name="api_hosts")
    op.drop_table("api_hosts")
