"""M9a 逻辑测试：feishu_approval_daily 审批三段动态（tenant token + open_id 过滤）。"""
import json
from types import SimpleNamespace

import pytest

from app.runtime import tools
from app.runtime.tools import ToolContext, dispatch_tool


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


def _ctx(allow=True, open_id="ou_self"):
    return ToolContext(
        db=_FakeDB(),
        user=SimpleNamespace(id=1, feishu_open_id=open_id, username="u1"),
        agent=SimpleNamespace(id=1),
        allow_feishu=allow,
    )


# ---------- 假飞书 API（URL 前缀路由，同 test_m8_daily 约定） ----------

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

    async def _app_token(client):
        return "tenant_tok"

    monkeypatch.setattr(tools, "app_access_token", _app_token)
    yield
    _FakeFeishuClient.routes = {}
    _FakeFeishuClient.calls = []


def _task(name="美团管家权限申请", status="PENDING", ts="1760000000"):
    return {"approval": {"name": name, "code": "C-1"},
            "group": {}, "task": {"status": status},
            "instance": {"code": "INS-1", "status": "PENDING", "start_time": ts}}


def _instance(name="新店账套申请（美团管家）", status="PENDING", ts="1760000000"):
    return {"approval": {"name": name}, "group": {},
            "instance": {"code": "INS-2", "status": status, "start_time": ts}}


def _setup_routes():
    _FakeFeishuClient.routes["/approval/v4/tasks/search"] = {
        "code": 0, "data": {"task_list": [_task()], "has_more": False, "page_token": ""}}
    _FakeFeishuClient.routes["/approval/v4/instances/search_cc"] = {
        "code": 0, "data": {"cc_list": [_instance("抄送：门店装修预算", "APPROVED")],
                            "has_more": False, "page_token": ""}}
    _FakeFeishuClient.routes["/approval/v4/instances/query"] = {
        "code": 0, "data": {"instance_list": [_instance()], "has_more": False, "page_token": ""}}


@pytest.mark.asyncio
async def test_gate_off_refused(_fake_feishu):
    out = await dispatch_tool(_ctx(allow=False), "feishu_approval_daily", {})
    assert "未开通飞书内容读取能力" in out


@pytest.mark.asyncio
async def test_no_open_id_guides_binding(_fake_feishu):
    out = await dispatch_tool(_ctx(open_id=None), "feishu_approval_daily", {})
    assert "飞书登录" in out
    assert not _FakeFeishuClient.calls  # 没绑定就不该发任何请求


@pytest.mark.asyncio
async def test_three_sections_and_audit(_fake_feishu):
    _setup_routes()
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_approval_daily", {})
    assert "## 待我审批（1 条）" in out
    assert "美团管家权限申请" in out
    assert "## 我发起的（近 30 天）（1 条）" in out
    assert "新店账套申请（美团管家）" in out
    assert "PENDING" in out  # 我发起的带状态
    assert "## 抄送我的（近 30 天）（1 条）" in out
    assert "抄送：门店装修预算" in out
    assert "APPROVED" in out
    # 审计：计数不落正文
    audit = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.approval_daily"]
    assert len(audit) == 1
    assert "美团管家" not in audit[0].detail
    assert '"pending": 1' in audit[0].detail
    # 请求体按当前用户 open_id 过滤
    bodies = [c[1] for c in _FakeFeishuClient.calls if isinstance(c[1], dict)]
    assert bodies and all(b.get("user_id") == "ou_self" for b in bodies)


@pytest.mark.asyncio
async def test_pending_uses_pending_filter(_fake_feishu):
    _setup_routes()
    await dispatch_tool(_ctx(), "feishu_approval_daily", {})
    task_calls = [c for c in _FakeFeishuClient.calls if "/tasks/search" in c[0]]
    assert task_calls and task_calls[0][1].get("task_status_list") == ["PENDING"]


@pytest.mark.asyncio
async def test_partial_failure_keeps_other_sections(_fake_feishu):
    _setup_routes()
    _FakeFeishuClient.routes["/approval/v4/instances/query"] = {
        "code": 99991672, "data": {}, "msg": "Access denied"}
    out = await dispatch_tool(_ctx(), "feishu_approval_daily", {})
    assert "## 待我审批（1 条）" in out  # 其他段不受影响
    assert "## 我发起的（近 30 天）\n读取失败：Access denied" in out


@pytest.mark.asyncio
async def test_all_empty(_fake_feishu):
    _FakeFeishuClient.routes["/approval/v4/tasks/search"] = {
        "code": 0, "data": {"task_list": [], "has_more": False}}
    _FakeFeishuClient.routes["/approval/v4/instances/search_cc"] = {
        "code": 0, "data": {"cc_list": [], "has_more": False}}
    _FakeFeishuClient.routes["/approval/v4/instances/query"] = {
        "code": 0, "data": {"instance_list": [], "has_more": False}}
    out = await dispatch_tool(_ctx(), "feishu_approval_daily", {})
    assert out.count("（无）") == 3


def test_approval_ts_seconds_and_ms():
    assert tools._approval_ts("1760000000") == tools._approval_ts("1760000000000")
    assert tools._approval_ts("") == ""
    assert tools._approval_ts(None) == ""


# ---------- feishu_approval_detail（M9a.2） ----------

_DETAIL_CODE = "F5845B45-FD7E-46AE"
_DETAIL = {
    "code": 0,
    "data": {
        "approval_name": "费用申请（登月计划）",
        "instance_code": _DETAIL_CODE,
        "serial_number": "20260831001",
        "status": "pending",
        "start_time": "1760000000",
        "end_time": "0",
        "form": json.dumps([
            {"id": "w1", "name": "费用申请时是否有发票", "type": "radioV2", "value": "有发票"},
            {"id": "w2", "name": "事由", "type": "textarea", "value": "8月token充值费用"},
            {"id": "w3", "name": "说明 2", "type": "text", "value": "静态说明不渲染"},
            {"id": "w4", "name": "发票类型", "type": "checkboxV2", "value": ["全电发票"]},
            {"id": "w5", "name": "全电发票", "type": "fieldList", "value": [
                [{"id": "c1", "name": "金额", "value": "100"}, {"id": "c2", "name": "税率", "value": "6%"}],
                [{"id": "c1", "name": "金额", "value": "200"}, {"id": "c2", "name": "税率", "value": ""}],
            ]},
            {"id": "w6", "name": "空字段", "type": "input", "value": ""},
        ]),
        "task_list": [
            {"node_name": "审批", "open_id": "ou_approver1", "status": "APPROVED",
             "start_time": "1760000000000", "end_time": "1760003600000"},
            {"node_name": "审批", "open_id": "ou_approver2", "status": "PENDING",
             "start_time": "1760003600000", "end_time": "0"},
        ],
    },
}


def _detail_routes():
    _FakeFeishuClient.routes[f"/approval/v4/instances/{_DETAIL_CODE}"] = _DETAIL
    _FakeFeishuClient.routes["/contact/v3/users/batch"] = {"code": 0, "data": {"items": [
        {"open_id": "ou_approver1", "name": "王五"}, {"open_id": "ou_approver2", "name": "赵六"},
    ]}}


@pytest.mark.asyncio
async def test_detail_success_and_audit(_fake_feishu, monkeypatch):
    _detail_routes()

    async def _user_token(db, user):
        return "user_tok"

    monkeypatch.setattr(tools, "get_feishu_user_token", _user_token)
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_approval_detail", {"instance_code": _DETAIL_CODE})
    assert "## 费用申请（登月计划）" in out
    assert "状态：pending" in out and "单号：20260831001" in out
    assert "完结" not in out  # end_time="0" 未完结不显示完结时间
    # 表单：radio/textarea/checkbox/fieldList 各控件形态
    assert "费用申请时是否有发票：有发票" in out
    assert "事由：8月token充值费用" in out
    assert "静态说明不渲染" not in out  # text 静态说明控件跳过
    assert "发票类型：全电发票" in out
    assert "金额=100、税率=6%" in out and "金额=200" in out  # fieldList 明细行
    assert "空字段" not in out  # 空值字段跳过
    # 审批进度：contact 解析姓名 + 状态中文映射
    assert "### 审批进度" in out
    assert "王五：已同意" in out and "赵六：待处理" in out
    # 审计：只记 code/状态，不落表单内容
    audit = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.approval_detail"]
    assert len(audit) == 1
    assert _DETAIL_CODE in audit[0].detail
    assert "8月token充值费用" not in audit[0].detail


@pytest.mark.asyncio
async def test_detail_no_user_token_degrades_to_openid(_fake_feishu, monkeypatch):
    """用户没有有效 user token 时详情照常出，审批人降级为 open_id 前缀。"""
    _detail_routes()

    async def _no_token(db, user):
        return None

    monkeypatch.setattr(tools, "get_feishu_user_token", _no_token)
    out = await dispatch_tool(_ctx(), "feishu_approval_detail", {"instance_code": _DETAIL_CODE})
    assert "ou_approver1" in out and "待处理" in out
    assert not any("/contact/" in url for url, _ in _FakeFeishuClient.calls)  # 没 token 就不调 contact


@pytest.mark.asyncio
async def test_detail_without_form_degrades(_fake_feishu, monkeypatch):
    _FakeFeishuClient.routes[f"/approval/v4/instances/{_DETAIL_CODE}"] = {
        "code": 0, "data": {"approval_name": "合同申请", "status": "approved",
                            "start_time": "1760000000", "end_time": "1760001000"}}

    async def _no_token(db, user):
        return None

    monkeypatch.setattr(tools, "get_feishu_user_token", _no_token)
    out = await dispatch_tool(_ctx(), "feishu_approval_detail", {"instance_code": _DETAIL_CODE})
    assert "## 合同申请" in out and "（无表单字段或未返回）" in out
    assert "完结：" in out  # end_time 非 0 → 显示完结时间


@pytest.mark.asyncio
async def test_detail_gate_off_refused(_fake_feishu):
    out = await dispatch_tool(_ctx(allow=False), "feishu_approval_detail",
                              {"instance_code": _DETAIL_CODE})
    assert "未开通飞书内容读取能力" in out


@pytest.mark.asyncio
async def test_detail_missing_code(_fake_feishu):
    out = await dispatch_tool(_ctx(), "feishu_approval_detail", {})
    assert "请提供 instance_code" in out
    assert not _FakeFeishuClient.calls


def test_approval_widget_value_shapes():
    assert tools._approval_widget_value({"value": "有发票"}) == "有发票"
    assert tools._approval_widget_value({"value": ["a", "b"]}) == "a、b"
    assert tools._approval_widget_value({"value": None}) == ""
    assert tools._approval_widget_value({"value": ""}) == ""
    field_list = {"value": [[{"name": "金额", "value": "100"}, {"name": "空", "value": ""}]]}
    assert tools._approval_widget_value(field_list) == "金额=100"
    long_val = {"value": "长" * 500}
    assert len(tools._approval_widget_value(long_val)) == tools._APPROVAL_DETAIL_FIELD_MAX


def test_approval_line_includes_instance_code():
    line = tools._approval_line(_task(), with_status=False)
    assert "美团管家权限申请" in line and "INS-1" in line  # code 在行尾，供 detail 工具使用


@pytest.mark.asyncio
async def test_detail_serial_number_fallback(_fake_feishu, monkeypatch):
    """业务单号（纯数字流水号）→ 自动在三段列表里按 serial_id 匹配出真实实例 code 再查。"""
    serial_task = {"approval": {"name": "费用申请（登月计划）"}, "group": {}, "task": {"status": "PENDING"},
                   "instance": {"code": "INS-REAL-9", "serial_id": "202608280029",
                                "status": "PENDING", "start_time": "1760000000"}}
    _FakeFeishuClient.routes["/approval/v4/tasks/search"] = {
        "code": 0, "data": {"task_list": [serial_task], "has_more": False}}
    _FakeFeishuClient.routes["/approval/v4/instances/query"] = {
        "code": 0, "data": {"instance_list": [], "has_more": False}}
    _FakeFeishuClient.routes["/approval/v4/instances/search_cc"] = {
        "code": 0, "data": {"cc_list": [], "has_more": False}}
    _FakeFeishuClient.routes["/approval/v4/instances/INS-REAL-9"] = _DETAIL

    async def _no_token(db, user):
        return None

    monkeypatch.setattr(tools, "get_feishu_user_token", _no_token)
    out = await dispatch_tool(_ctx(), "feishu_approval_detail", {"instance_code": "202608280029"})
    assert "## 费用申请（登月计划）" in out  # 匹配成功，详情出来了
    # 纯单号不会直接调详情接口（必然失败），先走列表匹配
    assert not any("/instances/202608280029" in url for url, _ in _FakeFeishuClient.calls)
    assert any("/tasks/search" in url for url, _ in _FakeFeishuClient.calls)
    assert any("/instances/INS-REAL-9" in url for url, _ in _FakeFeishuClient.calls)


@pytest.mark.asyncio
async def test_detail_serial_no_match(_fake_feishu, monkeypatch):
    for key, lst in (("tasks/search", "task_list"), ("instances/query", "instance_list"),
                     ("instances/search_cc", "cc_list")):
        _FakeFeishuClient.routes[f"/approval/v4/{key}"] = {
            "code": 0, "data": {lst: [], "has_more": False}}

    async def _no_token(db, user):
        return None

    monkeypatch.setattr(tools, "get_feishu_user_token", _no_token)
    out = await dispatch_tool(_ctx(), "feishu_approval_detail", {"instance_code": "999999999999"})
    assert "没找到" in out and "GUID" in out
    assert not any("instances/999999999999" in url for url, _ in _FakeFeishuClient.calls)
