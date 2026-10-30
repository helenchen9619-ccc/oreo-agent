"""M31.1 企微存档：授权查看人门禁 / 消息分页列表 / 授权管理 / 告警接收人可配置。

fake 分派纪律（2026-09-23）：join 查询语句文本同时含授权表名和 "users"——
"wecom_viewers"/"wecom_alert_receivers" 分支必须放在通用 "users" 判断之前；
alerts 的 admin 回落查询用 "role" 区分于 _grant 的按 id 单查。
"""
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.wecom_archive.alerts as alerts
import app.wecom_archive.router as wrouter
from app.models.user import AuditLog
from app.models.wecom import WecomAlertReceiver, WecomViewer

pytestmark = pytest.mark.asyncio


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeResult:
    def __init__(self, row=None, rows=None, scalar=None):
        self._row, self._rows, self._scalar = row, rows or [], scalar

    def scalar_one_or_none(self):
        return self._row

    def scalar_one(self):
        return self._scalar

    def scalars(self):
        return _Scalars(self._rows)

    def all(self):  # join 查询的 (grant, user) 元组行
        return self._rows


class _FakeDB:
    def __init__(
        self,
        *,
        viewer=None,          # wecom_viewers 单查结果（门禁/_grant 去重/_revoke 共用）
        receiver_row=None,    # wecom_alert_receivers 单查结果
        receivers=(),         # wecom_alert_receivers 列表（alerts._alert_recipients）
        users=(),             # 按 id 集查用户（_alert_recipients 的名单解析）
        admins=(),            # role=admin 查询（alerts 回落）
        user=None,            # _grant 的按 id 单查用户
        messages=(),
        total=0,
        contacts=None,        # {userid: name}
        rooms=None,           # {roomid: roomname}
        join_rows=(),         # _list_grants 的 (grant, user) 元组
    ):
        self.viewer = viewer
        self.receiver_row = receiver_row
        self.receivers = list(receivers)
        self.users = list(users)
        self.admins = list(admins)
        self.user = user
        self.messages = list(messages)
        self.total = total
        self.contacts = [SimpleNamespace(userid=u, name=n) for u, n in (contacts or {}).items()]
        self.rooms = [SimpleNamespace(roomid=r, roomname=n) for r, n in (rooms or {}).items()]
        self.join_rows = list(join_rows)
        self.added = []
        self.commits = 0
        self.stmts = []

    async def execute(self, stmt, *a, **k):
        s = str(stmt)
        self.stmts.append(s)
        # 授权表分支必须先于通用 users 分支（join 语句同时含两者）
        if "wecom_alert_receivers" in s:
            if "users" in s:
                return _FakeResult(rows=self.join_rows)
            return _FakeResult(row=self.receiver_row, rows=self.receivers)
        if "wecom_viewers" in s:
            if "users" in s:
                return _FakeResult(rows=self.join_rows)
            return _FakeResult(row=self.viewer)
        if "wecom_messages" in s:
            return _FakeResult(rows=self.messages, scalar=self.total)
        if "wecom_contacts" in s:
            return _FakeResult(rows=self.contacts)
        if "wecom_rooms" in s:
            return _FakeResult(rows=self.rooms)
        if "users" in s:
            # select(User) 的 SELECT 列清单恒含 users.role——判别 admin 列表要看比较式
            if "users.role =" in s:  # alerts 的 admin 回落列表
                return _FakeResult(rows=self.admins)
            return _FakeResult(row=self.user, rows=self.users)
        return _FakeResult()

    def add(self, r):
        self.added.append(r)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1


def _msg(mid, text="你好", sender="zhangsan", receivers='["lisi"]', roomid="", msgtime=1758000000000, msgtype="text"):
    return SimpleNamespace(
        id=mid, msgid=f"m{mid}", action="send", msgtype=msgtype, text=text,
        msgtime=msgtime, sender_userid=sender, receivers=receivers, roomid=roomid,
    )


_ADMIN = SimpleNamespace(id=1, role="admin")
_MEMBER = SimpleNamespace(id=9, role="member")


# ---------- 门禁：require_wecom_viewer ----------

class TestViewerGate:
    async def test_admin_pass(self):
        db = _FakeDB()
        assert await wrouter.require_wecom_viewer(user=_ADMIN, db=db) is _ADMIN
        assert not db.stmts  # admin 直通不查表

    async def test_authorized_viewer_pass(self):
        db = _FakeDB(viewer=SimpleNamespace(user_id=9))
        assert await wrouter.require_wecom_viewer(user=_MEMBER, db=db) is _MEMBER

    async def test_unauthorized_403(self):
        with pytest.raises(HTTPException) as ei:
            await wrouter.require_wecom_viewer(user=_MEMBER, db=_FakeDB())
        assert ei.value.status_code == 403
        assert "未被授权" in ei.value.detail


class TestAccess:
    async def test_admin(self):
        r = await wrouter.wecom_access(db=_FakeDB(), user=_ADMIN)
        assert r == {"admin": True, "viewer": True}

    async def test_member_viewer(self):
        r = await wrouter.wecom_access(db=_FakeDB(viewer=SimpleNamespace()), user=_MEMBER)
        assert r == {"admin": False, "viewer": True}

    async def test_member_plain(self):
        r = await wrouter.wecom_access(db=_FakeDB(), user=_MEMBER)
        assert r == {"admin": False, "viewer": False}


# ---------- 消息列表 ----------

class TestMessages:
    async def test_pagination_and_offset(self):
        db = _FakeDB(messages=[_msg(2), _msg(1)], total=25)
        r = await wrouter.list_messages(db=db, user=_ADMIN, page=2)
        assert (r["total"], r["page"], r["page_size"]) == (25, 2, 20)
        assert [m["id"] for m in r["items"]] == [2, 1]
        assert any("OFFSET" in s for s in db.stmts)
        # 翻页是同次查看的延续：page!=1 不留审计、不 commit
        assert not [x for x in db.added if isinstance(x, AuditLog)]
        assert db.commits == 0

    async def test_audit_on_first_page_no_content(self):
        db = _FakeDB(messages=[_msg(1, text="机密正文abc")], total=1)
        r = await wrouter.list_messages(db=db, user=_ADMIN, sender="张三", keyword="机密")
        audit = next(x for x in db.added if isinstance(x, AuditLog))
        assert audit.action == "wecom.messages_view" and audit.actor_user_id == 1
        detail = json.loads(audit.detail)
        assert detail["total"] == 1 and detail["filters"]["sender"] == "张三"
        assert "机密正文abc" not in audit.detail  # 不落正文
        assert db.commits == 1
        assert r["items"][0]["text"] == "机密正文abc"

    async def test_sender_fuzzy_via_contacts(self):
        db = _FakeDB(messages=[_msg(1)], total=1, contacts={"zhangsan": "张三"})
        await wrouter.list_messages(db=db, user=_ADMIN, sender="张")
        assert any("wecom_contacts" in s and "LIKE" in s for s in db.stmts)  # 姓名模糊解 userid
        assert any("sender_userid IN" in s for s in db.stmts)  # 解出的集合+原值并入

    async def test_room_dm(self):
        db = _FakeDB(messages=[_msg(1, roomid="")], total=1)
        await wrouter.list_messages(db=db, user=_ADMIN, room="dm")
        assert any("wecom_messages.roomid = :roomid_1" in s for s in db.stmts)  # 空串也走参数化

    async def test_room_group(self):
        db = _FakeDB(messages=[_msg(1, roomid="wr1")], total=1, rooms={"wr1": "销售一群"})
        await wrouter.list_messages(db=db, user=_ADMIN, room="group")
        assert any("wecom_messages.roomid != :roomid_1" in s for s in db.stmts)

    async def test_room_name_fuzzy(self):
        db = _FakeDB(messages=[_msg(1, roomid="wr1")], total=1, rooms={"wr1": "销售一群"})
        await wrouter.list_messages(db=db, user=_ADMIN, room="wr1")
        assert any("wecom_rooms" in s and "LIKE" in s for s in db.stmts)
        assert any("wecom_messages.roomid IN" in s for s in db.stmts)

    async def test_keyword_and_time_window(self):
        from zoneinfo import ZoneInfo

        db = _FakeDB(messages=[_msg(1)], total=1)
        start = datetime(2026, 9, 22, tzinfo=ZoneInfo("Asia/Shanghai"))
        end = datetime(2026, 9, 23, 23, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
        await wrouter.list_messages(db=db, user=_ADMIN, keyword="报价", start=start, end=end)
        assert any("wecom_messages.text LIKE" in s for s in db.stmts)
        assert any("msgtime >=" in s for s in db.stmts)
        assert any("msgtime <=" in s for s in db.stmts)

    async def test_name_room_mapping_filled(self):
        db = _FakeDB(
            messages=[_msg(1, sender="zhangsan", receivers='["lisi","wangwu"]', roomid="wr1")],
            total=1,
            contacts={"zhangsan": "张三", "lisi": "李四"},
            rooms={"wr1": "销售一群"},
        )
        r = await wrouter.list_messages(db=db, user=_ADMIN)
        m = r["items"][0]
        assert m["sender_name"] == "张三" and m["room_name"] == "销售一群"
        assert m["receivers"] == [{"userid": "lisi", "name": "李四"}, {"userid": "wangwu", "name": ""}]

    async def test_meta_rooms(self):
        db = _FakeDB(rooms={"wr1": "销售一群", "wr2": ""})
        r = await wrouter.messages_meta(db=db, _=_ADMIN)
        assert r["rooms"] == [{"roomid": "wr1", "roomname": "销售一群"}, {"roomid": "wr2", "roomname": ""}]


# ---------- 授权管理 ----------

class TestGrants:
    def _user(self, **over):
        base = dict(id=9, name="王五", username="wangwu", feishu_open_id=None, status="active")
        base.update(over)
        return SimpleNamespace(**base)

    async def test_grant_viewer_create(self):
        db = _FakeDB(user=self._user())
        r = await wrouter.grant_viewer(wrouter.GrantIn(user_id=9), db=db, admin=_ADMIN)
        v = next(x for x in db.added if isinstance(x, WecomViewer))
        assert v.user_id == 9 and r["user_id"] == 9
        assert any(getattr(x, "action", "") == "wecom.viewer_grant" for x in db.added)
        assert db.commits == 1

    async def test_grant_viewer_dup_409(self):
        db = _FakeDB(user=self._user(), viewer=SimpleNamespace(user_id=9, deleted_at=None))
        with pytest.raises(HTTPException) as ei:
            await wrouter.grant_viewer(wrouter.GrantIn(user_id=9), db=db, admin=_ADMIN)
        assert ei.value.status_code == 409

    async def test_grant_viewer_revive(self):
        row = SimpleNamespace(id=7, user_id=9, deleted_at=datetime(2026, 9, 1))
        db = _FakeDB(user=self._user(), viewer=row)
        await wrouter.grant_viewer(wrouter.GrantIn(user_id=9), db=db, admin=_ADMIN)
        assert row.deleted_at is None  # 复活软删行而非新建
        assert not [x for x in db.added if isinstance(x, WecomViewer)]

    async def test_grant_user_404(self):
        with pytest.raises(HTTPException) as ei:
            await wrouter.grant_viewer(wrouter.GrantIn(user_id=99), db=_FakeDB(), admin=_ADMIN)
        assert ei.value.status_code == 404

    async def test_receiver_requires_feishu_400(self):
        db = _FakeDB(user=self._user())  # 未绑飞书
        with pytest.raises(HTTPException) as ei:
            await wrouter.add_receiver(wrouter.GrantIn(user_id=9), db=db, admin=_ADMIN)
        assert ei.value.status_code == 400
        assert "未绑定飞书" in ei.value.detail

    async def test_receiver_create(self):
        db = _FakeDB(user=self._user(feishu_open_id="ou_x"))
        await wrouter.add_receiver(wrouter.GrantIn(user_id=9), db=db, admin=_ADMIN)
        rc = next(x for x in db.added if isinstance(x, WecomAlertReceiver))
        assert rc.user_id == 9
        assert any(getattr(x, "action", "") == "wecom.receiver_add" for x in db.added)

    async def test_revoke_soft_delete_and_audit(self):
        row = SimpleNamespace(id=3, user_id=9, deleted_at=None)
        db = _FakeDB(viewer=row)
        await wrouter.revoke_viewer(3, db=db, admin=_ADMIN)
        assert row.deleted_at is not None
        assert any(getattr(x, "action", "") == "wecom.viewer_revoke" for x in db.added)

    async def test_revoke_404(self):
        with pytest.raises(HTTPException) as ei:
            await wrouter.revoke_viewer(3, db=_FakeDB(), admin=_ADMIN)
        assert ei.value.status_code == 404

    async def test_list_grants_feishu_bound(self):
        grant = SimpleNamespace(id=1, created_at=datetime(2026, 9, 23, 10, 0))
        user = SimpleNamespace(id=9, name="王五", username="wangwu", feishu_open_id="ou_x")
        db = _FakeDB(join_rows=[(grant, user)])
        r = await wrouter.list_viewers(db=db, _=_ADMIN)
        assert r[0]["user"]["feishu_bound"] is True
        assert "open_id" not in json.dumps(r)  # open_id 不出后端
        assert r[0]["user"]["name"] == "王五" and r[0]["created_at"].startswith("2026-09-23")


# ---------- 告警接收人解析 ----------

class TestAlertRecipients:
    async def test_empty_table_fallback_admins(self):
        admins = [SimpleNamespace(id=1, feishu_open_id="ou_a")]
        db = _FakeDB(admins=admins)
        assert await alerts._alert_recipients(db) == admins

    async def test_configured_receivers_used(self):
        receivers = [SimpleNamespace(user_id=9)]
        users = [SimpleNamespace(id=9, feishu_open_id="ou_r")]
        admins = [SimpleNamespace(id=1, feishu_open_id="ou_a")]
        db = _FakeDB(receivers=receivers, users=users, admins=admins)
        r = await alerts._alert_recipients(db)
        assert [u.id for u in r] == [9]  # 只推名单内，不回落 admin

    async def test_configured_but_none_pushable_fallback(self):
        receivers = [SimpleNamespace(user_id=9)]
        admins = [SimpleNamespace(id=1, feishu_open_id="ou_a")]
        db = _FakeDB(receivers=receivers, users=[], admins=admins)  # 名单无人能推
        assert await alerts._alert_recipients(db) == admins  # 宁可多推不可漏推


# ---------- Layout.jsx TDZ 白屏守护（2026-09-23 事故：全部非 admin 白屏） ----------

class TestLayoutTdz:
    def test_layout_declares_wecom_viewer_before_nav_filter(self):
        """源码断言：渲染期引用的 state 必须先声明——esbuild 不查 TDZ，
        admin 因 isAdmin|| 短路正常、非 admin 必崩，build 绿≠运行安全。"""
        from pathlib import Path

        src = (Path(__file__).parent.parent.parent / "frontend" / "src" / "components" / "Layout.jsx").read_text(encoding="utf-8")
        assert src.index("const [wecomViewer") < src.index("items = NAV.filter"), "wecomViewer 必须先声明再被 NAV.filter 渲染期引用（TDZ 白屏教训）"
