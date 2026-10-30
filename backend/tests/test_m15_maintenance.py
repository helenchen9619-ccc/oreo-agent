"""M15 逻辑测试：维护模式（is_maintenance_on 缓存 + 开关端点 + get_current_user 拦截）。"""
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.system.router as sys_router
from app.core.deps import get_current_user
from app.models.system import SystemSetting

pytestmark = pytest.mark.asyncio


class _FakeResult:
    def __init__(self, row=None):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _FakeDB:
    def __init__(self, row=None):
        self.row = row
        self.queries = 0
        self.added = []

    async def execute(self, *a, **k):
        self.queries += 1
        return _FakeResult(row=self.row)

    def add(self, r):
        self.added.append(r)

    async def commit(self):
        pass


@pytest.fixture(autouse=True)
def _clear():
    sys_router._clear_cache()
    yield
    sys_router._clear_cache()


def _setting(value):
    s = SystemSetting(key="maintenance", value=value)
    return s


# ---------- is_maintenance_on 与缓存 ----------

class TestMaintenanceRead:
    async def test_no_row_is_off(self):
        assert await sys_router.is_maintenance_on(_FakeDB(row=None)) is False

    async def test_on_and_off(self):
        assert await sys_router.is_maintenance_on(_FakeDB(row="1")) is True  # select(value) 取出的是字符串
        sys_router._clear_cache()
        assert await sys_router.is_maintenance_on(_FakeDB(row="0")) is False

    async def test_cache_ttl_avoids_db_hits(self):
        db = _FakeDB(row="1")
        assert await sys_router.is_maintenance_on(db) is True
        assert await sys_router.is_maintenance_on(db) is True
        assert db.queries == 1  # TTL 内第二次不查库
        sys_router._cache["at"] = time.monotonic() - 10  # 模拟过期
        assert await sys_router.is_maintenance_on(db) is True
        assert db.queries == 2


# ---------- 开关端点 ----------

class TestToggle:
    async def test_create_then_update(self):
        db = _FakeDB(row=None)  # 第一次：无记录 → 新建
        r = await sys_router.set_maintenance(sys_router.MaintenanceIn(enabled=True), db=db, admin=SimpleNamespace(id=1))
        assert r == {"maintenance": True}
        s = next(x for x in db.added if isinstance(x, SystemSetting))
        assert s.key == "maintenance" and s.value == "1"

        db2 = _FakeDB(row=s)  # 第二次：更新已有记录
        r2 = await sys_router.set_maintenance(sys_router.MaintenanceIn(enabled=False), db=db2, admin=SimpleNamespace(id=1))
        assert r2 == {"maintenance": False} and s.value == "0"

    async def test_audit_written(self):
        db = _FakeDB(row=None)
        await sys_router.set_maintenance(sys_router.MaintenanceIn(enabled=True), db=db, admin=SimpleNamespace(id=1))
        audits = [x for x in db.added if getattr(x, "action", "") == "admin.maintenance_toggle"]
        assert len(audits) == 1 and '"enabled": true' in audits[0].detail

    async def test_toggle_clears_cache(self):
        assert await sys_router.is_maintenance_on(_FakeDB(row="0")) is False  # 缓存 off
        db = _FakeDB(row=None)
        await sys_router.set_maintenance(sys_router.MaintenanceIn(enabled=True), db=db, admin=SimpleNamespace(id=1))
        # 开关后缓存已清，下次读取立即查库
        assert await sys_router.is_maintenance_on(_FakeDB(row="1")) is True


# ---------- get_current_user 拦截 ----------

class TestDepsGate:
    def _cred(self):
        return SimpleNamespace(credentials="tok")

    async def test_member_blocked_when_on(self, monkeypatch):
        monkeypatch.setattr("app.core.deps.decode_access_token", lambda t: 7)
        user = SimpleNamespace(id=7, role="member", status="active", deleted_at=None)
        db = _FakeDB(row=None)  # get_current_user 先查用户；这里 row 要按查询类型区分——用多功能 fake

        class _DB(_FakeDB):
            def __init__(self):
                super().__init__()
                self.calls = 0

            async def execute(self, stmt, *a, **k):
                self.calls += 1
                # 第一次查 User，第二次查 SystemSetting
                return _FakeResult(row=user if self.calls == 1 else "1")

        with pytest.raises(HTTPException) as ei:
            await get_current_user(cred=self._cred(), db=_DB())
        assert ei.value.status_code == 503 and ei.value.detail["maintenance"] is True

    async def test_admin_passes_when_on(self, monkeypatch):
        monkeypatch.setattr("app.core.deps.decode_access_token", lambda t: 1)
        admin = SimpleNamespace(id=1, role="admin", status="active")
        r = await get_current_user(cred=self._cred(), db=_FakeDB(row=admin))
        assert r is admin  # admin 直接放行，不再查 system_settings

    async def test_member_passes_when_off(self, monkeypatch):
        monkeypatch.setattr("app.core.deps.decode_access_token", lambda t: 7)
        user = SimpleNamespace(id=7, role="member", status="active")

        class _DB(_FakeDB):
            def __init__(self):
                super().__init__()
                self.calls = 0

            async def execute(self, stmt, *a, **k):
                self.calls += 1
                return _FakeResult(row=user if self.calls == 1 else None)

        r = await get_current_user(cred=self._cred(), db=_DB())
        assert r is user
