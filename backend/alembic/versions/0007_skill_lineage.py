"""M3.3：技能流通体系——署名 + 血缘 + fork 源快照

- skills: +created_by（原作者署名；收录时=原属主）
- skills: +source_skill_id（血缘：fork/收录自哪个技能）
- skills: +source_updated_at（fork 时源的 updated_at 快照，判断"源有更新"）

存量回填：个人技能 created_by = owner_user_id。

Revision ID: 0007_skill_lineage
Revises: 0006_pin
Create Date: 2026-08-11

"""
import sqlalchemy as sa
from alembic import op

revision = "0007_skill_lineage"
down_revision = "0006_pin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("skills", sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True))
    op.add_column("skills", sa.Column("source_skill_id", sa.Integer(), sa.ForeignKey("skills.id"), nullable=True))
    op.add_column("skills", sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE skills SET created_by = owner_user_id WHERE owner_user_id IS NOT NULL")


def downgrade() -> None:
    op.drop_column("skills", "source_updated_at")
    op.drop_column("skills", "source_skill_id")
    op.drop_column("skills", "created_by")
