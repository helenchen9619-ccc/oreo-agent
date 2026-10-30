"""AGENT-OPS：turn_records 表（轮次级运行记录）+ admin 角色授予「运行中心」模块

Revision ID: 0011_turn_records
Revises: 0010_allow_web_search
Create Date: 2026-08-14

"""
import sqlalchemy as sa
from alembic import op

revision = "0011_turn_records"
down_revision = "0010_allow_web_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "turn_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rounds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_errors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
        sa.Column("error_msg", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_turn_records_agent_started", "turn_records", ["agent_id", "started_at"])
    op.create_index("ix_turn_records_conv", "turn_records", ["conversation_id"])
    # admin 开箱即有「运行中心」权限（幂等）
    op.execute(
        "INSERT INTO role_modules (role, module, created_at, updated_at)"
        " SELECT 'admin', 'ops', now(), now()"
        " WHERE NOT EXISTS (SELECT 1 FROM role_modules WHERE role = 'admin' AND module = 'ops' AND deleted_at IS NULL)"
    )


def downgrade() -> None:
    op.execute("DELETE FROM role_modules WHERE role = 'admin' AND module = 'ops'")
    op.drop_index("ix_turn_records_conv", table_name="turn_records")
    op.drop_index("ix_turn_records_agent_started", table_name="turn_records")
    op.drop_table("turn_records")
