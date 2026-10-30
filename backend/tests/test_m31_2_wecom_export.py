"""M31.2 企微存档：消息导出 xlsx / 保留策略（配置+清扫）/ 48009 通讯录失败加固。

fake 分派沿用三个已记录的坑：授权/设置分支先于通用分支；"users.role =" 判别式；
空串比较渲染参数化。sync 测试不碰真实 SDK/RSA（注入假 SDK + monkeypatch）。
"""
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.wecom_archive.router as wrouter
import app.wecom_archive.sync as sync
from app.models.system import SystemSetting
from app.models.user import AuditLog

pytestmark = pytest.mark.asyncio


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeResult:
    def __init__(self, row=None, rows=None, scalar=None, rowcount=None):
        self._row, self._rows, self._scalar = row, rows or [], scalar
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._row

    def scalar_one(self):
        return self._scalar

    def scalars(self):
        return _Scalars(self._rows)


class _FakeDB:
    def __init__(self, *, messages=(), total=0, contacts=None, rooms=None, setting_row=None, delete_rowcount=0):
        self.messages = list(messages)
        self.total = total
        self.contacts = [SimpleNamespace(userid=u, name=n) for u, n in (contacts or {}).items()]
        self.rooms = [SimpleNamespace(roomid=r, roomname=n) for r, n in (rooms or {}).items()]
        self.setting_row = setting_row
        self.delete_rowcount = delete_rowcount
        self.added = []
        self.commits = 0
        self.stmts = []
        self.deleted = False

    async def execute(self, stmt, *a, **k):
        s = str(stmt)
        self.stmts.append(s)
        # 坑：deleted_at 子串含 "DELETE"——判别真删除语句要用 startswith
        if s.lstrip().upper().startswith("DELETE") and "wecom_messages" in s:
            self.deleted = True
            return _FakeResult(rowcount=self.delete_rowcount)
        if "system_settings" in s:
            # 坑：select(SystemSetting.value) 标量查询要回 value 字符串，整行查询才回行对象
            if "system_settings.id" not in s:
                return _FakeResult(row=self.setting_row.value if self.setting_row else None)
            return _FakeResult(row=self.setting_row)
        if "wecom_contacts" in s:
            return _FakeResult(rows=self.contacts)
        if "wecom_rooms" in s:
            return _FakeResult(rows=self.rooms)
        if "wecom_messages" in s:
            if "max(" in s.lower():  # status 的最近入库时间
                return _FakeResult(scalar=None)
            return _FakeResult(rows=self.messages, scalar=self.total)
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


async def _read_body(resp) -> bytes:
    return b"".join([c async for c in resp.body_iterator])


# ---------- 消息导出 ----------

class TestExport:
    async def test_export_headers_rows_and_audit(self):
        db = _FakeDB(
            messages=[_msg(1, text="机密正文abc", roomid="wr1"), _msg(2, sender="lisi")],
            total=2,
            contacts={"zhangsan": "张三", "lisi": "李四"},
            rooms={"wr1": "销售一群"},
        )
        resp = await wrouter.export_messages(db=db, user=_ADMIN)
        assert resp.media_type.endswith("spreadsheetml.sheet")
        cd = resp.headers["content-disposition"]
        assert "filename*=utf-8''" in cd and ".xlsx" in cd  # RFC 5987 中文文件名

        from io import BytesIO

        from openpyxl import load_workbook

        ws = load_workbook(BytesIO(await _read_body(resp))).active
        rows = list(ws.iter_rows(values_only=True))
        assert rows[0] == ("时间", "发送人", "会话", "类型", "内容", "接收人")
        assert len(rows) == 3
        r1 = next(r for r in rows[1:] if r[1] == "张三")
        assert r1[2] == "销售一群" and r1[4] == "机密正文abc" and r1[5] == "李四"

        audit = next(x for x in db.added if isinstance(x, AuditLog))
        assert audit.action == "wecom.messages_export"
        detail = json.loads(audit.detail)
        assert detail["total"] == 2
        assert "机密正文abc" not in audit.detail  # 不落正文
        assert db.commits == 1

    async def test_export_over_limit_400(self):
        db = _FakeDB(total=wrouter._EXPORT_MAX + 1)
        with pytest.raises(HTTPException) as ei:
            await wrouter.export_messages(db=db, user=_ADMIN)
        assert ei.value.status_code == 400 and "缩小" in ei.value.detail
        assert not [x for x in db.added if isinstance(x, AuditLog)]  # 拒绝不留审计

    async def test_export_follows_filters(self):
        db = _FakeDB(messages=[_msg(1)], total=1, contacts={"zhangsan": "张三"})
        await wrouter.export_messages(
            db=db, user=_ADMIN, sender="张",
            start=datetime(2026, 9, 22, tzinfo=UTC), end=datetime(2026, 9, 23, tzinfo=UTC),
        )
        assert any("wecom_contacts" in s and "LIKE" in s for s in db.stmts)
        assert any("msgtime >=" in s and "msgtime <=" in s for s in db.stmts)

    async def test_export_empty_result_ok(self):
        resp = await wrouter.export_messages(db=_FakeDB(), user=_ADMIN)
        assert resp.media_type.endswith("spreadsheetml.sheet")


# ---------- 保留策略 ----------

class TestRetention:
    async def test_get_default_zero(self):
        assert await wrouter.get_retention(db=_FakeDB(), _=_ADMIN) == {"days": 0}

    async def test_set_creates_row_and_audits(self):
        db = _FakeDB()
        r = await wrouter.set_retention(wrouter.RetentionIn(days=90), db=db, admin=_ADMIN)
        row = next(x for x in db.added if isinstance(x, SystemSetting))
        assert row.key == "wecom_retention_days" and row.value == "90"
        assert r == {"days": 90}
        assert any(getattr(x, "action", "") == "wecom.retention_set" for x in db.added)
        assert db.commits == 1

    async def test_set_updates_existing(self):
        row = SystemSetting(key="wecom_retention_days", value="30")
        db = _FakeDB(setting_row=row)
        await wrouter.set_retention(wrouter.RetentionIn(days=0), db=db, admin=_ADMIN)
        assert row.value == "0"  # 0=永久
        assert not [x for x in db.added if isinstance(x, SystemSetting)]

    async def test_set_validation_rejects_out_of_range(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            wrouter.RetentionIn(days=-1)
        with pytest.raises(ValidationError):
            wrouter.RetentionIn(days=3651)

    async def test_get_retention_days_bad_value_safe(self):
        db = _FakeDB(setting_row=SystemSetting(key="wecom_retention_days", value="abc"))
        assert await sync.get_retention_days(db) == 0  # 非法值按永久处理，绝不错删

    async def test_sweep_zero_days_noop(self):
        db = _FakeDB()
        assert await sync.sweep_expired_messages(db) == 0
        assert not db.deleted and db.commits == 0

    async def test_sweep_deletes_expired_by_msgtime(self):
        db = _FakeDB(setting_row=SystemSetting(key="wecom_retention_days", value="30"), delete_rowcount=7)
        assert await sync.sweep_expired_messages(db) == 7
        assert db.deleted
        assert any("msgtime <" in s for s in db.stmts)  # 按 msgtime 阈值删（索引现成）
        assert db.commits == 1


# ---------- 48009 加固：失败快速重试 + 状态可见 ----------

class _FakeSdk:
    def __init__(self, pages):
        self.pages = list(pages)

    def get_chat_data(self, seq, limit=100):
        return self.pages.pop(0) if self.pages else []

    def decrypt_data(self, rand_key, enc):
        return json.loads(enc)

    def close(self):
        pass


def _settings(**over):
    base = dict(
        WECOM_CORPID="corp", WECOM_ARCHIVE_SECRET="sec", WECOM_ARCHIVE_KEY_PATH="k.pem",
        WECOM_ARCHIVE_PUBKEY_VER="2", WECOM_ARCHIVE_SO_PATH="sdk.so", WECOM_CONTACT_SECRET="csec",
        WECOM_ARCHIVE_SYNC_ENABLED=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _reset_sync_state():
    sync._last_contact_refresh = None
    sync._contact_status.update({"at": None, "ok": None, "error": ""})
    sync._priv_key_cache.clear()
    yield


@pytest.fixture
def _patch_sync(monkeypatch):
    monkeypatch.setattr(sync, "get_settings", lambda: _settings())
    monkeypatch.setattr(sync, "_private_key_pem", lambda path: b"pem")
    monkeypatch.setattr(sync, "decrypt_random_key", lambda pem, b64: b"rand")


class TestContactRetry:
    async def test_failure_retries_in_10min_and_status_visible(self, monkeypatch, _patch_sync):
        async def _boom(*a):
            raise RuntimeError("通讯录 user/list 失败 errcode=48009")

        monkeypatch.setattr(sync, "refresh_contacts", _boom)
        await sync.sync_wecom_archive(_FakeDB(), sdk=_FakeSdk(pages=[[]]))

        assert sync._contact_status["ok"] is False
        assert "48009" in sync._contact_status["error"] and sync._contact_status["at"]
        # 失败置位 = now - 6h + 10min → 距现在约 5h50m（成功才会置成 now 走满 6 小时节流）
        delta = datetime.now(UTC) - sync._last_contact_refresh
        assert timedelta(hours=5, minutes=49) < delta < timedelta(hours=6)

    async def test_success_status_and_throttle(self, monkeypatch, _patch_sync):
        calls = []

        async def _ok(db, corpid, secret):
            calls.append(1)
            return 3

        monkeypatch.setattr(sync, "refresh_contacts", _ok)
        await sync.sync_wecom_archive(_FakeDB(), sdk=_FakeSdk(pages=[[]]))
        assert sync._contact_status["ok"] is True and sync._contact_status["error"] == ""
        assert datetime.now(UTC) - sync._last_contact_refresh < timedelta(minutes=1)
        await sync.sync_wecom_archive(_FakeDB(), sdk=_FakeSdk(pages=[[]]))  # 6h 内第二轮不刷新
        assert len(calls) == 1


class TestStatusContactSync:
    async def test_status_includes_contact_sync(self, _patch_sync):
        sync._contact_status.update({"at": "2026-09-23T10:00:00+00:00", "ok": False, "error": "errcode=48009"})
        r = await wrouter.wecom_status(db=_FakeDB(), _=_ADMIN)
        assert r["contact_sync"] == {"at": "2026-09-23T10:00:00+00:00", "ok": False, "error": "errcode=48009"}
