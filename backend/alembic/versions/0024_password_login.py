"""M14b：users 表加 password_hash 列（账号密码登录；NULL=未设密码，纯飞书账号）。

Revision ID: 0024_password_login
Revises: 0023_image_gen
Create Date: 2026-09-04
"""
import sqlalchemy as sa
from alembic import op

revision = "0024_password_login"
down_revision = "0023_image_gen"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "password_hash")
