"""M31.4 企微存档：外部人/外部群手工别名。

覆盖：PUT rooms/contacts upsert+软删复活+审计；contacts/unknown 待认领清单；
手工群名后 resolve_room_name 零 API 调用（负缓存重试不得覆盖手工名）；
display_names 显示链路能解出别名。
fake 判别沿用已知坑：标量行查询按 "表名.字段" 子串分派，group-by 走 .all()。
"""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.wecom_archive.maps as maps
import app.wecom_archive.router as wrouter
from app.models.user import AuditLog
from app.models.wecom import WecomContact, WecomRoom

pytestmark = pytest.mark.asyncio


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeResult:
    def __init__(self, row=None, rows=None, all_rows=None):
        self._row, self._rows, self._all = row, rows or [], all_rows or []

    def scalar_one_or_none(self):
        return self._row

    def scalars(self):
        return _Scalars(self._rows)

    def all(self):
        return self._all


class _FakeDB:
    def __init__(self, *, room_row=None, contact_row=None, contacts=(), msg_groups=()):
        self.room_row = room_row
        self.contact_row = contact_row
        self.contacts = list(contacts)
        self.msg_groups = list(msg_groups)  # [(userid, 消息数)]，unknown 端点 group-by 结果
        self.added = []
        self.commits = 0

    async def execute(self, stmt, *a, **k):
        s = str(stmt)
        if "wecom_rooms" in s:
            return _FakeResult(row=self.room_row)
        if "wecom_contacts" in s:
            # 坑：全表 select 的列清单也含 "wecom_contacts.userid"——按 userid 等值查要带 " =" 判别
            if "wecom_contacts.userid =" in s:
                return _FakeResult(row=self.contact_row)
            return _FakeResult(rows=self.contacts)
        if "wecom_messages" in s:
            return _FakeResult(all_rows=self.msg_groups)
        return _FakeResult()

    def add(self, r):
        self.added.append(r)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1


_ADMIN = SimpleNamespace(id=1, role="admin")


def _room(roomid="wr1", roomname="", deleted_at=None):
    return SimpleNamespace(roomid=roomid, roomname=roomname, deleted_at=deleted_at, updated_at=datetime.now(UTC))


# ---------- PUT /rooms/{roomid} ----------

class TestRoomRename:
    async def test_create_new_room(self):
        db = _FakeDB()
        r = await wrouter.rename_room("wr9", wrouter.RoomNameIn(name="楼下酸奶总部群"), db=db, admin=_ADMIN)
        row = next(x for x in db.added if isinstance(x, WecomRoom))
        assert row.roomid == "wr9" and row.roomname == "楼下酸奶总部群"
        audit = next(x for x in db.added if isinstance(x, AuditLog))
        assert audit.action == "wecom.room_rename" and audit.target_type == "wecom_room"
        assert r == {"roomid": "wr9", "roomname": "楼下酸奶总部群"}
        assert db.commits == 1

    async def test_rename_existing(self):
        row = _room(roomname="旧名")
        await wrouter.rename_room("wr1", wrouter.RoomNameIn(name="新名"), db=_FakeDB(room_row=row), admin=_ADMIN)
        assert row.roomname == "新名"

    async def test_revive_soft_deleted(self):
        row = _room(deleted_at=datetime.now(UTC))
        db = _FakeDB(room_row=row)
        await wrouter.rename_room("wr1", wrouter.RoomNameIn(name="复活"), db=db, admin=_ADMIN)
        assert row.deleted_at is None and row.roomname == "复活"
        assert not [x for x in db.added if isinstance(x, WecomRoom)]  # 复活不新增行

    async def test_empty_name_400(self):
        with pytest.raises(HTTPException) as ei:
            await wrouter.rename_room("wr1", wrouter.RoomNameIn(name="  "), db=_FakeDB(), admin=_ADMIN)
        assert ei.value.status_code == 400


# ---------- PUT /contacts/{userid} ----------

class TestContactAlias:
    async def test_create_alias(self):
        db = _FakeDB()
        r = await wrouter.alias_contact("wmABC", wrouter.ContactNameIn(name="王经理"), db=db, admin=_ADMIN)
        row = next(x for x in db.added if isinstance(x, WecomContact))
        assert row.userid == "wmABC" and row.name == "王经理"
        audit = next(x for x in db.added if isinstance(x, AuditLog))
        assert audit.action == "wecom.contact_alias"
        assert r == {"userid": "wmABC", "name": "王经理"}

    async def test_rename_existing_contact(self):
        row = SimpleNamespace(userid="wmABC", name="旧", deleted_at=None)
        await wrouter.alias_contact("wmABC", wrouter.ContactNameIn(name="新"), db=_FakeDB(contact_row=row), admin=_ADMIN)
        assert row.name == "新" and row.deleted_at is None

    async def test_display_names_resolves_alias(self):
        """显示链路回归：别名落 wecom_contacts 后 display_names 直接解出。"""
        db = _FakeDB(contacts=[SimpleNamespace(userid="wmABC", name="王经理")])
        assert await maps.display_names(db, ["wmABC", "wmOTHER"]) == {"wmABC": "王经理"}


# ---------- GET /contacts/unknown ----------

class TestUnknownSenders:
    async def test_only_unmapped_sorted_by_count(self):
        db = _FakeDB(
            contacts=[SimpleNamespace(userid="zhangsan", name="张三")],
            msg_groups=[("zhangsan", 5), ("wmA", 3), ("wmB", 10)],
        )
        r = await wrouter.unknown_senders(db=db, _=_ADMIN)
        assert r["items"] == [{"userid": "wmB", "messages": 10}, {"userid": "wmA", "messages": 3}]


# ---------- 手工群名永不被负缓存重试覆盖 ----------

class TestManualRoomNameSafe:
    async def test_named_room_zero_api_call(self, monkeypatch):
        async def _boom(*a):
            raise AssertionError("手工命名的群不应再调 API")

        monkeypatch.setattr(maps, "_access_token", _boom)
        row = _room(roomname="楼下酸奶总部群")
        assert await maps.resolve_room_name(_FakeDB(room_row=row), "corp", "sec", "wr1") == "楼下酸奶总部群"
        assert row.roomname == "楼下酸奶总部群"


# ---------- M31.5 授权查看人开放面：路由依赖矩阵 + viewer 直调回归 ----------

class TestRouteGuardMatrix:
    """把开放矩阵固化成回归：viewer 可读写关键词/映射、只读状态/保留天数；授权管理与保留写仍 admin。"""

    async def test_dependency_matrix(self):
        from app.core.deps import require_admin

        viewer_routes = {
            ("GET", "/api/admin/wecom/status"),
            ("GET", "/api/admin/wecom/keywords"),
            ("POST", "/api/admin/wecom/keywords"),
            ("DELETE", "/api/admin/wecom/keywords/{kid}"),
            ("GET", "/api/admin/wecom/messages"),
            ("GET", "/api/admin/wecom/messages/export"),
            ("GET", "/api/admin/wecom/messages/meta"),
            ("PUT", "/api/admin/wecom/rooms/{roomid}"),
            ("GET", "/api/admin/wecom/contacts/unknown"),
            ("PUT", "/api/admin/wecom/contacts/{userid}"),
            ("GET", "/api/admin/wecom/retention"),
        }
        admin_routes = {
            ("GET", "/api/admin/wecom/viewers"),
            ("POST", "/api/admin/wecom/viewers"),
            ("DELETE", "/api/admin/wecom/viewers/{rid}"),
            ("GET", "/api/admin/wecom/alert-receivers"),
            ("POST", "/api/admin/wecom/alert-receivers"),
            ("DELETE", "/api/admin/wecom/alert-receivers/{rid}"),
            ("PUT", "/api/admin/wecom/retention"),
        }
        seen = {}
        for route in wrouter.router.routes:
            for m in route.methods:
                seen[(m, route.path)] = {d.call for d in route.dependant.dependencies}
        for key in viewer_routes:
            assert key in seen, f"缺路由 {key}"
            assert wrouter.require_wecom_viewer in seen[key], f"{key} 应走 require_wecom_viewer"
            assert require_admin not in seen[key], f"{key} 不应再挂 require_admin"
        for key in admin_routes:
            assert key in seen, f"缺路由 {key}"
            assert require_admin in seen[key], f"{key} 必须保持 require_admin"
        # 全覆盖：除 /access（get_current_user）外不允许矩阵外路由静默新增
        known = viewer_routes | admin_routes | {("GET", "/api/admin/wecom/access")}
        assert set(seen) == known


class TestViewerDirectCalls:
    async def test_viewer_actor_writes_and_audits(self):
        """依赖注入场景直调即放行——测函数体对非 admin actor 无隐含假设，审计留痕到 viewer 本人。"""
        viewer = SimpleNamespace(id=7, role="staff")
        db = _FakeDB()
        await wrouter.create_keyword(wrouter.KeywordIn(word="回扣"), db=db, admin=viewer)
        await wrouter.rename_room("wr9", wrouter.RoomNameIn(name="群"), db=db, admin=viewer)
        await wrouter.alias_contact("wmX", wrouter.ContactNameIn(name="某人"), db=db, admin=viewer)
        audits = [x for x in db.added if isinstance(x, AuditLog)]
        assert {a.action for a in audits} == {"wecom.keyword_create", "wecom.room_rename", "wecom.contact_alias"}
        assert all(a.actor_user_id == 7 for a in audits)
