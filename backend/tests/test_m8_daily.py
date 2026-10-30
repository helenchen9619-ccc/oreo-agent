"""M8 逻辑测试：p2p 私聊枚举/人名定位、feishu_chat_daily 全会话扫描、推送推给本人。"""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.runtime import tools
from app.runtime.tools import ToolContext, dispatch_tool
from app.scheduler import feishu_push
from app.scheduler.router import _validate_push


class _FakeResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def scalar_one_or_none(self):
        return None

    def scalars(self):  # M28.4：名册查询走 scalars().all()
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows=None):
        self.added = []
        self._rows = rows or []  # 预置名册行（FeishuP2pChat 或同名属性的命名空间）

    async def execute(self, *a, **k):
        return _FakeResult(self._rows)

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        pass


def _ctx(allow=True, roster=None):
    return ToolContext(
        db=_FakeDB(rows=roster),
        user=SimpleNamespace(id=1, feishu_open_id="ou_self", username="ou_self"),
        agent=SimpleNamespace(id=1),
        allow_feishu=allow,
    )


# ---------- 假飞书 API（同 test_m7_logic 的约定：URL 前缀路由，先注册更具体的前缀） ----------

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


def _chats_payload():
    """一群一私聊（私聊 name 为空、chat_mode=p2p）。"""
    return {"code": 0, "data": {"items": [
        {"chat_id": "oc_group1", "name": "门店经营分析群", "chat_mode": "group"},
        {"chat_id": "oc_p2p1", "name": "", "chat_mode": "p2p"},
    ]}}


def _msg_payload(text="今天日报记得交"):
    return {"code": 0, "data": {"items": [
        {"sender": {"id": "ou_peer1"}, "create_time": "1760000000000", "msg_type": "text",
         "body": {"content": json.dumps({"text": text})}, "message_id": "om_1"},
    ]}}


def _setup_p2p_routes():
    # members 前缀更具体，必须先注册（_match 按注册顺序找第一个命中）
    _FakeFeishuClient.routes["/members"] = {"code": 0, "data": {"items": [
        {"member_id": "ou_self"}, {"member_id": "ou_peer1"},
    ]}}
    _FakeFeishuClient.routes["/contact/v3/users/batch"] = {"code": 0, "data": {"items": [
        {"open_id": "ou_peer1", "name": "张三"},
    ]}}
    _FakeFeishuClient.routes["/im/v1/chats"] = _chats_payload()


# ---------- chat_search：空 query 列全部（群 + 私聊带人名） ----------

@pytest.mark.asyncio
async def test_chat_search_empty_lists_all(_fake_feishu):
    _setup_p2p_routes()
    r = await dispatch_tool(_ctx(), "feishu_chat_search", {"query": ""})
    assert "门店经营分析群" in r
    assert "私聊·张三" in r  # p2p 对方姓名解析出来了
    assert "oc_p2p1" in r


@pytest.mark.asyncio
async def test_chat_search_by_person_name(_fake_feishu):
    _setup_p2p_routes()
    r = await dispatch_tool(_ctx(), "feishu_chat_search", {"query": "张三"})
    assert "私聊·张三" in r and "门店经营分析群" not in r


# ---------- M28：列表不含 p2p 时按人名搜同事走消息搜索兜底 ----------

@pytest.mark.asyncio
async def test_chat_search_person_via_recent_p2p_fallback(_fake_feishu):
    """M28：/im/v1/chats 不含 p2p（云上实测形态）——列表无命中时走 _feishu_recent_p2p 兜底。"""
    # 注册顺序：更具体的前缀在前（members ⊃ chat 前缀，单条消息 ⊃ 列表前缀）
    _FakeFeishuClient.routes["/im/v1/chats/oc_p2p_x/members"] = {"code": 0, "data": {"items": [
        {"member_id": "ou_self"}, {"member_id": "ou_peer1"},
    ]}}
    _FakeFeishuClient.routes["/im/v1/chats/oc_p2p_x"] = {"code": 0, "data": {"name": "", "chat_mode": "p2p"}}
    _single_msg_routes({"om_p1": "oc_p2p_x"})
    _FakeFeishuClient.routes["/contact/v3/users/batch"] = {"code": 0, "data": {"items": [
        {"open_id": "ou_peer1", "name": "李四"},
    ]}}
    _FakeFeishuClient.routes["/search/v2/message"] = _search_payload(["om_p1"])
    _FakeFeishuClient.routes["/im/v1/chats"] = {"code": 0, "data": {"items": [
        {"chat_id": "oc_group1", "name": "门店经营分析群", "chat_mode": "group"},
    ]}}  # 列表里没有 p2p
    r = await dispatch_tool(_ctx(), "feishu_chat_search", {"query": "李四"})
    assert "私聊·李四" in r and "oc_p2p_x" in r and "没找到" not in r


@pytest.mark.asyncio
async def test_chat_search_group_hit_skips_p2p_fallback(_fake_feishu):
    """M28：群已命中时不花消息搜索开销（兜底只在列表全落空时触发）。"""
    _setup_p2p_routes()
    r = await dispatch_tool(_ctx(), "feishu_chat_search", {"query": "经营"})
    assert "门店经营分析群" in r
    assert not any("/search/v2/message" in url for url, _ in _FakeFeishuClient.calls)


# ---------- M28.1：定向深扫——翻页更深（突破 100 条窗口）+ 命中即停 ----------

def _setup_deep_scan_routes(peer_name="林晓", pages=None):
    """定向深扫公共路由：p2p 会话 oc_p2p_x（对方 peer_name）+ 按 page_token 分页的搜索结果。"""
    _FakeFeishuClient.routes["/im/v1/chats/oc_p2p_x/members"] = {"code": 0, "data": {"items": [
        {"member_id": "ou_self"}, {"member_id": "ou_peer1"},
    ]}}
    _FakeFeishuClient.routes["/im/v1/chats/oc_p2p_x"] = {"code": 0, "data": {"name": "", "chat_mode": "p2p"}}
    _FakeFeishuClient.routes["/contact/v3/users/batch"] = {"code": 0, "data": {"items": [
        {"open_id": "ou_peer1", "name": peer_name},
    ]}}
    _FakeFeishuClient.routes["/search/v2/message"] = lambda body: pages[(body or {}).get("page_token", "")]
    _FakeFeishuClient.routes["/im/v1/chats"] = {"code": 0, "data": {"items": [
        {"chat_id": "oc_group1", "name": "门店经营分析群", "chat_mode": "group"},
    ]}}  # 列表里没有 p2p


@pytest.mark.asyncio
async def test_chat_search_target_deep_scan_beyond_window(_fake_feishu):
    """M28.1：目标私聊消息排在第 6 页（前 100 条全是群消息，旧窗口必然漏掉）→ 深扫翻页命中。"""
    group_ids = [f"om_g{i:03d}" for i in range(100)]  # 同长度零填充，避免前缀误配
    mapping = {mid: "oc_group1" for mid in group_ids}
    mapping["om_p1"] = "oc_p2p_x"
    _single_msg_routes(mapping)
    pages, tok = {}, ""
    for i in range(5):  # 5 页 × 20 条群消息 = 旧 100 条窗口全部占满
        nxt = f"p{i + 2}"
        pages[tok] = {"code": 0, "data": {"items": group_ids[i * 20:(i + 1) * 20], "has_more": True, "page_token": nxt}}
        tok = nxt
    pages[tok] = {"code": 0, "data": {"items": ["om_p1"], "has_more": False}}  # 第 6 页才是私聊消息
    _setup_deep_scan_routes("林晓", pages)
    r = await dispatch_tool(_ctx(), "feishu_chat_search", {"query": "林晓"})
    assert "私聊·林晓" in r and "oc_p2p_x" in r


@pytest.mark.asyncio
async def test_chat_search_target_stops_on_first_hit(_fake_feishu):
    """M28.1：第 1 页就命中时不再翻页（典型「昨天刚聊过」场景要秒回）。"""
    _single_msg_routes({"om_g000": "oc_group1", "om_p1": "oc_p2p_x"})
    pages = {
        "": {"code": 0, "data": {"items": ["om_g000", "om_p1"], "has_more": True, "page_token": "p2"}},
        "p2": {"code": 0, "data": {"items": ["om_g001"], "has_more": False}},  # 不应被请求到
    }
    _setup_deep_scan_routes("林晓", pages)
    r = await dispatch_tool(_ctx(), "feishu_chat_search", {"query": "林晓"})
    assert "私聊·林晓" in r
    assert sum(1 for url, _ in _FakeFeishuClient.calls if "/search/v2/message" in url) == 1


# ---------- M28.3：start/end 时间窗定向找私聊 + 未命中覆盖报告 ----------

def test_parse_feishu_dt():
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt
    from app.runtime.tools import _parse_feishu_dt
    tz = ZoneInfo("Asia/Shanghai")
    assert _parse_feishu_dt("2026-09-21") == int(_dt(2026, 9, 21, tzinfo=tz).timestamp())
    assert _parse_feishu_dt("2026-09-21 19:30") == int(_dt(2026, 9, 21, 19, 30, tzinfo=tz).timestamp())
    assert _parse_feishu_dt("2026/09/21") == int(_dt(2026, 9, 21, tzinfo=tz).timestamp())  # 斜杠容忍
    assert _parse_feishu_dt("2026-09-21", end_of_day=True) == int(_dt(2026, 9, 22, tzinfo=tz).timestamp())  # 日期粒度=当天全天
    assert _parse_feishu_dt("垃圾") is None and _parse_feishu_dt("") is None and _parse_feishu_dt("2026-13-01") is None


@pytest.mark.asyncio
async def test_chat_history_window_find_p2p(_fake_feishu):
    """M28.3：带 start/end 时深扫限定窗口内（from_time/to_time=窗口时间戳），且拉消息窗口同步。"""
    from app.runtime.tools import _parse_feishu_dt
    pages = {"": {"code": 0, "data": {"items": ["om_g000", "om_p1"], "has_more": False}}}
    _setup_deep_scan_routes("林晓", pages)
    _single_msg_routes({"om_g000": "oc_group1", "om_p1": "oc_p2p_x"})
    _FakeFeishuClient.routes["/im/v1/messages"] = _msg_payload()
    r = await dispatch_tool(_ctx(), "feishu_chat_history",
                            {"chat": "林晓", "start": "2026-09-01 19:00", "end": "2026-09-01 21:00"})
    assert "私聊·林晓" in r and "林晓：今天日报记得交" in r
    exp_lo, exp_hi = str(_parse_feishu_dt("2026-09-01 19:00")), str(_parse_feishu_dt("2026-09-01 21:00"))
    search_bodies = [extra for url, extra in _FakeFeishuClient.calls if "/search/v2/message" in url]
    assert search_bodies and all(b.get("from_time") == exp_lo and b.get("to_time") == exp_hi for b in search_bodies)
    list_calls = [extra for url, extra in _FakeFeishuClient.calls if url.endswith("/im/v1/messages")]
    assert list_calls and list_calls[0].get("start_time") == exp_lo and list_calls[0].get("end_time") == exp_hi


@pytest.mark.asyncio
async def test_chat_history_window_date_only_all_day(_fake_feishu):
    """M28.3：start 只给日期 = 当天全天（end 缺省推到次日零点）。"""
    from app.runtime.tools import _parse_feishu_dt
    pages = {"": {"code": 0, "data": {"items": ["om_p1"], "has_more": False}}}
    _setup_deep_scan_routes("林晓", pages)
    _single_msg_routes({"om_p1": "oc_p2p_x"})
    _FakeFeishuClient.routes["/im/v1/messages"] = _msg_payload()
    r = await dispatch_tool(_ctx(), "feishu_chat_history", {"chat": "林晓", "start": "2026-09-01"})
    assert "私聊·林晓" in r
    search_bodies = [extra for url, extra in _FakeFeishuClient.calls if "/search/v2/message" in url]
    assert search_bodies[0].get("to_time") == str(_parse_feishu_dt("2026-09-02"))  # end 缺省=次日零点


@pytest.mark.asyncio
async def test_chat_history_bad_start_format(_fake_feishu):
    r = await dispatch_tool(_ctx(), "feishu_chat_history", {"chat": "林晓", "start": "昨晚八点"})
    assert "start 格式不对" in r
    assert not _FakeFeishuClient.calls  # 格式错误不发任何飞书请求


@pytest.mark.asyncio
async def test_chat_history_miss_reports_coverage(_fake_feishu):
    """M28.3：深扫未命中时如实报告覆盖范围（扫了多久/多少条），引导补时间窗重试。"""
    pages = {"": {"code": 0, "data": {"items": ["om_g000", "om_g001"], "has_more": False}}}
    _setup_deep_scan_routes("林晓", pages)
    _single_msg_routes({"om_g000": "oc_group1", "om_g001": "oc_group1"})
    r = await dispatch_tool(_ctx(), "feishu_chat_history", {"chat": "林晓", "days": 1})
    assert "没找到" in r and "已按时间倒序扫了" in r and "共 2 条消息" in r and "按时间窗定向找" in r
    assert "发一条消息" in r  # M28.4：授权前私聊不进索引的激活指引


# ---------- M28.4 私聊名册：发现过的 p2p 持久化，按名字先查名册 ----------

@pytest.mark.asyncio
async def test_roster_hit_skips_deep_scan(_fake_feishu):
    """M28.4：名册里有的私聊直接命中，不触发消息搜索深扫（授权前私聊也能按名字找到）。"""
    roster = [SimpleNamespace(chat_id="oc_p2p_x", peer_name="林晓")]
    _FakeFeishuClient.routes["/im/v1/chats"] = {"code": 0, "data": {"items": [
        {"chat_id": "oc_group1", "name": "门店经营分析群", "chat_mode": "group"},
    ]}}
    _FakeFeishuClient.routes["/im/v1/messages"] = _msg_payload()
    r = await dispatch_tool(_ctx(roster=roster), "feishu_chat_history", {"chat": "林晓", "days": 1})
    assert "私聊·林晓" in r
    assert not any("/search/v2/message" in url for url, _ in _FakeFeishuClient.calls)  # 名册命中免深扫


@pytest.mark.asyncio
async def test_deep_scan_persists_roster(_fake_feishu):
    """M28.4：深扫新发现的私聊回写名册（找到一次终身可查）。"""
    _FakeFeishuClient.routes["/im/v1/chats/oc_p2p_x/members"] = {"code": 0, "data": {"items": [
        {"member_id": "ou_self"}, {"member_id": "ou_peer1"},
    ]}}
    _FakeFeishuClient.routes["/im/v1/chats/oc_p2p_x"] = {"code": 0, "data": {"name": "", "chat_mode": "p2p"}}
    _single_msg_routes({"om_p1": "oc_p2p_x"})
    _FakeFeishuClient.routes["/contact/v3/users/batch"] = {"code": 0, "data": {"items": [
        {"open_id": "ou_peer1", "name": "林晓"},
    ]}}
    _FakeFeishuClient.routes["/search/v2/message"] = _search_payload(["om_p1"])
    _FakeFeishuClient.routes["/im/v1/chats"] = {"code": 0, "data": {"items": [
        {"chat_id": "oc_group1", "name": "门店经营分析群", "chat_mode": "group"},
    ]}}
    ctx = _ctx()
    r = await dispatch_tool(ctx, "feishu_chat_search", {"query": "林晓"})
    assert "私聊·林晓" in r
    from app.models.feishu import FeishuP2pChat
    assert any(isinstance(row, FeishuP2pChat) and row.chat_id == "oc_p2p_x" and row.peer_name == "林晓"
               for row in ctx.db.added)


# ---------- 按人名定位私聊拉历史 ----------

@pytest.mark.asyncio
async def test_chat_history_by_person_name(_fake_feishu):
    _setup_p2p_routes()
    _FakeFeishuClient.routes["/im/v1/messages"] = _msg_payload()
    ctx = _ctx()
    r = await dispatch_tool(ctx, "feishu_chat_history", {"chat": "张三", "days": 1})
    assert "私聊·张三" in r and "张三：今天日报记得交" in r
    # 拉的是 p2p 会话的消息
    assert any("oc_p2p1" in str(extra) or "oc_p2p1" in url for url, extra in _FakeFeishuClient.calls)


@pytest.mark.asyncio
async def test_chat_history_by_person_name_via_search(_fake_feishu):
    """M8b：私聊不在 /im/v1/chats 列表时，按人名定位走消息搜索兜底发现私聊会话。"""
    # 注册顺序：更具体的前缀在前（members ⊃ chat 前缀，单条消息 ⊃ 消息列表前缀）
    _FakeFeishuClient.routes["/im/v1/chats/oc_p2p_x/members"] = {"code": 0, "data": {"items": [
        {"member_id": "ou_self"}, {"member_id": "ou_peer1"},
    ]}}
    _FakeFeishuClient.routes["/im/v1/chats/oc_p2p_x"] = {"code": 0, "data": {"name": "", "chat_mode": "p2p"}}
    _single_msg_routes({"om_p1": "oc_p2p_x"})
    _FakeFeishuClient.routes["/im/v1/messages"] = _msg_payload()
    _FakeFeishuClient.routes["/contact/v3/users/batch"] = {"code": 0, "data": {"items": [
        {"open_id": "ou_peer1", "name": "张三"},
    ]}}
    _FakeFeishuClient.routes["/search/v2/message"] = _search_payload(["om_p1"])
    _FakeFeishuClient.routes["/im/v1/chats"] = {"code": 0, "data": {"items": [
        {"chat_id": "oc_group1", "name": "门店经营分析群", "chat_mode": "group"},
    ]}}  # 列表里没有 p2p（云上实测形态）
    r = await dispatch_tool(_ctx(), "feishu_chat_history", {"chat": "张三", "days": 1})
    assert "私聊·张三" in r and "张三：今天日报记得交" in r
    assert any("oc_p2p_x" in url for url, _ in _FakeFeishuClient.calls)  # 拉的是搜索发现的私聊会话


# ---------- feishu_chat_daily：搜索管线（search ids → 逐条 get_message → 按会话分组） ----------

def _search_payload(ids):
    return {"code": 0, "data": {"items": ids, "has_more": False}}


def _single_msg_routes(mapping):
    """mapping: message_id → chat_id 或 None（None=230027 私聊不可读）。注册单条 GET 路由。"""
    def _mk(mid, chat_id):
        if chat_id is None:
            return {"code": 230027, "msg": "message not visible"}
        return {"code": 0, "data": {"items": [{
            "message_id": mid, "chat_id": chat_id,
            "sender": {"id": "ou_peer1"}, "create_time": "1760000000000", "msg_type": "text",
            "body": {"content": json.dumps({"text": f"内容{mid[-2:]}"})},
        }]}}
    for mid, cid in mapping.items():
        _FakeFeishuClient.routes[f"/im/v1/messages/{mid}"] = _mk(mid, cid)


@pytest.mark.asyncio
async def test_daily_groups_sections_and_audit(_fake_feishu):
    # 具体前缀必须先于通用路由注册（_match 按注册顺序找第一个命中，且 members URL 包含 chat 前缀）
    _FakeFeishuClient.routes["/im/v1/chats/oc_p2p_new/members"] = {"code": 0, "data": {"items": [
        {"member_id": "ou_self"}, {"member_id": "ou_peer1"},
    ]}}
    _FakeFeishuClient.routes["/im/v1/chats/oc_p2p_new"] = {"code": 0, "data": {"name": "", "chat_mode": "p2p"}}
    _setup_p2p_routes()
    _FakeFeishuClient.routes["/search/v2/message"] = _search_payload(["om_g1", "om_g2", "om_p1", "om_bad"])
    # om_p1 来自不在会话列表里的私聊（M8b：p2p 可读，标签经 members 解析成对方姓名）；
    # om_bad 读取失败（兜底计数路径保留）
    _single_msg_routes({"om_g1": "oc_group1", "om_g2": "oc_group1", "om_p1": "oc_p2p_new", "om_bad": None})
    ctx = _ctx()
    r = await dispatch_tool(ctx, "feishu_chat_daily", {"days": 1})
    assert "门店经营分析群" in r and "张三：内容g1" in r  # 分组 + 发送者姓名
    assert "私聊·张三" in r  # p2p 会话标签解析出对方姓名
    assert "另有 1 条消息正文读取失败" in r  # 失败如实标注
    audits = [x for x in ctx.db.added if getattr(x, "action", "") == "feishu.chat_daily"]
    assert len(audits) == 1
    detail = json.loads(audits[0].detail)
    assert detail["chats"] == 2 and detail["messages"] == 3 and detail["unreadable_p2p"] == 1
    assert "内容g1" not in audits[0].detail  # 审计不落消息正文
    assert "user_tok" not in audits[0].detail


@pytest.mark.asyncio
async def test_daily_all_unreadable_guides_reauth(_fake_feishu):
    """全部读取失败 → 引导重授权（可能缺 im:message.p2p_msg:get_as_user）。"""
    _FakeFeishuClient.routes["/search/v2/message"] = _search_payload(["om_p1", "om_p2"])
    _single_msg_routes({"om_p1": None, "om_p2": None})
    r = await dispatch_tool(_ctx(), "feishu_chat_daily", {"days": 1})
    assert "全部读取失败" in r and "upgrade=1" in r


@pytest.mark.asyncio
async def test_daily_no_messages(_fake_feishu):
    _FakeFeishuClient.routes["/search/v2/message"] = _search_payload([])
    r = await dispatch_tool(_ctx(), "feishu_chat_daily", {"days": 1})
    assert "没有消息" in r


@pytest.mark.asyncio
async def test_daily_unknown_chat_resolved_via_get_chat_info(_fake_feishu):
    """消息所属会话不在 /im/v1/chats 列表里时，单独 get_chat_info 拿名字。"""
    # 具体前缀必须先于通用 /im/v1/chats 注册（_match 按注册顺序找第一个命中）
    _FakeFeishuClient.routes["/im/v1/chats/oc_unknown"] = {"code": 0, "data": {"name": "外部协作群", "chat_mode": "group"}}
    _setup_p2p_routes()
    _FakeFeishuClient.routes["/search/v2/message"] = _search_payload(["om_x1"])
    _single_msg_routes({"om_x1": "oc_unknown"})
    r = await dispatch_tool(_ctx(), "feishu_chat_daily", {"days": 1})
    assert "外部协作群" in r


@pytest.mark.asyncio
async def test_daily_person_filter(_fake_feishu):
    _setup_p2p_routes()
    _FakeFeishuClient.routes["/search/v2/message"] = _search_payload(["om_g1", "om_p1"])
    _single_msg_routes({"om_g1": "oc_group1", "om_p1": "oc_p2p1"})
    r = await dispatch_tool(_ctx(), "feishu_chat_daily", {"person": "张三"})
    assert "私聊·张三" in r and "门店经营分析群" not in r


@pytest.mark.asyncio
async def test_daily_person_not_found(_fake_feishu):
    _setup_p2p_routes()
    _FakeFeishuClient.routes["/search/v2/message"] = _search_payload(["om_g1"])
    _single_msg_routes({"om_g1": "oc_group1"})
    r = await dispatch_tool(_ctx(), "feishu_chat_daily", {"person": "不存在的人"})
    assert "没有和" in r


@pytest.mark.asyncio
async def test_daily_rejected_without_switch():
    r = await dispatch_tool(_ctx(allow=False), "feishu_chat_daily", {})
    assert "未开通飞书内容读取" in r


@pytest.mark.asyncio
async def test_daily_no_token_guides(monkeypatch):
    async def _no_token(db, user):
        return None

    monkeypatch.setattr(tools, "get_feishu_user_token", _no_token)
    r = await dispatch_tool(_ctx(), "feishu_chat_daily", {})
    assert "/login?upgrade=1" in r


# ---------- 推送推给本人（push_feishu_card_to_me） ----------

@pytest.mark.asyncio
async def test_push_to_me_success(monkeypatch):
    sent = []

    async def _app_token(client):
        return "app_tok"

    async def _send(client, token, open_id, card):
        sent.append((token, open_id, card))

    monkeypatch.setattr("app.auth.feishu.app_access_token", _app_token)
    monkeypatch.setattr("app.feishu.client.send_p2p_card", _send)
    err = await feishu_push.push_feishu_card_to_me("ou_x", "【日报】运行成功", "正文")
    assert err is None
    assert sent[0][0] == "app_tok" and sent[0][1] == "ou_x"
    assert sent[0][2]["header"]["title"]["content"] == "【日报】运行成功"


@pytest.mark.asyncio
async def test_push_to_me_failure_returns_str(monkeypatch):
    from app.feishu.client import FeishuApiError

    async def _app_token(client):
        return "app_tok"

    async def _send_fail(client, token, open_id, card):
        raise FeishuApiError("Bot ability is not activated")

    monkeypatch.setattr("app.auth.feishu.app_access_token", _app_token)
    monkeypatch.setattr("app.feishu.client.send_p2p_card", _send_fail)
    err = await feishu_push.push_feishu_card_to_me("ou_x", "t", "正文")
    assert err and "Bot ability" in err  # 错误归一成字符串，不抛


@pytest.mark.asyncio
async def test_push_to_me_splits_long_content(monkeypatch):
    sent = []

    async def _app_token(client):
        return "app_tok"

    async def _send(client, token, open_id, card):
        sent.append(card)

    monkeypatch.setattr("app.auth.feishu.app_access_token", _app_token)
    monkeypatch.setattr("app.feishu.client.send_p2p_card", _send)
    monkeypatch.setattr(feishu_push, "CARD_BODY_CHARS", 10)  # 运行期读模块常量，可压小测拆片
    err = await feishu_push.push_feishu_card_to_me("ou_x", "t", "一二三四五六七八九十一二三四五六七八九十")
    assert err is None and len(sent) == 2
    assert "(1/2)" in sent[0]["header"]["title"]["content"]


# ---------- 推送配置校验（router._validate_push） ----------

def test_validate_push_mutual_exclusion():
    user = SimpleNamespace(feishu_open_id="ou_x")
    with pytest.raises(HTTPException):
        _validate_push(user, True, "https://open.feishu.cn/open-apis/bot/v2/hook/abc")
    _validate_push(user, True, None)   # 推本人 OK
    _validate_push(user, False, "https://open.feishu.cn/open-apis/bot/v2/hook/abc")  # 推群 OK
    _validate_push(user, False, None)  # 不推 OK


def test_validate_push_requires_feishu_binding():
    with pytest.raises(HTTPException):
        _validate_push(SimpleNamespace(feishu_open_id=None), True, None)
