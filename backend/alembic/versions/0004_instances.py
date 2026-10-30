"""M2.5：模板/实例二层模型 + 技能文件夹 + 主 agent

- agents: +is_template/template_id/owner_user_id/is_master；存量 agent 置为模板
- skills: +category；instructions 迁移进 skill_files(SKILL.md)
- 新表: agent_claim_grants / skill_files
- 废弃: agent_user_overlays / user_agent_skills

Revision ID: 0004_instances
Revises: 0003_message_tools
Create Date: 2026-08-06

"""
import sqlalchemy as sa
from alembic import op

revision = "0004_instances"
down_revision = "0003_message_tools"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- agents 二层模型 ---
    op.add_column("agents", sa.Column("is_template", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("agents", sa.Column("template_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=True))
    op.add_column("agents", sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True))
    op.add_column("agents", sa.Column("is_master", sa.Boolean(), nullable=False, server_default=sa.false()))
    # 存量数字人全部视为模板
    op.execute("UPDATE agents SET is_template = true")

    # --- 模板可领用范围 ---
    op.create_table(
        "agent_claim_grants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("grantee_type", sa.String(16), nullable=False),
        sa.Column("grantee_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- 技能文件夹化 ---
    op.add_column("skills", sa.Column("category", sa.String(64), nullable=False, server_default=""))
    op.create_table(
        "skill_files",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("skill_id", sa.Integer(), sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("path", sa.String(256), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("skill_id", "path", name="uq_skill_file"),
    )
    # 存量 instructions → SKILL.md
    op.execute(
        """
        INSERT INTO skill_files (skill_id, path, content, created_at, updated_at)
        SELECT id, 'SKILL.md', instructions, now(), now() FROM skills WHERE deleted_at IS NULL
        """
    )

    # --- 废弃 overlay 层 ---
    op.drop_table("user_agent_skills")
    op.drop_table("agent_user_overlays")


def downgrade() -> None:
    op.create_table(
        "agent_user_overlays",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("custom_instructions", sa.Text(), nullable=False, server_default=""),
        sa.Column("personal_memory", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "agent_id", name="uq_user_agent_overlay"),
    )
    op.create_table(
        "user_agent_skills",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False),
        sa.Column("skill_id", sa.Integer(), sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "agent_id", "skill_id", name="uq_user_agent_skill"),
    )
    op.drop_table("skill_files")
    op.drop_column("skills", "category")
    op.drop_table("agent_claim_grants")
    op.drop_column("agents", "is_master")
    op.drop_column("agents", "owner_user_id")
    op.drop_column("agents", "template_id")
    op.drop_column("agents", "is_template")
