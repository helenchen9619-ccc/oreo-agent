"""M3.9 逻辑测试：联网工具（schema 合法性 / dispatch 双保险 / SSRF 防护 / 搜索格式化 / 正文抽取截断）。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.runtime import tools
from app.runtime.tools import (
    TOOL_SCHEMAS,
    WEB_TOOL_NAMES,
    WEB_TOOL_SCHEMAS,
    ToolContext,
    _check_fetch_url,
    _extract_text,
    _web_search,
    dispatch_tool,
)


def _ctx(allow_web_search: bool) -> ToolContext:
    db = MagicMock()
    db.add = MagicMock()
    return ToolContext(
        db=db,
        user=SimpleNamespace(id=1, role="member"),
        agent=SimpleNamespace(id=1),
        allow_web_search=allow_web_search,
    )


class TestWebToolSchemas:
    def test_valid_openai_function_format(self):
        for s in WEB_TOOL_SCHEMAS:
            assert s["type"] == "function"
            fn = s["function"]
            assert fn["name"] and fn["description"]
            assert fn["parameters"]["type"] == "object"

    def test_required_params(self):
        by_name = {s["function"]["name"]: s["function"]["parameters"] for s in WEB_TOOL_SCHEMAS}
        assert by_name["web_search"]["required"] == ["query"]
        assert by_name["web_fetch"]["required"] == ["url"]

    def test_not_in_base_schemas(self):
        base_names = {s["function"]["name"] for s in TOOL_SCHEMAS}
        assert not (WEB_TOOL_NAMES & base_names)


class TestDispatchGuard:
    @pytest.mark.asyncio
    async def test_blocked_when_flag_off(self):
        ctx = _ctx(allow_web_search=False)
        r1 = await dispatch_tool(ctx, "web_search", {"query": "x"})
        r2 = await dispatch_tool(ctx, "web_fetch", {"url": "https://example.com"})
        assert "未开通联网搜索" in r1
        assert "未开通联网搜索" in r2

    @pytest.mark.asyncio
    async def test_missing_api_key_gives_guidance(self, monkeypatch):
        monkeypatch.setattr(tools, "get_settings", lambda: SimpleNamespace(BOCHA_API_KEY=""))
        ctx = _ctx(allow_web_search=True)
        r = await _web_search(ctx, {"query": "测试"})
        assert "未配置" in r and "BOCHA_API_KEY" in r


class TestCheckFetchUrl:
    def test_rejects_loopback_and_private(self):
        assert _check_fetch_url("http://localhost/") is not None
        assert _check_fetch_url("http://127.0.0.1:8000/healthz") is not None
        assert _check_fetch_url("http://192.168.1.1/x") is not None
        assert _check_fetch_url("http://10.0.0.5/") is not None
        assert _check_fetch_url("http://172.16.0.1/") is not None

    def test_rejects_link_local_cloud_metadata(self):
        assert _check_fetch_url("http://169.254.169.254/latest/meta-data") is not None

    def test_rejects_non_http_scheme(self):
        assert _check_fetch_url("file:///etc/passwd") is not None
        assert _check_fetch_url("ftp://example.com/x") is not None
        assert _check_fetch_url("") is not None
        assert _check_fetch_url("not a url") is not None

    def test_accepts_public_https(self):
        assert _check_fetch_url("https://example.com/page") is None


class TestWebSearchFormatting:
    def _mock_response(self, values):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"code": 200, "data": {"webPages": {"value": values}}}
        return resp

    @pytest.mark.asyncio
    async def test_numbered_list_with_snippet_and_meta(self, monkeypatch):
        monkeypatch.setattr(tools, "get_settings", lambda: SimpleNamespace(BOCHA_API_KEY="sk-test"))
        values = [
            {"name": "标题一", "url": "https://a.com/1", "summary": "摘要一", "siteName": "站点A", "dateLastCrawled": "2026-08-13T10:00:00"},
            {"name": "标题二", "url": "https://b.com/2", "snippet": "摘要二"},  # 无 summary 回退 snippet
        ]
        client = MagicMock()
        client.post = AsyncMock(return_value=self._mock_response(values))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(tools.httpx, "AsyncClient", lambda **kw: client)

        out = await _web_search(_ctx(True), {"query": "测试", "count": 5})
        assert "[1] 标题一" in out and "https://a.com/1" in out
        assert "摘要一" in out and "站点A" in out and "2026-08-13" in out
        assert "[2] 标题二" in out and "摘要二" in out

    @pytest.mark.asyncio
    async def test_total_output_truncated(self, monkeypatch):
        monkeypatch.setattr(tools, "get_settings", lambda: SimpleNamespace(BOCHA_API_KEY="sk-test"))
        values = [{"name": f"标题{i}" + "题" * 300, "url": f"https://a.com/{i}", "summary": "长" * 300} for i in range(10)]
        client = MagicMock()
        client.post = AsyncMock(return_value=self._mock_response(values))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(tools.httpx, "AsyncClient", lambda **kw: client)

        out = await _web_search(_ctx(True), {"query": "测试", "count": 10})
        assert len(out) <= tools._SEARCH_OUT_MAX + 20
        assert "已截断" in out

    @pytest.mark.asyncio
    async def test_http_error_returned_not_raised(self, monkeypatch):
        monkeypatch.setattr(tools, "get_settings", lambda: SimpleNamespace(BOCHA_API_KEY="sk-bad"))
        resp = MagicMock(status_code=401, text="unauthorized")
        client = MagicMock()
        client.post = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(tools.httpx, "AsyncClient", lambda **kw: client)

        out = await _web_search(_ctx(True), {"query": "x"})
        assert "401" in out


class TestExtractText:
    def test_extracts_body_from_html(self):
        html = (
            b"<html><head><title>t</title><style>body{color:red}</style></head>"
            b"<body><nav>menu</nav><article><h1>Heading</h1>"
            b"<p>" + b"body text sentence. " * 50 + b"</p></article>"
            b"<script>var x=1;</script></body></html>"
        )
        text = _extract_text(html)
        assert "body text sentence" in text
        assert "var x=1" not in text

    def test_garbage_returns_empty_or_short(self):
        assert isinstance(_extract_text(b""), str)
        assert isinstance(_extract_text(b"\xff\xfe\x00binary"), str)
