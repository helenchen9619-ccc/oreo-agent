"""M2 数字人体系 schema

Revision ID: 0002_agents
Revises: 0001_init
Create Date: 2026-08-06

"""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0002_agents"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def _common() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "agents",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("avatar", sa.String(512), nullable=True),
        sa.Column("description", sa.String(512), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("default_model_id", sa.Integer(), sa.ForeignKey("models.id"), nullable=True),
        sa.Column("model_allowlist", sa.Text(), nullable=True),
        sa.Column("allow_code_exec", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("max_loop_rounds", sa.Integer(), nullable=False, server_default="8"),
        *_common(),
    )
    op.create_table(
        "agent_files",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("file_key", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.UniqueConstraint("agent_id", "file_key", name="uq_agent_file"),
        *_common(),
    )
    op.create_table(
        "agent_file_versions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("agent_file_id", sa.Integer(), sa.ForeignKey("agent_files.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("saved_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "skills",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(64), unique=True, nullable=False),
        sa.Column("description", sa.String(512), nullable=False, server_default=""),
        sa.Column("instructions", sa.Text(), nullable=False, server_default=""),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_common(),
    )
    op.create_table(
        "agent_skills",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("skill_id", sa.Integer(), sa.ForeignKey("skills.id"), nullable=False),
        sa.UniqueConstraint("agent_id", "skill_id", name="uq_agent_skill"),
        *_common(),
    )
    op.create_table(
        "agent_user_overlays",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("custom_instructions", sa.Text(), nullable=False, server_default=""),
        sa.Column("personal_memory", sa.Text(), nullable=False, server_default=""),
        sa.UniqueConstraint("user_id", "agent_id", name="uq_user_agent_overlay"),
        *_common(),
    )
    op.create_table(
        "user_agent_skills",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("skill_id", sa.Integer(), sa.ForeignKey("skills.id"), nullable=False),
        sa.UniqueConstraint("user_id", "agent_id", "skill_id", name="uq_user_agent_skill"),
        *_common(),
    )
    op.create_table(
        "agent_memory_notes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("note_date", sa.Date(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_memory_notes_agent_user_date", "agent_memory_notes", ["agent_id", "user_id", "note_date"])
    op.create_table(
        "workspace_files",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("filename", sa.String(256), nullable=False),
        sa.Column("mime", sa.String(128), nullable=False, server_default="application/octet-stream"),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("storage_path", sa.String(512), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=True),
        *_common(),
    )
    op.create_table(
        "role_modules",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("module", sa.String(32), nullable=False),
        sa.UniqueConstraint("role", "module", name="uq_role_module"),
        *_common(),
    )
    # 内置角色默认权限：admin 全开，member 仅对话
    from datetime import UTC, datetime
    now = datetime.now(UTC)
    role_modules = sa.table(
        "role_modules",
        sa.column("role", sa.String), sa.column("module", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)), sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        role_modules,
        [{"role": "admin", "module": m, "created_at": now, "updated_at": now} for m in ("chat", "agents_admin", "skills_admin", "gateway_admin", "stats", "audit")]
        + [{"role": "member", "module": "chat", "created_at": now, "updated_at": now}],
    )
    # conversations 增加压缩摘要与最近上下文 token 数
    op.add_column("conversations", sa.Column("compact_summary", sa.Text(), nullable=True))
    op.add_column("conversations", sa.Column("last_context_tokens", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("conversations", "last_context_tokens")
    op.drop_column("conversations", "compact_summary")
    for t in ("role_modules", "workspace_files", "agent_memory_notes", "user_agent_skills", "agent_user_overlays", "agent_skills", "skills", "agent_file_versions", "agent_files", "agents"):
        op.drop_table(t)
