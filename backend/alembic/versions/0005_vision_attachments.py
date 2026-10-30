"""M2.6：模型视觉能力标记 + 消息图片附件

- models: +supports_vision（网关管理可标记哪些模型支持图片输入）
- messages: +attachments（JSON 文本，图片等临时附件元数据 [{name,path,mime}]，文件落盘 data/tmp_uploads/）

Revision ID: 0005_vision_attachments
Revises: 0004_instances
Create Date: 2026-08-07

"""
import sqlalchemy as sa
from alembic import op

revision = "0005_vision_attachments"
down_revision = "0004_instances"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("models", sa.Column("supports_vision", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("messages", sa.Column("attachments", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "attachments")
    op.drop_column("models", "supports_vision")
