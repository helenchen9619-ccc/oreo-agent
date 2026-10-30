"""M15：system_settings 通用键值表（首个用途：maintenance 维护模式开关）。

Revision ID: 0025_system_settings
Revises: 0024_password_login
Create Date: 2026-09-04
"""
import sqlalchemy as sa
from alembic import op

revision = "0025_system_settings"
down_revision = "0024_password_login"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(64), nullable=False, unique=True),
        sa.Column("value", sa.String(255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("system_settings")
