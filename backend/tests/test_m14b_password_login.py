"""M14b 逻辑测试：账号密码登录（哈希/校验纯函数 + password-login 端点 + 建号/改密 + 防爆破锁定）。

风格同既有测试：无 TestClient，_FakeDB 直调 handler，不碰真实 DB/网络。
"""
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.auth.org_router as org_router
import app.auth.router as auth_router
from app.core.security import hash_password, verify_password
from app.models.user import User

pytestmark = pytest.mark.asyncio


class _FakeResult:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._row

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, row=None):
        self.row = row
        self.added = []

    async def execute(self, *a, **k):
        return _FakeResult(row=self.row)

    def add(self, r):
        self.added.append(r)

    async def flush(self):
        pass

    async def commit(self):
        pass


def _user(uid=1, username="test01", status="active", password=None):
    u = User()
    u.id = uid
    u.username = username
    u.name = "测试员工"
    u.role = "member"
    u.status = status
    u.password_hash = hash_password(password) if password else None
    return u


@pytest.fixture(autouse=True)
def _clear_lockout():
    auth_router._login_fails.clear()
    yield
    auth_router._login_fails.clear()


# ---------- 哈希纯函数 ----------

class TestPasswordHash:
    def test_roundtrip(self):
        h = hash_password("abc123")
        assert verify_password("abc123", h) is True
        assert verify_password("abc124", h) is False

    def test_random_salt(self):
        assert hash_password("abc123") != hash_password("abc123")

    def test_bad_stored_always_false(self):
        assert verify_password("x", None) is False
        assert verify_password("x", "") is False
        assert verify_password("x", "garbage") is False
        assert verify_password("x", "bcrypt$1$2$3") is False


# ---------- password-login 端点 ----------

class TestPasswordLogin:
    async def test_success(self):
        r = await auth_router.password_login(
            auth_router.PasswordLoginIn(username="test01", password="abc123"),
            db=_FakeDB(row=_user(password="abc123")),
        )
        assert r.token and r.user["username"] == "test01"

    async def test_uniform_error_unknown_user(self):
        with pytest.raises(HTTPException) as ei:
            await auth_router.password_login(
                auth_router.PasswordLoginIn(username="ghost", password="abc123"), db=_FakeDB(row=None))
        assert ei.value.status_code == 400 and ei.value.detail == "用户名或密码错误"

    async def test_uniform_error_wrong_password(self):
        with pytest.raises(HTTPException) as ei:
            await auth_router.password_login(
                auth_router.PasswordLoginIn(username="test01", password="wrong1"), db=_FakeDB(row=_user(password="abc123")))
        assert ei.value.status_code == 400 and ei.value.detail == "用户名或密码错误"

    async def test_no_password_account_same_error(self):
        # 纯飞书账号（password_hash 为 NULL）不能用密码登录，文案不泄露区别
        with pytest.raises(HTTPException) as ei:
            await auth_router.password_login(
                auth_router.PasswordLoginIn(username="test01", password="abc123"), db=_FakeDB(row=_user(password=None)))
        assert ei.value.status_code == 400 and ei.value.detail == "用户名或密码错误"

    async def test_disabled_403(self):
        with pytest.raises(HTTPException) as ei:
            await auth_router.password_login(
                auth_router.PasswordLoginIn(username="test01", password="abc123"),
                db=_FakeDB(row=_user(status="disabled", password="abc123")))
        assert ei.value.status_code == 403

    async def test_flag_off_403(self, monkeypatch):
        monkeypatch.setattr(auth_router.settings, "PASSWORD_LOGIN_ENABLED", False)
        with pytest.raises(HTTPException) as ei:
            await auth_router.password_login(
                auth_router.PasswordLoginIn(username="test01", password="abc123"), db=_FakeDB())
        assert ei.value.status_code == 403

    async def test_lockout_after_5_fails(self):
        db = _FakeDB(row=_user(password="abc123"))
        for _ in range(auth_router._LOGIN_FAIL_LIMIT):
            with pytest.raises(HTTPException):
                await auth_router.password_login(
                    auth_router.PasswordLoginIn(username="test01", password="wrong1"), db=db)
        # 第 6 次连正确密码也进不去
        with pytest.raises(HTTPException) as ei:
            await auth_router.password_login(
                auth_router.PasswordLoginIn(username="test01", password="abc123"), db=db)
        assert ei.value.status_code == 429

    async def test_old_fails_expire(self):
        # 窗口外的失败不计入锁定
        auth_router._login_fails["test01"] = [time.monotonic() - 700] * 10
        r = await auth_router.password_login(
            auth_router.PasswordLoginIn(username="test01", password="abc123"),
            db=_FakeDB(row=_user(password="abc123")))
        assert r.token

    async def test_success_clears_fail_counter(self):
        db = _FakeDB(row=_user(password="abc123"))
        for _ in range(4):
            with pytest.raises(HTTPException):
                await auth_router.password_login(
                    auth_router.PasswordLoginIn(username="test01", password="wrong1"), db=db)
        r = await auth_router.password_login(
            auth_router.PasswordLoginIn(username="test01", password="abc123"), db=db)
        assert r.token and "test01" not in auth_router._login_fails


# ---------- 建号/改密（org_router） ----------

class TestAdminPasswordOps:
    def test_validate_password(self):
        assert org_router.validate_password(None) is None
        assert org_router.validate_password("   ") is None
        assert org_router.validate_password("abc123") == "abc123"
        with pytest.raises(HTTPException):
            org_router.validate_password("12345")  # 少于 6 位

    async def test_create_with_password(self):
        db = _FakeDB(row=None)  # 用户名查重 → 不存在
        body = org_router.UserCreateIn(username="test02", name="测试2", role="member", password="abc123")
        r = await org_router.admin_create_user(body, db=db, admin=SimpleNamespace(id=99))
        assert r["username"] == "test02"
        u = next(x for x in db.added if isinstance(x, User))
        assert verify_password("abc123", u.password_hash)
        audit = next(x for x in db.added if getattr(x, "action", "") == "admin.user_create")
        assert "abc123" not in audit.detail  # 审计不落密码

    async def test_create_without_password(self):
        db = _FakeDB(row=None)
        body = org_router.UserCreateIn(username="test03", name="测试3", role="member")
        await org_router.admin_create_user(body, db=db, admin=SimpleNamespace(id=99))
        u = next(x for x in db.added if isinstance(x, User))
        assert u.password_hash is None

    async def test_patch_reset_and_clear_password(self):
        u = _user(password="old123")
        body = org_router.UserPatchIn(password="new456", set_password=True)
        db2 = _FakeDB(row=u)  # admin_update_user 按 id 查用户 → 命中
        await org_router.admin_update_user(u.id, body, db=db2, admin=SimpleNamespace(id=99))
        assert verify_password("new456", u.password_hash) and not verify_password("old123", u.password_hash)
        audit = next(x for x in db2.added if getattr(x, "action", "") == "admin.user_update")
        assert "new456" not in audit.detail and "reset" in audit.detail

        db3 = _FakeDB(row=u)
        await org_router.admin_update_user(u.id, org_router.UserPatchIn(password="", set_password=True), db=db3, admin=SimpleNamespace(id=99))
        assert u.password_hash is None
