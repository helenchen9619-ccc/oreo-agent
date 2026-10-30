"""M8c 逻辑测试：群聊富文本（post）消息正文解析；M8c.1：帖内附图落盘。

契约（飞书 im v1 post content）：
- 两种形状：直给 {"title","content":[[...]]}；带语言包 {"post":{"zh_cn":{...}}}（优先 zh_cn）
- content=段落二维数组；元素 tag：text/a(text+href)/at(user_id,user_name)/img/emotion
- M8c 设计决策（2026-09-15 用户拍板）：正文只读文字，图片原位留 [图片] 标记
- M8c.1（同日苏晴反馈文字总结不稳定）：帖内附图下载落 feishu_imgs/（每帖≤3、共享全局3张预算），
  路径标注后模型用 run_code OCR 读图，与正文合并成总结
"""
import json
from types import SimpleNamespace

import pytest

from app.feishu.client import _post_image_keys
from app.runtime import tools
from app.runtime.tools import ToolContext, _feishu_msg_text, _post_text, dispatch_tool


# ---------- _post_text 纯函数 ----------

def test_post_text_direct_shape_full_elements():
    raw = json.dumps({
        "title": "巡店反馈",
        "content": [
            [{"tag": "text", "text": "大学路店：鸳鸯酪基底风味OK"}],
            [{"tag": "text", "text": "详情见"}, {"tag": "a", "text": "周报", "href": "https://x.feishu.cn/docx/abc"}],
            [{"tag": "at", "user_id": "ou_9", "user_name": "苏晴"}, {"tag": "text", "text": " 请跟进"}],
            [{"tag": "img", "image_key": "img_v2_xxx"}],
        ],
    })
    out = _post_text(raw)
    assert out.startswith("【巡店反馈】")
    assert "大学路店：鸳鸯酪基底风味OK" in out
    assert "周报(https://x.feishu.cn/docx/abc)" in out   # 链接带 href
    assert "@苏晴" in out
    assert "[图片]" in out                                # 原位标记，不下载


def test_post_text_locale_wrapped_prefers_zh_cn():
    raw = json.dumps({"post": {
        "zh_cn": {"title": "中文标题", "content": [[{"tag": "text", "text": "中文正文"}]]},
        "en_us": {"title": "EN", "content": [[{"tag": "text", "text": "English body"}]]},
    }})
    out = _post_text(raw)
    assert "中文正文" in out and "English" not in out


def test_post_text_locale_wrapped_first_locale_fallback():
    raw = json.dumps({"post": {"en_us": {"title": "", "content": [[{"tag": "text", "text": "only english"}]]}}})
    assert "only english" in _post_text(raw)


def test_post_text_bad_json_returns_raw():
    assert _post_text("not-json{") == "not-json{"


def test_post_text_unknown_tag_and_at_fallback():
    raw = json.dumps({"content": [[
        {"tag": "emotion", "emoji_type": "SMILE"},
        {"tag": "at", "user_id": "ou_noid"},
        {"tag": "media", "file_key": "f1"},
        {"tag": "code_block", "language": "python", "text": "print(1)"},
    ]]})
    out = _post_text(raw)
    assert "[表情]" in out
    assert "@ou_noid" in out            # at 无 user_name 时用 user_id
    assert "print(1)" in out            # 未知 tag 有 text 字段取 text


# ---------- _feishu_msg_text post 分支 ----------

def test_msg_text_post_empty_falls_back_to_placeholder():
    assert _feishu_msg_text("post", json.dumps({"title": "", "content": [[]]})) == "[富文本消息]"


def test_msg_text_post_normal_parses():
    out = _feishu_msg_text("post", json.dumps({"content": [[{"tag": "text", "text": "正文"}]]}))
    assert out == "正文"


# ---------- 端到端：chat_history 渲染 post ----------

class _FakeResult:
    def scalar_one_or_none(self):
        return None


class _FakeDB:
    def __init__(self):
        self.added = []

    async def execute(self, *a, **k):
        return _FakeResult()

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        pass


def _ctx():
    return ToolContext(
        db=_FakeDB(),
        user=SimpleNamespace(id=1, username="u1", feishu_open_id="ou_self",
                             feishu_scopes="im:message im:chat:readonly contact:user.base:readonly"),
        agent=SimpleNamespace(id=1),
        allow_feishu=True,
    )


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _FakeFeishuClient:
    routes: dict = {}
    calls: list = []

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _match(self, url, extra):
        _FakeFeishuClient.calls.append(url)
        for prefix, payload in _FakeFeishuClient.routes.items():
            if prefix in url:
                payload = payload(extra) if callable(payload) else payload
                return _FakeResp(payload)
        return _FakeResp({"code": 999, "msg": "no route"})

    async def get(self, url, headers=None, params=None):
        return self._match(url, params)

    async def post(self, url, headers=None, json=None, params=None):
        return self._match(url, json)


@pytest.fixture
def _fake_feishu(monkeypatch, tmp_path):
    _FakeFeishuClient.routes = {}
    _FakeFeishuClient.calls = []
    monkeypatch.setattr(tools.httpx, "AsyncClient", _FakeFeishuClient)

    async def _token(db, user):
        return "user_tok"

    async def _dl(client, token, message_id, file_key):
        return b"\x89PNG-fake"

    monkeypatch.setattr(tools, "get_feishu_user_token", _token)
    monkeypatch.setattr(tools, "download_im_resource", _dl)
    monkeypatch.setattr(tools, "_workspace_root", lambda uid, aid: tmp_path)
    yield
    _FakeFeishuClient.routes = {}
    _FakeFeishuClient.calls = []


# ---------- M8c.1：_post_image_keys 纯函数 ----------

def test_post_image_keys_multi_in_order():
    raw = json.dumps({"content": [
        [{"tag": "text", "text": "看图"}],
        [{"tag": "img", "image_key": "k1"}],
        [{"tag": "img", "image_key": "k2"}, {"tag": "img", "image_key": ""}],
        [{"tag": "img", "image_key": "k3"}],
    ]})
    assert _post_image_keys(raw) == ["k1", "k2", "k3"]


def test_post_image_keys_locale_wrapped():
    raw = json.dumps({"post": {"zh_cn": {"content": [[{"tag": "img", "image_key": "kz"}]]}}})
    assert _post_image_keys(raw) == ["kz"]


def test_post_image_keys_bad_json_empty():
    assert _post_image_keys("oops{") == []
    assert _post_image_keys(json.dumps({"content": [[{"tag": "text", "text": "纯文字"}]]})) == []


def _register_chat(msg_items):
    _FakeFeishuClient.routes["/im/v1/chats"] = {"code": 0, "data": {"items": [
        {"chat_id": "oc_aaa", "name": "柿子爆爆爆"},
    ]}}
    _FakeFeishuClient.routes["/im/v1/messages"] = {"code": 0, "data": {"items": msg_items}}
    _FakeFeishuClient.routes["/contact/v3/users/batch"] = {"code": 0, "data": {"items": [
        {"open_id": "ou_ct", "name": "苏晴"},
    ]}}


def _post_msg(mid, paras, ts="1760000000000"):
    return {"sender": {"id": "ou_ct"}, "create_time": ts, "msg_type": "post",
            "body": {"content": json.dumps({"post": {"zh_cn": {"title": "", "content": paras}}})},
            "message_id": mid, "chat_id": "oc_aaa"}


@pytest.mark.asyncio
async def test_chat_history_renders_post_full_text(_fake_feishu):
    """苏晴场景：富文本巡店帖读出全文，不再是「[富文本消息]」占位。"""
    post_body = json.dumps({"post": {"zh_cn": {"title": "巡店反馈-虹口龙之梦", "content": [
        [{"tag": "text", "text": "堂食：苹果芭乐碗 17 点售罄但小程序仍可下单"}],
        [{"tag": "text", "text": "问题：等餐近 1 小时；鸳鸯酪基底稀"}],
        [{"tag": "img", "image_key": "img_v2_abc"}],
        [{"tag": "at", "user_id": "ou_ct", "user_name": "苏晴"}],
    ]}}})
    _register_chat([
        {"sender": {"id": "ou_ct"}, "create_time": "1760000000000", "msg_type": "post",
         "body": {"content": post_body}, "message_id": "om_1", "chat_id": "oc_aaa"},
        {"sender": {"id": "ou_ct"}, "create_time": "1760000100000", "msg_type": "text",
         "body": {"content": json.dumps({"text": "补充：脆皮状态很好"})}, "message_id": "om_2", "chat_id": "oc_aaa"},
    ])

    r = await dispatch_tool(_ctx(), "feishu_chat_history", {"chat": "柿子", "days": 1})
    assert "[富文本消息]" not in r
    assert "【巡店反馈-虹口龙之梦】" in r
    assert "苹果芭乐碗 17 点售罄" in r and "等餐近 1 小时" in r
    assert "苏晴：" in r                       # sender 正常解析
    assert "补充：脆皮状态很好" in r           # text 消息不受影响
    # M8c.1：帖内附图落盘并标注路径（oc_aaa/om_1 各取尾 6 位 + _p1 序号）
    assert "帖内图片 1 张已存工作区：feishu_imgs/oc_aaa_om_1_p1.png" in r


# ---------- M8c.1：帖内附图下载纪律 ----------

@pytest.mark.asyncio
async def test_post_multi_images_download_with_index(_fake_feishu):
    _register_chat([_post_msg("om_mul", [
        [{"tag": "text", "text": "纯图反馈"}],
        [{"tag": "img", "image_key": "k1"}],
        [{"tag": "img", "image_key": "k2"}],
    ])])
    r = await dispatch_tool(_ctx(), "feishu_chat_history", {"chat": "柿子", "days": 1})
    assert "帖内图片 2 张已存工作区" in r
    assert "feishu_imgs/oc_aaa_om_mul_p1.png" in r and "feishu_imgs/oc_aaa_om_mul_p2.png" in r


@pytest.mark.asyncio
async def test_post_images_capped_per_post(_fake_feishu):
    paras = [[{"tag": "img", "image_key": f"k{i}"}] for i in range(4)]
    _register_chat([_post_msg("om_cap", paras)])
    r = await dispatch_tool(_ctx(), "feishu_chat_history", {"chat": "柿子", "days": 1})
    assert "帖内图片 3 张已存工作区" in r            # _FEISHU_POST_IMG_MAX=3
    assert "_p4.png" not in r


@pytest.mark.asyncio
async def test_post_images_share_global_budget(_fake_feishu, monkeypatch):
    """image 消息已占满全局 3 张预算时，post 附图不再下载。"""
    imgs = [{"sender": {"id": "ou_ct"}, "create_time": f"1760000{i:06d}", "msg_type": "image",
             "body": {"content": json.dumps({"image_key": f"ik{i}"})},
             "message_id": f"om_img{i}", "chat_id": "oc_aaa"} for i in range(3)]
    _register_chat(imgs + [_post_msg("om_post", [[{"tag": "img", "image_key": "pk"}]], ts="1760009900000")])
    r = await dispatch_tool(_ctx(), "feishu_chat_history", {"chat": "柿子", "days": 1})
    assert r.count("已存工作区 feishu_imgs/") == 3   # 三张独立图
    assert "帖内图片" not in r                        # post 附图没预算了


@pytest.mark.asyncio
async def test_post_image_single_failure_skipped(_fake_feishu, monkeypatch):
    async def _flaky(client, token, message_id, file_key):
        if file_key == "bad":
            raise RuntimeError("boom")
        return b"\x89PNG-fake"

    monkeypatch.setattr(tools, "download_im_resource", _flaky)
    _register_chat([_post_msg("om_flk", [
        [{"tag": "img", "image_key": "bad"}],
        [{"tag": "img", "image_key": "good"}],
    ])])
    r = await dispatch_tool(_ctx(), "feishu_chat_history", {"chat": "柿子", "days": 1})
    assert "帖内图片 1 张已存工作区" in r   # 坏的那张静默跳过，好图照常
