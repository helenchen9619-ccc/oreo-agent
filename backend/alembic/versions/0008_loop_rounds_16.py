"""M3.4.1：工具调用轮次上限默认 8 → 16（多步 ETL 任务 8 轮不够用被终止）

- agents.max_loop_rounds: server_default 8 → 16
- 存量行一并提到 16（用户拍板；管理端 PATCH 仍可在 1-20 内按 agent 单独调）

Revision ID: 0008_loop_rounds_16
Revises: 0007_skill_lineage
Create Date: 2026-08-12

"""
import sqlalchemy as sa
from alembic import op

revision = "0008_loop_rounds_16"
down_revision = "0007_skill_lineage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("agents", "max_loop_rounds", server_default="16")
    op.execute(sa.text("UPDATE agents SET max_loop_rounds = 16 WHERE max_loop_rounds = 8"))


def downgrade() -> None:
    op.alter_column("agents", "max_loop_rounds", server_default="8")
