"""M13a 逻辑测试：文生图工具（schema/开关/配置降级/size 白名单/成功路径落库）+ AgentIn 字段透传。外部 HTTP 全 fake。"""
from types import SimpleNamespace

import pytest

import app.runtime.tools as tools
from app.agents.router import AgentIn, _apply_agent_in
from app.models.agent import Agent


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
        pass

    async def commit(self):
        self.committed += 1


def _ctx(db=None, **over):
    base = dict(db=db or _FakeDB(), user=SimpleNamespace(id=1), agent=SimpleNamespace(id=2))
    base.update(over)
    return tools.ToolContext(**base)


class _FakeResp:
    def __init__(self, status_code=200, payload=None, headers=None, content=b"", text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.content = content
        self.text = text

    def json(self):
        return self._payload


class _FakeArkClient:
    """方舟 images/generations + 图片下载的 fake（post 返图片 URL，get 返 PNG 字节）。"""

    posts = []

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        type(self).posts.append({"url": url, "json": json})
        return _FakeResp(payload={"data": [{"url": "https://cdn.example.com/x.png"}]})

    async def get(self, url, timeout=None):
        return _FakeResp(headers={"content-type": "image/png"}, content=b"\x89PNG-fake-bytes")


def _settings(tmp_path, ark_key="k"):
    return SimpleNamespace(
        VOLC_ARK_API_KEY=ark_key,
        VOLC_ARK_IMAGE_MODEL="test-model",
        DATA_DIR=str(tmp_path),
    )


# ---------- schema 与开关 ----------

def test_img_schema():
    s = tools.IMG_TOOL_SCHEMAS[0]["function"]
    assert s["name"] == "image_generate"
    assert s["parameters"]["required"] == ["prompt"]
    assert "image_generate" in tools.IMG_TOOL_NAMES


@pytest.mark.asyncio
async def test_dispatch_blocked_without_switch():
    r = await tools.dispatch_tool(_ctx(allow_image_generate=False), "image_generate", {"prompt": "x"})
    assert "未开通文生图" in r


@pytest.mark.asyncio
async def test_empty_prompt():
    r = await tools._image_generate(_ctx(allow_image_generate=True), {"prompt": "  "})
    assert "不能为空" in r


@pytest.mark.asyncio
async def test_no_key_returns_config_hint(monkeypatch):
    monkeypatch.setattr(tools, "get_settings", lambda: _settings(None, ark_key=""))
    r = await tools._image_generate(_ctx(allow_image_generate=True), {"prompt": "一只猫"})
    assert "VOLC_ARK_API_KEY" in r and "管理员" in r


# ---------- 成功路径与 size 白名单 ----------

@pytest.mark.asyncio
async def test_success_saves_workspace_and_created_files(monkeypatch, tmp_path):
    _FakeArkClient.posts = []
    monkeypatch.setattr(tools, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(tools.httpx, "AsyncClient", _FakeArkClient)
    db = _FakeDB()
    ctx = _ctx(db=db, allow_image_generate=True)
    r = await tools._image_generate(ctx, {"prompt": "一只猫", "size": "114x514"})  # 非法 size → 回落默认
    assert "gen_imgs/" in r
    assert _FakeArkClient.posts[0]["json"]["size"] == "1024x1024"  # 白名单回落
    ws_rows = [x for x in db.added if isinstance(x, tools.WorkspaceFile)]
    assert len(ws_rows) == 1 and ws_rows[0].mime == "image/png" and ws_rows[0].filename.startswith("gen_imgs/")
    assert len(ctx.created_files) == 1
    assert ctx.created_files[0]["mime"] == "image/png" and ctx.created_files[0]["path"].endswith(".png")
    audits = [x for x in db.added if getattr(x, "action", "") == "image.generate"]
    assert len(audits) == 1 and "一只猫" not in audits[0].detail  # 审计不落 prompt 全文


@pytest.mark.asyncio
async def test_http_error_returns_message(monkeypatch, tmp_path):
    class _ErrClient(_FakeArkClient):
        async def post(self, url, headers=None, json=None):
            return _FakeResp(status_code=429, text="rate limited")

    monkeypatch.setattr(tools, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(tools.httpx, "AsyncClient", _ErrClient)
    ctx = _ctx(allow_image_generate=True)
    r = await tools._image_generate(ctx, {"prompt": "一只猫"})
    assert "HTTP 429" in r and not ctx.created_files


# ---------- AgentIn 字段透传 ----------

def test_agent_in_field():
    assert AgentIn(name="x").allow_image_generate is False
    assert AgentIn(name="x", allow_image_generate=True).allow_image_generate is True
    a = Agent()
    _apply_agent_in(a, AgentIn(name="x", allow_image_generate=True))
    assert a.allow_image_generate is True
