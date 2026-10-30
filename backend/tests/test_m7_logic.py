"""M7 逻辑测试：飞书 URL 解析、per-user token 状态机/滚动刷新、消息格式化、工具开关与审计。"""
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.feishu import tokens as feishu_tokens
from app.feishu.tokens import get_feishu_user_token, store_feishu_tokens, token_state
from app.runtime import tools
from app.runtime.tools import (
    FEISHU_TOOL_SCHEMAS,
    ToolContext,
    dispatch_tool,
    parse_feishu_url,
)


class _FakeResult:
    def scalar_one_or_none(self):
        return None

    def scalars(self):  # M28.4：名册查询走 scalars().all()
        return self

    def all(self):
        return []


class _FakeDB:
    def __init__(self):
        self.added = []

    async def execute(self, *a, **k):
        return _FakeResult()

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        pass


def _ctx(allow=True):
    return ToolContext(
        db=_FakeDB(),
        user=SimpleNamespace(id=1, feishu_open_id="ou_self", username="ou_self"),
        agent=SimpleNamespace(id=1),
        allow_feishu=allow,
    )


@pytest.fixture(autouse=True)
def _plain_crypto(monkeypatch):
    """加解密打桩为恒等，测试不依赖 FERNET_KEY。"""
    monkeypatch.setattr(feishu_tokens, "encrypt_secret", lambda s: s)
    monkeypatch.setattr(feishu_tokens, "decrypt_secret", lambda s: s)


# ---------- parse_feishu_url ----------

@pytest.mark.parametrize("raw,expected", [
    ("https://xxx.feishu.cn/docx/ABCdef1234567890abcd", ("docx", "ABCdef1234567890abcd")),
    ("看看这个 https://xxx.feishu.cn/docs/ABCdef1234567890abcd 谢谢", ("docx", "ABCdef1234567890abcd")),
    ("https://xxx.feishu.cn/doc/ABCdef1234567890abcd", ("docx", "ABCdef1234567890abcd")),
    ("https://xxx.feishu.cn/wiki/WikiToken1234567890ab", ("wiki", "WikiToken1234567890ab")),
    ("https://xxx.feishu.cn/sheets/SheetToken1234567890", ("sheet", "SheetToken1234567890")),
    ("https://xxx.feishu.cn/base/BaseToken1234567890ab", ("base", "BaseToken1234567890ab")),
    ("https://xxx.larksuite.com/docx/ABCdef1234567890abcd", ("docx", "ABCdef1234567890abcd")),
    ("ABCdef1234567890abcd", ("docx", "ABCdef1234567890abcd")),  # 裸 token
])
def test_parse_feishu_url_ok(raw, expected):
    assert parse_feishu_url(raw) == expected


@pytest.mark.parametrize("raw", ["hello world", "https://example.com/docx/abc", "", "short123"])
def test_parse_feishu_url_bad(raw):
    assert parse_feishu_url(raw) is None


# ---------- token_state / store / refresh ----------

def _user(**over):
    base = dict(
        id=1,
        feishu_user_token_enc=None,
        feishu_refresh_token_enc=None,
        feishu_token_expires_at=None,
        feishu_refresh_expires_at=None,
        feishu_scopes=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _info(**over):
    base = dict(access_token="at1", refresh_token="rt1", expires_in=7200,
                refresh_expires_in=2592000, scope="docx:document:readonly im:message")
    base.update(over)
    return base


def test_token_state_none():
    assert token_state(_user()) == "none"


def test_token_state_valid():
    u = _user()
    store_feishu_tokens(u, _info())
    assert u.feishu_user_token_enc == "at1"  # 恒等桩下可直接看
    assert u.feishu_scopes == "docx:document:readonly im:message"
    assert token_state(u) == "valid"


def test_token_state_refresh_and_reauth():
    now = datetime.now(UTC)
    # access 过期、refresh 有效 → refresh
    u = _user(feishu_user_token_enc="at", feishu_refresh_token_enc="rt",
              feishu_token_expires_at=now - timedelta(minutes=1),
              feishu_refresh_expires_at=now + timedelta(days=10))
    assert token_state(u) == "refresh"
    # refresh 也过期 → reauth
    u.feishu_refresh_expires_at = now - timedelta(days=1)
    assert token_state(u) == "reauth"
    # 没有 refresh_token → reauth
    u2 = _user(feishu_user_token_enc="at", feishu_token_expires_at=now - timedelta(minutes=1))
    assert token_state(u2) == "reauth"


def test_token_state_naive_datetime_treated_utc():
    """DB 读出的 naive datetime 不炸，按 UTC 比较。"""
    u = _user(feishu_user_token_enc="at",
              feishu_token_expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1))  # naive
    assert token_state(u) == "valid"


@pytest.mark.asyncio
async def test_get_token_valid_no_network():
    u = _user()
    store_feishu_tokens(u, _info())
    assert await get_feishu_user_token(_FakeDB(), u) == "at1"


@pytest.mark.asyncio
async def test_get_token_none_and_reauth():
    assert await get_feishu_user_token(_FakeDB(), _user()) is None
    now = datetime.now(UTC)
    u = _user(feishu_user_token_enc="at", feishu_token_expires_at=now - timedelta(hours=3))
    assert await get_feishu_user_token(_FakeDB(), u) is None


@pytest.mark.asyncio
async def test_get_token_refresh_success(monkeypatch):
    now = datetime.now(UTC)
    u = _user(feishu_user_token_enc="old_at", feishu_refresh_token_enc="old_rt",
              feishu_token_expires_at=now - timedelta(minutes=1),
              feishu_refresh_expires_at=now + timedelta(days=10))

    async def _fake_refresh(rt):
        assert rt == "old_rt"
        return _info(access_token="new_at", refresh_token="new_rt")

    monkeypatch.setattr(feishu_tokens.feishu, "refresh_user_token", _fake_refresh)
    db = _FakeDB()
    assert await get_feishu_user_token(db, u) == "new_at"
    assert u.feishu_user_token_enc == "new_at" and u.feishu_refresh_token_enc == "new_rt"  # 滚动换新已回写


@pytest.mark.asyncio
async def test_get_token_refresh_failure_returns_none(monkeypatch):
    now = datetime.now(UTC)
    u = _user(feishu_user_token_enc="old_at", feishu_refresh_token_enc="old_rt",
              feishu_token_expires_at=now - timedelta(minutes=1),
              feishu_refresh_expires_at=now + timedelta(days=10))

    async def _fail(rt):
        raise RuntimeError("refresh_token expired")

    monkeypatch.setattr(feishu_tokens.feishu, "refresh_user_token", _fail)
    assert await get_feishu_user_token(_FakeDB(), u) is None


# ---------- 消息格式化 ----------

def test_msg_text():
    assert tools._feishu_msg_text("text", json.dumps({"text": "你好"})) == "你好"
    assert tools._feishu_msg_text("text", "不是json") == "不是json"
    assert tools._feishu_msg_text("image", "{}") == "[图片]"
    assert tools._feishu_msg_text("merge_forward", "{}") == "[合并转发]"
    assert tools._feishu_msg_text("weird_type", "{}") == "[weird_type消息]"


# ---------- 工具开关与授权引导 ----------

def test_feishu_schema_names():
    assert {s["function"]["name"] for s in FEISHU_TOOL_SCHEMAS} == {
        "feishu_doc_read", "feishu_doc_search", "feishu_chat_search", "feishu_chat_history",
        "feishu_chat_daily", "feishu_approval_daily",  # M9a
        "feishu_approval_detail",  # M9a.2
        "feishu_mail_daily",  # M9b
        "feishu_mail_search",  # M9b.3
        "feishu_folder_list",  # M10a
        "feishu_doc_append", "feishu_sheet_write", "feishu_bitable_write",  # M10b
        "feishu_doc_delete_tail",  # M10b.2
        "feishu_doc_create",  # M10b.3
        "feishu_sheet_create", "feishu_bitable_create",  # M10b.6
        "feishu_bitable_field",  # M10b.7
        "feishu_doc_edit",  # M10c
        "feishu_file_upload",  # M29
    }


@pytest.mark.asyncio
async def test_feishu_tools_rejected_without_switch():
    r = await dispatch_tool(_ctx(allow=False), "feishu_doc_read", {"url": "https://x.feishu.cn/docx/ABCdef1234567890abcd"})
    assert "未开通飞书内容读取" in r


@pytest.mark.asyncio
async def test_doc_read_guides_when_no_token(monkeypatch):
    async def _no_token(db, user):
        return None

    monkeypatch.setattr(tools, "get_feishu_user_token", _no_token)
    r = await dispatch_tool(_ctx(), "feishu_doc_read", {"url": "https://x.feishu.cn/docx/ABCdef1234567890abcd"})
    assert "/login?upgrade=1" in r  # 引导链接原样转述给用户


# ---------- 假飞书 API ----------

class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _FakeFeishuClient:
    """httpx.AsyncClient 假身：按 URL 前缀路由到编排好的响应。calls 记录所有请求。"""

    routes: dict = {}
    calls: list = []

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _match(self, url, extra):
        _FakeFeishuClient.calls.append((url, extra))
        for prefix, payload in _FakeFeishuClient.routes.items():
            if prefix in url:
                return _FakeResp(payload(extra) if callable(payload) else payload)
        return _FakeResp({"code": 999, "msg": "no route"})

    async def get(self, url, headers=None, params=None):
        return self._match(url, params)

    async def post(self, url, headers=None, json=None, params=None):
        return self._match(url, json)


@pytest.fixture
def _fake_feishu(monkeypatch):
    _FakeFeishuClient.routes = {}
    _FakeFeishuClient.calls = []
    monkeypatch.setattr(tools.httpx, "AsyncClient", _FakeFeishuClient)

    async def _token(db, user):
        return "user_tok"

    monkeypatch.setattr(tools, "get_feishu_user_token", _token)
    yield
    _FakeFeishuClient.routes = {}
    _FakeFeishuClient.calls = []


@pytest.mark.asyncio
async def test_doc_read_success_and_audit(_fake_feishu):
    _FakeFeishuClient.routes["/docx/v1/documents/"] = {"code": 0, "data": {"content": "会议纪要正文"}}
    ctx = _ctx()
    r = await dispatch_tool(ctx, "feishu_doc_read", {"url": "https://x.feishu.cn/docx/ABCdef1234567890abcd"})
    assert "会议纪要正文" in r
    audits = [x for x in ctx.db.added if getattr(x, "action", "") == "feishu.doc_read"]
    assert len(audits) == 1
    detail = json.loads(audits[0].detail)
    assert detail["doc_token"] == "ABCdef1234567890abcd"
    assert "user_tok" not in audits[0].detail  # 审计不落 token


@pytest.mark.asyncio
async def test_doc_read_wiki_resolves_first(_fake_feishu):
    def _wiki(params):
        return {"code": 0, "data": {"node": {"obj_token": "RealDoc1234567890abcd", "obj_type": "docx", "title": "知识库文档"}}}

    _FakeFeishuClient.routes["/wiki/v2/spaces/get_node"] = _wiki
    _FakeFeishuClient.routes["/docx/v1/documents/"] = {"code": 0, "data": {"content": "正文"}}
    r = await dispatch_tool(_ctx(), "feishu_doc_read", {"url": "https://x.feishu.cn/wiki/WikiToken1234567890ab"})
    assert "《知识库文档》" in r and "正文" in r
    assert any("RealDoc1234567890abcd" in url for url, _ in _FakeFeishuClient.calls)


@pytest.mark.asyncio
async def test_doc_read_sheet_without_scope_guides():
    # M10a 起 sheet 已支持读取；无 sheets scope 时（未重授权）返回授权引导，不打飞书 API
    r = await dispatch_tool(_ctx(), "feishu_doc_read", {"url": "https://x.feishu.cn/sheets/SheetToken1234567890"})
    assert "/login?upgrade=1" in r


@pytest.mark.asyncio
async def test_doc_read_permission_error_guides(_fake_feishu):
    _FakeFeishuClient.routes["/docx/v1/documents/"] = {"code": 1061002, "msg": "no permission scope"}
    r = await dispatch_tool(_ctx(), "feishu_doc_read", {"url": "https://x.feishu.cn/docx/ABCdef1234567890abcd"})
    assert "权限不足" in r and "/login?upgrade=1" in r


@pytest.mark.asyncio
async def test_doc_read_truncation(_fake_feishu):
    _FakeFeishuClient.routes["/docx/v1/documents/"] = {"code": 0, "data": {"content": "长" * (tools._FEISHU_TEXT_MAX + 100)}}
    r = await dispatch_tool(_ctx(), "feishu_doc_read", {"url": "https://x.feishu.cn/docx/ABCdef1234567890abcd"})
    assert "已截断" in r and len(r) < tools._FEISHU_TEXT_MAX + 200


@pytest.mark.asyncio
async def test_chat_search_and_history(_fake_feishu):
    _FakeFeishuClient.routes["/im/v1/chats"] = {"code": 0, "data": {"items": [
        {"chat_id": "oc_aaa", "name": "门店经营分析群"},
        {"chat_id": "oc_bbb", "name": "门店巡检群"},
    ]}}
    _FakeFeishuClient.routes["/im/v1/messages"] = {"code": 0, "data": {"items": [
        {"sender": {"id": "ou_1"}, "create_time": "1760000000000", "msg_type": "text",
         "body": {"content": json.dumps({"text": "昨天销售额达标"})}},
        {"sender": {"id": "ou_2"}, "create_time": "1760000100000", "msg_type": "image", "body": {"content": "{}"}},
    ]}}
    _FakeFeishuClient.routes["/contact/v3/users/batch"] = {"code": 0, "data": {"items": [
        {"open_id": "ou_1", "name": "张三"}, {"open_id": "ou_2", "name": "李四"},
    ]}}

    r = await dispatch_tool(_ctx(), "feishu_chat_search", {"query": "经营"})
    assert "门店经营分析群" in r and "oc_aaa" in r

    ctx = _ctx()
    r = await dispatch_tool(ctx, "feishu_chat_history", {"chat": "经营分析", "days": 1})
    assert "张三：昨天销售额达标" in r  # sender 解析成姓名
    assert "[图片]" in r  # 非文本消息占位
    audits = [x for x in ctx.db.added if getattr(x, "action", "") == "feishu.chat_history"]
    assert len(audits) == 1
    assert "昨天销售额" not in audits[0].detail  # 审计不落消息正文


@pytest.mark.asyncio
async def test_chat_history_ambiguous_match(_fake_feishu):
    _FakeFeishuClient.routes["/im/v1/chats"] = {"code": 0, "data": {"items": [
        {"chat_id": "oc_aaa", "name": "门店经营分析群"},
        {"chat_id": "oc_bbb", "name": "门店经营周报群"},
    ]}}
    r = await dispatch_tool(_ctx(), "feishu_chat_history", {"chat": "经营"})
    assert "匹配到多个群" in r and "oc_aaa" in r


@pytest.mark.asyncio
async def test_contact_failure_degrades_to_openid(_fake_feishu):
    """缺 contact 权限时静默降级显示 open_id 前 12 位，不影响总结。"""
    _FakeFeishuClient.routes["/im/v1/chats"] = {"code": 0, "data": {"items": [{"chat_id": "oc_aaa", "name": "测试群"}]}}
    _FakeFeishuClient.routes["/im/v1/messages"] = {"code": 0, "data": {"items": [
        {"sender": {"id": "ou_xyz123456789"}, "create_time": "1760000000000", "msg_type": "text",
         "body": {"content": json.dumps({"text": "在的"})}},
    ]}}
    _FakeFeishuClient.routes["/contact/v3/users/batch"] = {"code": 999, "msg": "forbidden"}
    r = await dispatch_tool(_ctx(), "feishu_chat_history", {"chat": "测试群"})
    assert "ou_xyz123456" in r and "在的" in r


# ---------- 群聊图片下载落盘 ----------

def _img_msg(mid, key):
    return {"sender": {"id": "ou_1"}, "create_time": "1760000000000", "msg_type": "image",
            "body": {"content": json.dumps({"image_key": key})}, "message_id": mid}


@pytest.fixture
def _img_setup(_fake_feishu, monkeypatch, tmp_path):
    """群聊含图片消息；workspace 重定向到 tmp；download 打桩。"""
    _FakeFeishuClient.routes["/im/v1/chats"] = {"code": 0, "data": {"items": [{"chat_id": "oc_aaa", "name": "测试群"}]}}
    monkeypatch.setattr(tools, "_workspace_root", lambda uid, aid: tmp_path)
    calls = []

    async def _dl(client, token, mid, key):
        calls.append((mid, key))
        return b"\x89PNG-fake"

    monkeypatch.setattr(tools, "download_im_resource", _dl)
    return calls


@pytest.mark.asyncio
async def test_chat_image_saved_to_workspace(_img_setup):
    calls = _img_setup
    _FakeFeishuClient.routes["/im/v1/messages"] = {"code": 0, "data": {"items": [_img_msg("om_img001", "img_v3_xxx")]}}
    ctx = _ctx()
    r = await dispatch_tool(ctx, "feishu_chat_history", {"chat": "测试群"})
    assert calls == [("om_img001", "img_v3_xxx")]
    assert "[图片 已存工作区 feishu_imgs/" in r and "run_code" in r
    rows = [x for x in ctx.db.added if getattr(x, "filename", "").startswith("feishu_imgs/")]
    assert len(rows) == 1 and rows[0].mime == "image/png"
    from pathlib import Path
    assert Path(rows[0].storage_path).read_bytes() == b"\x89PNG-fake"


@pytest.mark.asyncio
async def test_chat_image_download_failure_degrades(_img_setup, monkeypatch):
    async def _fail(client, token, mid, key):
        raise RuntimeError("403")

    monkeypatch.setattr(tools, "download_im_resource", _fail)
    _FakeFeishuClient.routes["/im/v1/messages"] = {"code": 0, "data": {"items": [_img_msg("om_img001", "img_v3_xxx")]}}
    r = await dispatch_tool(_ctx(), "feishu_chat_history", {"chat": "测试群"})
    assert "[图片]" in r and "已存工作区" not in r


@pytest.mark.asyncio
async def test_chat_image_cap(_img_setup):
    calls = _img_setup
    _FakeFeishuClient.routes["/im/v1/messages"] = {"code": 0, "data": {"items": [
        _img_msg(f"om_img00{i}", f"key{i}") for i in range(5)
    ]}}
    r = await dispatch_tool(_ctx(), "feishu_chat_history", {"chat": "测试群"})
    assert len(calls) == tools._FEISHU_IMG_MAX  # 只下载前 3 张
    assert r.count("[图片 已存工作区") == tools._FEISHU_IMG_MAX
    assert "[图片]" in r  # 其余降级占位
