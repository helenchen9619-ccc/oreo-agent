"""M13a：agents 表加 allow_image_generate 列（文生图工具开关，火山方舟 Seedream）。

Revision ID: 0023_image_gen
Revises: 0022_announcements
Create Date: 2026-09-04
"""
import sqlalchemy as sa
from alembic import op

revision = "0023_image_gen"
down_revision = "0022_announcements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("allow_image_generate", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("agents", "allow_image_generate")
