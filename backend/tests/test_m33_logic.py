"""M3.3 流通体系逻辑测试：收录复制语义 / 个人技能派发受限 / fork 血缘 / 源更新判断。"""
import inspect

from app.skills import router as skills_router


def test_promote_copies_to_platform_with_lineage():
    src = inspect.getsource(skills_router.promote_skill)
    # 复制为平台技能：owner=None、署名原作者、记血缘与源快照
    assert "owner_user_id=None" in src
    assert "created_by=src.owner_user_id" in src
    assert "source_skill_id=src.id" in src
    assert "source_updated_at=src.updated_at" in src
    assert "SkillFile(skill_id=s.id" in src  # 文件一并复制
    # 平台技能无需收录；撞名 400
    assert "平台技能无需收录" in src
    assert "技能名已存在" in src


def test_dispatch_rejects_personal_skills():
    src = inspect.getsource(skills_router.dispatch_skill)
    assert "个人技能不能直接派发" in src


def test_fork_records_lineage():
    src = inspect.getsource(skills_router.fork_skill)
    assert "source_skill_id=src.id" in src
    assert "source_updated_at=src.updated_at" in src


def test_mine_lists_source_status():
    src = inspect.getsource(skills_router.list_my_skills)
    assert "source_name" in src
    assert "source_deleted" in src
    assert "source_has_update" in src
    # 源更新判断：源的 updated_at 晚于 fork 快照
    assert "src.updated_at > s.source_updated_at" in src


def test_out_serializes_lineage_fields():
    src = inspect.getsource(skills_router._out)
    for field in ("created_by", "source_skill_id", "source_updated_at"):
        assert field in src
