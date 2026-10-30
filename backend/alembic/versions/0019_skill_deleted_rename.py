"""技能软删除改名：释放 skills.name 唯一约束（软删除行统一改为 name--del-<id>，修复同名重建 500）

Revision ID: 0019_skill_deleted_rename
Revises: 0018_task_push_to_me
Create Date: 2026-08-31
"""
import sqlalchemy as sa
from alembic import op

revision = "0019_skill_deleted_rename"
down_revision = "0018_task_push_to_me"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 与 app.skills.router._deleted_name 同规则：name[:64-len(suffix)] + '--del-<id>'
    op.execute(
        sa.text(
            """
            UPDATE skills
            SET name = left(name, 64 - length('--del-' || id::text)) || '--del-' || id
            WHERE deleted_at IS NOT NULL AND name NOT LIKE '%--del-' || id::text
            """
        )
    )


def downgrade() -> None:
    # 不可逆：原名已被覆盖，无法还原
    pass
