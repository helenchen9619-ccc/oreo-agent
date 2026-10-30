"""M12 逻辑测试：更新公告 CRUD（fake DB 直调 handler）+ 轮次上限 100 校验。"""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agents.router import AgentIn
from app.announcements.router import (
    AnnouncementIn,
    AnnouncementUpdate,
    _check_date,
    announcement_status,
    create_announcement,
    delete_announcement,
    list_announcements,
    mark_announcements_read,
    update_announcement,
)
from app.core.deps import require_admin
from app.models.announcement import Announcement


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.added = []
        self.committed = 0

    async def execute(self, *a, **k):
        return _FakeResult(self.rows)

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        # 模拟自增主键
        for r in self.added:
            if getattr(r, "id", None) is None:
                r.id = 99

    async def commit(self):
        self.committed += 1


def _admin():
    return SimpleNamespace(id=1, role="admin", last_read_announcement_id=None)


def _employee():
    return SimpleNamespace(id=2, role="employee", last_read_announcement_id=None)


def _ann(**over):
    base = dict(id=1, title="飞书生态", pub_date="2026-09-03", content="正文", created_at=datetime.now(UTC), deleted_at=None)
    base.update(over)
    return SimpleNamespace(**base)


# ---------- 入参校验 ----------

def test_date_format():
    assert _check_date(" 2026-09-03 ") == "2026-09-03"
    with pytest.raises(Exception):
        _check_date("2026/09/03")
    with pytest.raises(Exception):
        _check_date("20260903")


def test_announcement_in_requires_fields():
    with pytest.raises(ValidationError):
        AnnouncementIn(title="", pub_date="2026-09-03", content="x")
    with pytest.raises(ValidationError):
        AnnouncementIn(title="t", pub_date="2026-09-03", content="")


# ---------- CRUD ----------

@pytest.mark.asyncio
async def test_create_writes_audit_and_returns_row():
    db = _FakeDB()
    r = await create_announcement(AnnouncementIn(title=" 标题 ", pub_date="2026-09-03", content="正文"), db=db, admin=_admin())
    assert r["id"] == 99 and r["title"] == "标题" and r["pub_date"] == "2026-09-03"
    audits = [x for x in db.added if getattr(x, "action", "") == "announcement.create"]
    assert len(audits) == 1
    assert "正文" not in audits[0].detail  # 审计不落正文


@pytest.mark.asyncio
async def test_list_excludes_nothing_but_maps_fields():
    db = _FakeDB(rows=[_ann(), _ann(id=2, title="旧公告", pub_date="2026-08-01")])
    rows = await list_announcements(db=db, _=_employee())  # 普通员工可读
    assert [r["title"] for r in rows] == ["飞书生态", "旧公告"]
    assert rows[0]["created_at"]


@pytest.mark.asyncio
async def test_update_partial_fields():
    a = _ann()
    db = _FakeDB(rows=[a])
    r = await update_announcement(1, AnnouncementUpdate(title="新标题"), db=db, admin=_admin())
    assert r["title"] == "新标题" and r["pub_date"] == "2026-09-03"  # 未传字段不动


@pytest.mark.asyncio
async def test_update_404_and_bad_date():
    db = _FakeDB(rows=[])
    with pytest.raises(Exception) as ei:
        await update_announcement(9, AnnouncementUpdate(title="x"), db=db, admin=_admin())
    assert ei.value.status_code == 404
    db2 = _FakeDB(rows=[_ann()])
    with pytest.raises(Exception) as ei2:
        await update_announcement(1, AnnouncementUpdate(pub_date="2026/09/03"), db=db2, admin=_admin())
    assert ei2.value.status_code == 400


@pytest.mark.asyncio
async def test_delete_is_soft():
    a = _ann()
    db = _FakeDB(rows=[a])
    await delete_announcement(1, db=db, admin=_admin())
    assert a.deleted_at is not None
    with pytest.raises(Exception) as ei:
        await delete_announcement(1, db=_FakeDB(rows=[]), admin=_admin())
    assert ei.value.status_code == 404


# ---------- 权限 ----------

@pytest.mark.asyncio
async def test_require_admin_blocks_employee():
    with pytest.raises(Exception) as ei:
        await require_admin(user=_employee())
    assert ei.value.status_code == 403


# ---------- M27：未读 NEW 标签（服务端已读游标） ----------

@pytest.mark.asyncio
async def test_status_unread_when_never_read():
    db = _FakeDB(rows=[_ann(id=5)])
    r = await announcement_status(db=db, user=_employee())
    assert r == {"latest_id": 5, "unread": True}


@pytest.mark.asyncio
async def test_status_read_when_cursor_covers():
    u = _employee()
    u.last_read_announcement_id = 5
    db = _FakeDB(rows=[_ann(id=5)])
    r = await announcement_status(db=db, user=u)
    assert r == {"latest_id": 5, "unread": False}


@pytest.mark.asyncio
async def test_status_no_announcements():
    r = await announcement_status(db=_FakeDB(rows=[]), user=_employee())
    assert r == {"latest_id": None, "unread": False}


@pytest.mark.asyncio
async def test_mark_read_advances_cursor():
    u = _employee()
    db = _FakeDB(rows=[_ann(id=7)])
    r = await mark_announcements_read(db=db, user=u)
    assert r["latest_id"] == 7 and u.last_read_announcement_id == 7 and db.committed == 1
    # 无公告时游标不动、不提交
    u2 = _employee()
    db2 = _FakeDB(rows=[])
    r2 = await mark_announcements_read(db=db2, user=u2)
    assert r2["latest_id"] is None and u2.last_read_announcement_id is None and db2.committed == 0


@pytest.mark.asyncio
async def test_publisher_not_flagged_unread():
    admin = _admin()
    db = _FakeDB()
    r = await create_announcement(AnnouncementIn(title="t", pub_date="2026-09-21", content="x"), db=db, admin=admin)
    assert admin.last_read_announcement_id == r["id"] == 99


# ---------- 轮次上限 100 ----------

def test_loop_rounds_cap_100():
    assert AgentIn(name="x", max_loop_rounds=100).max_loop_rounds == 100
    with pytest.raises(ValidationError):
        AgentIn(name="x", max_loop_rounds=101)
    assert AgentIn(name="x").max_loop_rounds == 16  # 新数字人默认 16 不变
