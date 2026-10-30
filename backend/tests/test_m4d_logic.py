"""M4d 逻辑测试：api_request 工具（白名单/认证注入/登录换 cookie/串行限频）+ 管理端校验纯函数。"""
import json
from types import SimpleNamespace

import httpx
import pytest

from app.runtime import tools
from app.runtime.tools import API_TOOL_SCHEMAS, ToolContext, dispatch_tool


class _FakeScalars:
    def all(self):
        return []


class _FakeResult:
    def __init__(self, row=None):
        self._row = row

    def scalars(self):
        return _FakeScalars()

    def scalar_one_or_none(self):
        return self._row


class _FakeDB:
    def __init__(self, host_entry=None):
        self._entry = host_entry
        self.added = []

    async def execute(self, *a, **k):
        return _FakeResult(self._entry)

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        pass


def _host_entry(**over):
    base = dict(
        id=1, host="api.test.com", auth_type="none", auth_config_enc="",
        allowed_methods="GET,POST", min_interval_ms=0, enabled=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _ctx(entry, allow=True):
    return ToolContext(
        db=_FakeDB(entry),
        user=SimpleNamespace(id=1),
        agent=SimpleNamespace(id=1),
        allow_api_request=allow,
    )


@pytest.fixture(autouse=True)
def _clean_api_state():
    tools._API_SESSIONS.clear()
    tools._API_LAST_CALL.clear()
    yield
    tools._API_SESSIONS.clear()
    tools._API_LAST_CALL.clear()


class _FakeResp:
    def __init__(self, status, body="{}"):
        self.status_code = status
        self._body = body.encode()
        self.headers = {}

    async def aread(self):
        return self._body


class _FakeClient:
    """httpx.AsyncClient 假身：记录调用，登录时发 cookie，可编排 request 响应序列。"""

    responses: list[_FakeResp] = []

    def __init__(self, **kw):
        self.cookies = httpx.Cookies()
        self.logins: list[tuple] = []
        self.requests: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        self.logins.append((url, json, headers))
        self.cookies.set("bi_session", "sess1")
        return _FakeResp(200, '{"ok": true}')

    async def request(self, method, url, headers=None, json=None):
        self.requests.append((method, url, headers, json))
        return _FakeClient.responses.pop(0) if _FakeClient.responses else _FakeResp(200, '{"data": [1, 2]}')


def _patch_client(monkeypatch, holder: dict, responses: list[_FakeResp] | None = None):
    _FakeClient.responses = list(responses or [])

    def _factory(**kw):
        c = _FakeClient(**kw)
        holder["client"] = c
        return c

    monkeypatch.setattr(tools.httpx, "AsyncClient", _factory)


# ---------- 开关与参数校验 ----------

@pytest.mark.asyncio
async def test_api_request_rejected_without_switch():
    r = await dispatch_tool(_ctx(_host_entry(), allow=False), "api_request", {"method": "GET", "url": "https://api.test.com/x"})
    assert "未开通外部 API 调用" in r


def test_api_schema_separate_group():
    assert {s["function"]["name"] for s in API_TOOL_SCHEMAS} == {"api_request"}


@pytest.mark.asyncio
async def test_api_request_param_validation():
    ctx = _ctx(_host_entry())
    assert "method 必须" in await dispatch_tool(ctx, "api_request", {"method": "FETCH", "url": "https://api.test.com/x"})
    assert "URL 不合法" in await dispatch_tool(ctx, "api_request", {"method": "GET", "url": "notaurl"})
    assert "URL 不合法" in await dispatch_tool(ctx, "api_request", {"method": "GET", "url": "ftp://api.test.com/x"})
    assert "1-120" in await dispatch_tool(ctx, "api_request", {"method": "GET", "url": "https://api.test.com/x", "timeout_s": 999})
    assert "JSON 对象" in await dispatch_tool(ctx, "api_request", {"method": "POST", "url": "https://api.test.com/x", "body": "raw"})


@pytest.mark.asyncio
async def test_api_request_host_not_whitelisted():
    r = await dispatch_tool(_ctx(None), "api_request", {"method": "GET", "url": "https://evil.com/api"})
    assert "不在白名单内" in r


@pytest.mark.asyncio
async def test_api_request_method_not_allowed():
    r = await dispatch_tool(_ctx(_host_entry(allowed_methods="GET")), "api_request", {"method": "POST", "url": "https://api.test.com/x"})
    assert "仅允许 GET" in r


# ---------- 固定请求头认证 ----------

@pytest.mark.asyncio
async def test_api_request_headers_auth_and_model_header_blocked(monkeypatch):
    """预置门禁头自动注入；模型传的 Authorization/Cookie 被拦截，预置头覆盖一切。"""
    entry = _host_entry(auth_type="headers", auth_config_enc=json.dumps({"headers": {"Authorization": "Basic Zm9vOmJhcg=="}}))
    monkeypatch.setattr(tools, "decrypt_secret", lambda s: s)
    holder: dict = {}
    _patch_client(monkeypatch, holder)

    r = await dispatch_tool(
        _ctx(entry), "api_request",
        {"method": "GET", "url": "https://api.test.com/api/overview?month=2026-08",
         "headers": {"Authorization": "Bearer evil", "Cookie": "x=1", "X-Trace": "t1"}},
    )
    assert r.startswith("HTTP 200")
    assert '"data"' in r  # JSON 被美化输出
    _, _, headers, _ = holder["client"].requests[0]
    assert headers["Authorization"] == "Basic Zm9vOmJhcg=="  # 预置头，不是模型的 Bearer evil
    assert "Cookie" not in headers and "cookie" not in {k.lower(): v for k, v in headers.items() if k.lower() == "cookie"}
    assert headers["X-Trace"] == "t1"  # 普通自定义头放行
    audits = [x for x in holder and [] or []]
    assert not holder["client"].logins  # headers 型不登录


@pytest.mark.asyncio
async def test_api_request_audit_written(monkeypatch):
    entry = _host_entry()
    holder: dict = {}
    _patch_client(monkeypatch, holder)
    ctx = _ctx(entry)
    await dispatch_tool(ctx, "api_request", {"method": "GET", "url": "https://api.test.com/api/live"})
    audits = [x for x in ctx.db.added if getattr(x, "action", "") == "api.request"]
    assert len(audits) == 1
    detail = json.loads(audits[0].detail)
    assert detail["host"] == "api.test.com" and detail["status"] == 200
    assert "headers" not in detail  # 审计不落密钥/请求头


# ---------- 登录换 Cookie ----------

@pytest.mark.asyncio
async def test_api_request_login_session_flow(monkeypatch):
    """login_session 型：先登录拿 cookie 再请求；cookie 跨调用缓存复用（不重复登录）。"""
    cfg = {"headers": {"Authorization": "Basic Z2F0ZTox"}, "login_path": "/api/auth/login",
           "login_body": {"phone_number": "138", "password": "pw"}}
    entry = _host_entry(auth_type="login_session", auth_config_enc=json.dumps(cfg))
    monkeypatch.setattr(tools, "decrypt_secret", lambda s: s)
    holder: dict = {}
    _patch_client(monkeypatch, holder)

    r = await dispatch_tool(_ctx(entry), "api_request", {"method": "GET", "url": "https://api.test.com/api/daily-report?date=2026-08-19"})
    assert r.startswith("HTTP 200")
    client = holder["client"]
    assert len(client.logins) == 1
    login_url, login_body, login_headers = client.logins[0]
    assert login_url == "https://api.test.com/api/auth/login"
    assert login_body == {"phone_number": "138", "password": "pw"}
    assert login_headers["Authorization"] == "Basic Z2F0ZTox"  # 登录请求也带门禁头
    assert "sess1" in client.cookies.get("bi_session")
    # 响应里绝不泄露 cookie/密码
    assert "sess1" not in r and "pw" not in r


@pytest.mark.asyncio
async def test_api_request_login_session_cached(monkeypatch):
    """已有缓存 cookie 时不再登录。"""
    cfg = {"login_path": "/api/auth/login", "login_body": {"u": "1"}}
    entry = _host_entry(auth_type="login_session", auth_config_enc=json.dumps(cfg))
    monkeypatch.setattr(tools, "decrypt_secret", lambda s: s)
    tools._API_SESSIONS[entry.id] = httpx.Cookies()
    tools._API_SESSIONS[entry.id].set("bi_session", "cached")
    holder: dict = {}
    _patch_client(monkeypatch, holder)

    await dispatch_tool(_ctx(entry), "api_request", {"method": "GET", "url": "https://api.test.com/api/live"})
    assert not holder["client"].logins  # 没有重新登录


@pytest.mark.asyncio
async def test_api_request_401_triggers_relogin_once(monkeypatch):
    """请求 401（会话过期）→ 自动重登一次并重试；第二次 200 成功。"""
    cfg = {"login_path": "/api/auth/login", "login_body": {"u": "1"}}
    entry = _host_entry(auth_type="login_session", auth_config_enc=json.dumps(cfg))
    monkeypatch.setattr(tools, "decrypt_secret", lambda s: s)
    holder: dict = {}
    _patch_client(monkeypatch, holder, responses=[_FakeResp(401, '{"detail": "expired"}'), _FakeResp(200, '{"data": 1}')])

    r = await dispatch_tool(_ctx(entry), "api_request", {"method": "GET", "url": "https://api.test.com/api/overview"})
    assert r.startswith("HTTP 200")
    assert len(holder["client"].logins) == 2  # 首次登录 + 401 后重登
    assert len(holder["client"].requests) == 2


@pytest.mark.asyncio
async def test_api_request_4xx_returned_as_text(monkeypatch):
    """非 2xx：状态码 + 截断正文回给模型（不抛异常打死对话）。"""
    entry = _host_entry()
    holder: dict = {}
    _patch_client(monkeypatch, holder, responses=[_FakeResp(403, '{"detail": "no permission"}')])
    r = await dispatch_tool(_ctx(entry), "api_request", {"method": "GET", "url": "https://api.test.com/api/x"})
    assert r.startswith("HTTP 403") and "no permission" in r


# ---------- 管理端校验纯函数 ----------

def test_admin_normalize_host():
    from app.apihosts.router import _normalize_host

    assert _normalize_host("https://API.EXAMPLE.COM/api/x?y=1") == "api.example.com"
    assert _normalize_host("  api.example.com/ ") == "api.example.com"
    assert _normalize_host("api.example.com") == "api.example.com"


def test_admin_validate_auth():
    from fastapi import HTTPException

    from app.apihosts.router import _validate_auth

    assert _validate_auth("none", None) == {}
    assert _validate_auth("headers", {"headers": {"Authorization": "Basic x"}}) == {"headers": {"Authorization": "Basic x"}}
    cfg = _validate_auth("login_session", {"login_path": "/api/auth/login", "login_body": {"u": "1"}})
    assert cfg["login_path"] == "/api/auth/login" and cfg["headers"] == {}
    for bad_type, bad_cfg in [
        ("oauth2", {}),
        ("headers", {"headers": {}}),
        ("login_session", {"login_path": "api/login", "login_body": {"u": "1"}}),
        ("login_session", {"login_path": "/login", "login_body": {}}),
    ]:
        with pytest.raises(HTTPException):
            _validate_auth(bad_type, bad_cfg)


def test_admin_validate_methods():
    from fastapi import HTTPException

    from app.apihosts.router import _validate_methods

    assert _validate_methods("get, post") == "GET,POST"
    assert _validate_methods("GET") == "GET"
    with pytest.raises(HTTPException):
        _validate_methods("GET,FETCH")
    with pytest.raises(HTTPException):
        _validate_methods("")
