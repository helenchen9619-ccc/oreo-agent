"""M3.8：定时任务表（scheduled_tasks）

到点自动以属主身份对指定数字人发起一轮完整对话，结果落新会话（channel="cron"）。
DB 是调度唯一事实源：next_run_at 持久化，tick 循环每 30s 捞到期任务。

Revision ID: 0009_scheduled_tasks
Revises: 0008_loop_rounds_16
Create Date: 2026-08-12

"""
import sqlalchemy as sa
from alembic import op

revision = "0009_scheduled_tasks"
down_revision = "0008_loop_rounds_16"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduled_tasks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("cron_expr", sa.String(64), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Shanghai"),
        sa.Column("model_id", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(16), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_conversation_id", sa.Integer(), nullable=True),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_scheduled_tasks_user_id", "scheduled_tasks", ["user_id"])
    op.create_index("ix_scheduled_tasks_agent_id", "scheduled_tasks", ["agent_id"])


def downgrade() -> None:
    op.drop_index("ix_scheduled_tasks_agent_id", table_name="scheduled_tasks")
    op.drop_index("ix_scheduled_tasks_user_id", table_name="scheduled_tasks")
    op.drop_table("scheduled_tasks")
