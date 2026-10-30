"""workspace_shares.folder → paths（M5.1：单文件/多选共享）

folder=""（整个工作区）→ paths='[]'；folder='报表' → paths='["报表"]'。

Revision ID: 0015_share_paths
Revises: 0014_workspace_shares
Create Date: 2026-08-26
"""
import sqlalchemy as sa
from alembic import op

revision = "0015_share_paths"
down_revision = "0014_workspace_shares"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspace_shares", sa.Column("paths", sa.Text(), nullable=False, server_default="[]"))
    op.execute("UPDATE workspace_shares SET paths = CASE WHEN folder = '' THEN '[]' ELSE json_build_array(folder)::text END")
    op.drop_column("workspace_shares", "folder")


def downgrade() -> None:
    op.add_column("workspace_shares", sa.Column("folder", sa.String(256), nullable=False, server_default=""))
    op.execute("UPDATE workspace_shares SET folder = CASE WHEN paths = '[]' THEN '' ELSE (paths::jsonb ->> 0) END")
    op.drop_column("workspace_shares", "paths")
