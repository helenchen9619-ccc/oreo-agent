"""M33 领用副本自助升级：pull-update 覆盖+自动备份 / rollback-upgrade 回滚+重做 / mine 的 has_backup 标记。

fake DB 直调 handler（范式同 test_m12_announcements.py）：execute 按序弹出预置查询结果。
"""
import inspect
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.agent import Skill, SkillFile
from app.skills.router import (
    _backup_name,
    _overwrite_files,
    _snapshot_copy,
    list_my_skills,
    pull_update_skill,
    rollback_upgrade_skill,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)  # fork 时源快照
T1 = datetime(2026, 2, 1, tzinfo=UTC)  # 源更新后
T2 = datetime(2026, 3, 1, tzinfo=UTC)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows if isinstance(rows, list) else [rows]

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, results=None):
        self._q = list(results or [])
        self.added = []
        self.deleted = []
        self.executed = []
        self.commits = 0

    async def execute(self, stmt, *a, **k):
        self.executed.append(str(stmt))
        return _FakeResult(self._q.pop(0) if self._q else [])

    def add(self, row):
        self.added.append(row)

    async def delete(self, row):
        self.deleted.append(row)

    async def flush(self):
        for i, r in enumerate(self.added):
            if getattr(r, "id", None) is None:
                r.id = 1000 + i

    async def commit(self):
        self.commits += 1


USER = SimpleNamespace(id=7, role="member", name="张三")


def _copy(**kw):
    base = dict(
        id=5, name="sales-x", description="我的描述", category="sales", instructions="# old",
        owner_user_id=7, enabled=True, created_by=7, source_skill_id=1, source_updated_at=T0, deleted_at=None,
    )
    base.update(kw)
    return Skill(**base)


def _src(**kw):
    base = dict(
        id=1, name="sales", description="平台版", category="sales", instructions="# v2",
        owner_user_id=None, enabled=True, created_by=None, source_skill_id=None,
        source_updated_at=None, updated_at=T1, deleted_at=None,
    )
    base.update(kw)
    return Skill(**base)


def _backup_skill(**kw):
    base = dict(
        id=77, name="sales-x--bak-5", description="我的描述", category="sales", instructions="# old",
        owner_user_id=7, enabled=True, created_by=7, source_skill_id=5, source_updated_at=T0, deleted_at=T2,
    )
    base.update(kw)
    return Skill(**base)


# ---------- 小工具 ----------

def test_backup_name_truncates_like_deleted_name():
    assert _backup_name(_copy()) == "sales-x--bak-5"
    long_copy = _copy(name="a" * 64)
    renamed = _backup_name(long_copy)
    assert len(renamed) == 64 and renamed.endswith("--bak-5")


# ---------- pull-update ----------

@pytest.mark.asyncio
async def test_pull_update_overwrites_content_and_snapshots_backup():
    copy, src = _copy(), _src()
    db = _FakeDB([
        [copy],  # 副本
        [src],  # 源技能
        [],  # 无旧备份
        [SkillFile(skill_id=5, path="SKILL.md", content="# old")],  # 副本旧文件（进备份）
        [SkillFile(skill_id=1, path="SKILL.md", content="# v2"), SkillFile(skill_id=1, path="scripts/a.py", content="print(1)")],  # 源文件
    ])
    out = await pull_update_skill(5, USER, db)
    # 内容被源覆盖 + 版本游标刷新；副本自己的名字/描述保留
    assert copy.instructions == "# v2"
    assert copy.source_updated_at == T1
    assert copy.name == "sales-x" and copy.description == "我的描述"
    assert out["id"] == 5
    # 备份行：软删态、血缘指向副本、带走旧版本游标
    baks = [r for r in db.added if isinstance(r, Skill)]
    assert len(baks) == 1
    bak = baks[0]
    assert bak.name == "sales-x--bak-5" and bak.deleted_at is not None
    assert bak.source_skill_id == 5 and bak.source_updated_at == T0
    assert bak.owner_user_id == 7
    # 备份文件 = 旧内容；副本新文件 = 源全量（含附件）
    bak_files = [r for r in db.added if isinstance(r, SkillFile) and r.skill_id == bak.id]
    assert [(f.path, f.content) for f in bak_files] == [("SKILL.md", "# old")]
    new_files = [r for r in db.added if isinstance(r, SkillFile) and r.skill_id == 5]
    assert [(f.path, f.content) for f in new_files] == [("SKILL.md", "# v2"), ("scripts/a.py", "print(1)")]
    # 副本旧文件是硬删（规避 uq_skill_file 纯唯一约束），不是软删
    assert any("DELETE" in s.upper() and "skill_files" in s for s in db.executed)
    assert db.commits == 1


@pytest.mark.asyncio
async def test_pull_update_replaces_old_backup_no_name_clash():
    copy, src, old_bak = _copy(), _src(), _backup_skill()
    db = _FakeDB([
        [copy], [src],
        [old_bak],  # 已有一代备份 → 先硬删
        [],  # 硬删旧备份文件的 delete 语句（不占数据位）
        [],  # 副本当前文件
        [SkillFile(skill_id=1, path="SKILL.md", content="# v2")],
    ])
    await pull_update_skill(5, USER, db)
    assert db.deleted == [old_bak]  # 旧备份被硬删，单级备份
    new_baks = [r for r in db.added if isinstance(r, Skill)]
    assert len(new_baks) == 1  # 新备份顶替，不累积


@pytest.mark.asyncio
async def test_pull_update_error_paths():
    # 副本不存在 / 不属于我
    with pytest.raises(HTTPException) as e:
        await pull_update_skill(5, USER, _FakeDB([[]]))
    assert e.value.status_code == 404
    # 非 fork 的原创个人技能
    with pytest.raises(HTTPException) as e:
        await pull_update_skill(5, USER, _FakeDB([[_copy(source_skill_id=None)]]))
    assert e.value.status_code == 400 and "领用" in e.value.detail
    # 源已软删
    with pytest.raises(HTTPException) as e:
        await pull_update_skill(5, USER, _FakeDB([[_copy()], [_src(deleted_at=T1)]]))
    assert e.value.status_code == 409 and "删除" in e.value.detail
    # 源不存在（硬删等极端情况）同样 409
    with pytest.raises(HTTPException) as e:
        await pull_update_skill(5, USER, _FakeDB([[_copy()], []]))
    assert e.value.status_code == 409
    # 已是最新
    with pytest.raises(HTTPException) as e:
        await pull_update_skill(5, USER, _FakeDB([[_copy()], [_src(updated_at=T0)]]))
    assert e.value.status_code == 400 and "最新" in e.value.detail


# ---------- rollback-upgrade ----------

@pytest.mark.asyncio
async def test_rollback_restores_backup_and_keeps_redo():
    copy = _copy(instructions="# v2", source_updated_at=T1)  # 已升级后的状态
    bak = _backup_skill()
    db = _FakeDB([
        [copy],
        [bak],  # rollback 的 _find_backups
        [SkillFile(skill_id=77, path="SKILL.md", content="# old")],  # 备份文件（读进内存）
        [bak],  # _snapshot_copy 的 _find_backups（旧备份被硬删）
        [],  # 硬删旧备份文件的 delete 语句（不占数据位）
        [SkillFile(skill_id=5, path="SKILL.md", content="# v2")],  # 副本当前文件（存成 redo 备份）
    ])
    await rollback_upgrade_skill(5, USER, db)
    # 内容与版本游标回到升级前
    assert copy.instructions == "# old"
    assert copy.source_updated_at == T0
    # 旧备份行被硬删；当前状态转存为新备份（redo）
    assert db.deleted == [bak]
    redo = [r for r in db.added if isinstance(r, Skill)]
    assert len(redo) == 1 and redo[0].instructions == "# v2" and redo[0].source_updated_at == T1
    redo_files = [r for r in db.added if isinstance(r, SkillFile) and r.skill_id == redo[0].id]
    assert [(f.path, f.content) for f in redo_files] == [("SKILL.md", "# v2")]
    assert db.commits == 1


@pytest.mark.asyncio
async def test_rollback_error_paths():
    with pytest.raises(HTTPException) as e:
        await rollback_upgrade_skill(5, USER, _FakeDB([[]]))
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        await rollback_upgrade_skill(5, USER, _FakeDB([[_copy()], []]))  # 无备份
    assert e.value.status_code == 404 and "备份" in e.value.detail


# ---------- /mine 的 has_backup ----------

@pytest.mark.asyncio
async def test_mine_marks_has_backup():
    copy, plain, src = _copy(), _copy(id=6, name="original", source_skill_id=None, source_updated_at=None), _src()
    db = _FakeDB([
        [copy, plain],  # 我的技能
        [src],  # 源
        [5],  # 备份的 source_skill_id 集合
    ])
    out = await list_my_skills(USER, db)
    crow = next(x for x in out if x["id"] == 5)
    prow = next(x for x in out if x["id"] == 6)
    assert crow["has_backup"] is True and crow["source_has_update"] is True
    assert "has_backup" not in prow  # 非 fork 技能不带备份标记


# ---------- 源码断言（守护关键不变式） ----------

def test_overwrite_hard_deletes_files():
    # uq_skill_file 是纯唯一约束：必须硬删，软删后同名 INSERT 会撞 500
    src = inspect.getsource(_overwrite_files)
    assert "delete(SkillFile)" in src
    assert "deleted_at" not in src


def test_snapshot_is_soft_deleted_and_lineage_points_to_copy():
    src = inspect.getsource(_snapshot_copy)
    assert "deleted_at=utcnow()" in src  # 软删态：市场/我的技能/prompt 名册不可见
    assert "source_skill_id=copy.id" in src
    assert "source_updated_at=copy.source_updated_at" in src


def test_pull_update_ownership_and_guards():
    src = inspect.getsource(pull_update_skill)
    assert "Skill.owner_user_id == user.id" in src
    assert "只有领用的副本才能升级" in src
    assert "源技能已被删除" in src
    assert "已是最新版本" in src
