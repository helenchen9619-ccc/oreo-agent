"""M1 初始 schema

Revision ID: 0001_init
Revises:
Create Date: 2026-08-06

"""
import sqlalchemy as sa
from alembic import op

revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


def _common() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "departments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("feishu_dept_id", sa.String(64), unique=True, nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("departments.id"), nullable=True),
        *_common(),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("feishu_open_id", sa.String(64), unique=True, nullable=True),
        sa.Column("username", sa.String(64), unique=True, nullable=False),
        sa.Column("name", sa.String(128), nullable=False, server_default=""),
        sa.Column("avatar_url", sa.String(512), nullable=True),
        sa.Column("department_id", sa.Integer(), sa.ForeignKey("departments.id"), nullable=True),
        sa.Column("role", sa.String(16), nullable=False, server_default="member"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        *_common(),
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("detail", sa.String(4096), nullable=True),
        *_common(),
    )
    op.create_table(
        "model_providers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("slug", sa.String(64), unique=True, nullable=False),
        sa.Column("base_url", sa.String(512), nullable=False),
        sa.Column("api_key_encrypted", sa.String(1024), nullable=False),
        sa.Column("provider_type", sa.String(32), nullable=False, server_default="openai-compatible"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_common(),
    )
    op.create_table(
        "models",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("provider_id", sa.Integer(), sa.ForeignKey("model_providers.id"), nullable=False),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("model_type", sa.String(16), nullable=False, server_default="chat"),
        sa.Column("price_input_per_1k", sa.Numeric(12, 8), nullable=False, server_default="0"),
        sa.Column("price_output_per_1k", sa.Numeric(12, 8), nullable=False, server_default="0"),
        sa.Column("context_window", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_common(),
    )
    op.create_table(
        "model_grants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("model_id", sa.Integer(), sa.ForeignKey("models.id"), nullable=False),
        sa.Column("grantee_type", sa.String(16), nullable=False),
        sa.Column("grantee_id", sa.Integer(), nullable=True),
        *_common(),
    )
    op.create_table(
        "usage_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("provider_id", sa.Integer(), sa.ForeignKey("model_providers.id"), nullable=False),
        sa.Column("model_id", sa.Integer(), sa.ForeignKey("models.id"), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost", sa.Numeric(14, 8), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_usage_user_time", "usage_records", ["user_id", "created_at"])
    op.create_table(
        "quotas",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.Integer(), nullable=False),
        sa.Column("period", sa.String(8), nullable=False, server_default="day"),
        sa.Column("limit_tokens", sa.Integer(), nullable=True),
        sa.Column("limit_cost", sa.Numeric(12, 4), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_common(),
    )
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("agent_id", sa.Integer(), nullable=True),
        sa.Column("channel", sa.String(16), nullable=False, server_default="web"),
        sa.Column("external_chat_id", sa.String(128), nullable=True),
        sa.Column("title", sa.String(256), nullable=False, server_default="新对话"),
        *_common(),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("tool_calls", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_messages_conv", "messages", ["conversation_id", "id"])


def downgrade() -> None:
    for t in ("messages", "conversations", "quotas", "usage_records", "model_grants", "models", "model_providers", "audit_logs", "users", "departments"):
        op.drop_table(t)
