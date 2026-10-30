"""M24.0 热修：messages 加 reasoning_content 列。

Kimi 等思考模型（thinking mode）要求多轮请求把 assistant 的 reasoning_content 原样回传，
否则第二轮起报 400「The `reasoning_content` in the thinking mode must be passed back to the API」，
表现为数字人完全没反应。列只存不回传会丢——故入库持久化。

Revision ID: 0026_reasoning_content
Revises: 0025_system_settings
Create Date: 2026-09-17
"""
import sqlalchemy as sa
from alembic import op

revision = "0026_reasoning_content"
down_revision = "0025_system_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("reasoning_content", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "reasoning_content")
