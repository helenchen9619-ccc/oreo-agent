"""M9c 逻辑测试：zhishu_contract_mine 合同日报（tenant token + zhishu_user_id 过滤）。

契约全部来自 2026-09-01 云上探针（check_zhishu_contract.py v6/v7）：
- token：POST /auth/v3/tenant_access_token/internal，camelCase body，顶层 tenant_access_token + expire 秒
- 搜索必须带 combine_condition；user_id_type 只有 user_id 真正过滤（create_user_name 等被忽略）
- permission=1 我的可见 / 2 我申请的
- item 含 contract_name/number/status_code/status_name/counter_party_list/amount+currency_name/create_user_name 等
"""
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


_ZHISHU_USER_ID = "944f4998"


def _ctx(zsid=_ZHISHU_USER_ID, allow_zhishu=True):
    return ToolContext(
        db=_FakeDB(),
        user=SimpleNamespace(id=1, username="u1", zhishu_user_id=zsid),
        agent=SimpleNamespace(id=1),
        allow_zhishu=allow_zhishu,
    )


# ---------- 假智书 API（URL 前缀路由，同 test_m8_daily 约定） ----------

class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _FakeZhishuClient:
    routes: dict = {}
    calls: list = []

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _match(self, url, extra):
        _FakeZhishuClient.calls.append((url, extra))
        for prefix, payload in _FakeZhishuClient.routes.items():
            if prefix in url:
                return _FakeResp(payload(extra) if callable(payload) else payload)
        return _FakeResp({"code": 999, "msg": "no route"})

    async def get(self, url, headers=None, params=None):
        return self._match(url, params)

    async def post(self, url, headers=None, json=None, params=None):
        return self._match(url, json)


@pytest.fixture
def _fake_zhishu(monkeypatch):
    _FakeZhishuClient.routes = {}
    _FakeZhishuClient.calls = []
    monkeypatch.setattr(tools.httpx, "AsyncClient", _FakeZhishuClient)
    # 给 zhishu_token 一个假凭证（不然它先报「未配置智书凭证」）
    from app.zhishu import client as zc
    monkeypatch.setattr(zc, "get_settings",
                        lambda: SimpleNamespace(ZHISHU_APP_ID="app_id", ZHISHU_APP_SECRET="app_secret"))
    zc._token_cache["token"] = ""
    zc._token_cache["expires_at"] = 0.0
    yield
    _FakeZhishuClient.routes = {}
    _FakeZhishuClient.calls = []


def _setup_routes(items: list[dict], token_ok=True):
    _FakeZhishuClient.routes["/auth/v3/tenant_access_token/internal"] = (
        {"code": 0, "expire": 7200, "msg": "ok", "tenant_access_token": "zs_tok"}
        if token_ok else {"code": 999, "msg": "appSecret invalid"}
    )
    _FakeZhishuClient.routes["/contract/v1/contracts/search"] = {
        "code": 0, "msg": "success",
        "data": {"items": items, "has_more": False, "page_token": ""},
    }


def _contract(name, status_code=3, status_name="process", number="CT2026-001",
              amount=100, currency="人民币元", creator="王思远", cps=None):
    return {
        "contract_name": name,
        "contract_number": number,
        "contract_status_code": status_code,
        "contract_status_name": status_name,
        "counter_party_list": [{"counter_party_name": n} for n in (cps or [])],
        "amount": amount,
        "currency_name": currency,
        "submitter_user_name": creator,
        "create_user_name": creator,
    }


@pytest.mark.asyncio
async def test_gate_off_refused(_fake_zhishu):
    out = await dispatch_tool(_ctx(allow_zhishu=False), "zhishu_contract_mine", {})
    assert "未开通智书合同读取能力" in out
    assert not _FakeZhishuClient.calls


@pytest.mark.asyncio
async def test_no_zhishu_user_id_guides_admin(_fake_zhishu):
    out = await dispatch_tool(_ctx(zsid=""), "zhishu_contract_mine", {})
    assert "智书 user_id" in out and "管理员" in out
    assert not _FakeZhishuClient.calls  # 没配 user_id 不该发任何请求


@pytest.mark.asyncio
async def test_token_invalid_guides_admin(_fake_zhishu):
    from app.zhishu import client as zc
    zc._token_cache["token"] = ""
    zc._token_cache["expires_at"] = 0.0
    _setup_routes([], token_ok=False)
    out = await dispatch_tool(_ctx(), "zhishu_contract_mine", {})
    assert "智书合同读取失败" in out
    assert "appSecret" in out or "凭证" in out


@pytest.mark.asyncio
async def test_mine_success_and_audit(_fake_zhishu, monkeypatch):
    # 重置 token 缓存，确保走假 client 拿 token
    from app.zhishu import client as zc
    zc._token_cache["token"] = ""
    zc._token_cache["expires_at"] = 0.0
    _setup_routes([
        _contract("主数据框架合同-云谷智造", status_code=3, status_name="process",
                  number="CT2026-001", amount=100, cps=["上海云杉供应链管理有限公司"]),
        _contract("对公打款授权书", status_code=7, status_name="signed",
                  number="CT2026-002", amount=5000, cps=["康宁（江苏）健康科技有限公司"]),
    ])
    ctx = _ctx()
    out = await dispatch_tool(ctx, "zhishu_contract_mine", {})
    assert "「我可见」的智书合同共 2 条" in out
    assert "[审批中] 主数据框架合同-云谷智造" in out or "主数据框架合同-云谷智造" in out
    assert "上海云杉供应链管理有限公司" in out
    assert "CT2026-001" in out
    # 审计：只记 permission + 计数，不落合同名/金额
    audit = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.zhishu_contract_mine"]
    assert len(audit) == 1
    assert '"permission": 1' in audit[0].detail and '"items": 2' in audit[0].detail
    assert "主数据" not in audit[0].detail and "100" not in audit[0].detail


@pytest.mark.asyncio
async def test_permission_2_uses_my_applied(_fake_zhishu):
    from app.zhishu import client as zc
    zc._token_cache["token"] = ""
    zc._token_cache["expires_at"] = 0.0
    _setup_routes([_contract("申请函V2")])
    await dispatch_tool(_ctx(), "zhishu_contract_mine", {"permission": 2})
    search_calls = [extra for url, extra in _FakeZhishuClient.calls
                    if "contracts/search" in url and isinstance(extra, dict)]
    assert search_calls
    assert search_calls[0].get("combine_condition", {}).get("permission") == 2


@pytest.mark.asyncio
async def test_empty_result(_fake_zhishu):
    from app.zhishu import client as zc
    zc._token_cache["token"] = ""
    zc._token_cache["expires_at"] = 0.0
    _setup_routes([])
    out = await dispatch_tool(_ctx(), "zhishu_contract_mine", {})
    assert "共 0 条" in out


@pytest.mark.asyncio
async def test_max_items_capped(_fake_zhishu):
    from app.zhishu import client as zc
    zc._token_cache["token"] = ""
    zc._token_cache["expires_at"] = 0.0
    _setup_routes([_contract("x")])
    await dispatch_tool(_ctx(), "zhishu_contract_mine", {"max_items": 999})
    search_calls = [extra for url, extra in _FakeZhishuClient.calls
                    if "contracts/search" in url and isinstance(extra, dict)]
    assert search_calls[0].get("page_size") == 50


# ---------- M9c.2：query 关键词（contract_name 子串模糊，探针 v8 实测） ----------

@pytest.mark.asyncio
async def test_query_passed_to_contract_name(_fake_zhishu):
    """query → combine_condition.contract_name，且 user_id/permission 过滤不变。"""
    from app.zhishu import client as zc
    zc._token_cache["token"] = ""
    zc._token_cache["expires_at"] = 0.0
    _setup_routes([_contract("申请函V2（小程序主体统一申请）")])
    await dispatch_tool(_ctx(), "zhishu_contract_mine", {"query": "申请函"})
    search_calls = [extra for url, extra in _FakeZhishuClient.calls
                    if "contracts/search" in url and isinstance(extra, dict)]
    assert search_calls
    cc = search_calls[0].get("combine_condition", {})
    assert cc.get("contract_name") == "申请函"
    assert cc.get("user_id") == _ZHISHU_USER_ID and cc.get("permission") == 1


@pytest.mark.asyncio
async def test_query_no_query_omits_contract_name(_fake_zhishu):
    """不传 query 时不带 contract_name 字段（回归：全量列表行为不变）。"""
    from app.zhishu import client as zc
    zc._token_cache["token"] = ""
    zc._token_cache["expires_at"] = 0.0
    _setup_routes([_contract("x")])
    await dispatch_tool(_ctx(), "zhishu_contract_mine", {})
    search_calls = [extra for url, extra in _FakeZhishuClient.calls
                    if "contracts/search" in url and isinstance(extra, dict)]
    assert "contract_name" not in search_calls[0].get("combine_condition", {})


@pytest.mark.asyncio
async def test_query_hits_header_and_audit(_fake_zhishu):
    from app.zhishu import client as zc
    zc._token_cache["token"] = ""
    zc._token_cache["expires_at"] = 0.0
    _setup_routes([_contract("申请函V2（小程序主体统一申请）")])
    ctx = _ctx()
    out = await dispatch_tool(ctx, "zhishu_contract_mine", {"query": "申请函"})
    assert "名称含「申请函」" in out and "共 1 条" in out
    assert "申请函V2（小程序主体统一申请）" in out
    audit = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.zhishu_contract_mine"]
    assert len(audit) == 1 and '"has_query": true' in audit[0].detail
    assert "申请函" not in audit[0].detail  # 审计不落关键词原文


@pytest.mark.asyncio
async def test_query_no_hits_guides(_fake_zhishu):
    from app.zhishu import client as zc
    zc._token_cache["token"] = ""
    zc._token_cache["expires_at"] = 0.0
    _setup_routes([])
    out = await dispatch_tool(_ctx(), "zhishu_contract_mine", {"query": "不存在xyz"})
    assert "共 0 条" in out and "换个关键词" in out


# ---------- M9c.3：时间窗 + 状态过滤 + 自动翻页 ----------

def _setup_paged_routes(pages: list[list[dict]]):
    """搜索路由按 page_token 翻页：第 i 页 page_token=str(i)，has_more=还有下一页。"""
    _FakeZhishuClient.routes["/auth/v3/tenant_access_token/internal"] = {
        "code": 0, "expire": 7200, "msg": "ok", "tenant_access_token": "zs_tok"}

    def _search(body):
        pt = (body or {}).get("page_token") or ""
        idx = int(pt) if pt else 0
        items = pages[idx] if idx < len(pages) else []
        nxt = str(idx + 1) if idx + 1 < len(pages) else ""
        return {"code": 0, "msg": "success",
                "data": {"items": items, "has_more": bool(nxt), "page_token": nxt}}

    _FakeZhishuClient.routes["/contract/v1/contracts/search"] = _search


def _reset_token():
    from app.zhishu import client as zc
    zc._token_cache["token"] = ""
    zc._token_cache["expires_at"] = 0.0


def _last_cc() -> dict:
    calls = [extra for url, extra in _FakeZhishuClient.calls
             if "contracts/search" in url and isinstance(extra, dict)]
    assert calls
    return calls[0].get("combine_condition", {})


@pytest.mark.asyncio
async def test_days_sets_submit_time_range(_fake_zhishu):
    """days=7 → combine_condition 带 submited_time_start/end（默认 submit 口径），格式 'YYYY-MM-DD HH:MM:SS'。"""
    _reset_token()
    _setup_routes([_contract("x")])
    out = await dispatch_tool(_ctx(), "zhishu_contract_mine", {"days": 7})
    cc = _last_cc()
    import re as _re
    assert _re.fullmatch(r"\d{4}-\d{2}-\d{2} 00:00:00", cc.get("submited_time_start", ""))
    assert _re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", cc.get("submited_time_end", ""))
    assert cc["submited_time_start"] < cc["submited_time_end"]
    assert "近 7 天提交审批" in out


@pytest.mark.asyncio
async def test_explicit_dates_override_days(_fake_zhishu):
    """start_date/end_date 优先于 days，补全到当天 00:00:00 / 23:59:59。"""
    _reset_token()
    _setup_routes([_contract("x")])
    out = await dispatch_tool(_ctx(), "zhishu_contract_mine",
                              {"days": 7, "start_date": "2026-08-01", "end_date": "2026-08-31"})
    cc = _last_cc()
    assert cc.get("submited_time_start") == "2026-08-01 00:00:00"
    assert cc.get("submited_time_end") == "2026-08-31 23:59:59"
    assert "2026-08-01 ~ 2026-08-31" in out


@pytest.mark.asyncio
async def test_time_field_create_maps_create_time(_fake_zhishu):
    """time_field=create → create_time_*，且不带 submited_time_*。"""
    _reset_token()
    _setup_routes([_contract("x")])
    await dispatch_tool(_ctx(), "zhishu_contract_mine", {"days": 3, "time_field": "create"})
    cc = _last_cc()
    assert "create_time_start" in cc and "submited_time_start" not in cc


@pytest.mark.asyncio
async def test_status_in_passthrough(_fake_zhishu):
    _reset_token()
    _setup_routes([_contract("x")])
    await dispatch_tool(_ctx(), "zhishu_contract_mine", {"status_in": "3,6"})
    assert _last_cc().get("contract_status_in") == "3,6"


@pytest.mark.asyncio
async def test_no_time_params_omit_time_fields(_fake_zhishu):
    """回归：不传时间参数时 combine_condition 不带任何时间字段。"""
    _reset_token()
    _setup_routes([_contract("x")])
    await dispatch_tool(_ctx(), "zhishu_contract_mine", {})
    cc = _last_cc()
    assert not [k for k in cc if k.endswith("_time_start") or k.endswith("_time_end")]
    assert "contract_status_in" not in cc


@pytest.mark.asyncio
async def test_pagination_aggregates_pages(_fake_zhishu):
    """has_more 时自动翻页聚合：2 页合同全部出现在结果里。"""
    _reset_token()
    _setup_paged_routes([[_contract("第一页合同")], [_contract("第二页合同")]])
    out = await dispatch_tool(_ctx(), "zhishu_contract_mine", {})
    assert "共 2 条" in out and "第一页合同" in out and "第二页合同" in out


@pytest.mark.asyncio
async def test_pagination_cap_truncated_notice(_fake_zhishu):
    """满 5 页仍 has_more → 结果注明已达单次上限。"""
    _reset_token()
    _setup_paged_routes([[_contract(f"合同p{i}")] for i in range(6)])  # 6 页，永远 has_more
    out = await dispatch_tool(_ctx(), "zhishu_contract_mine", {})
    assert "共 5 条" in out and "已达单次上限" in out


# ---------- _zhishu_line 单元 ----------

def test_zhishu_line_full():
    line = tools._zhishu_line({
        "contract_name": "申请函V2", "contract_number": "CT2026-001",
        "contract_status_code": 3, "contract_status_name": "process",
        "counter_party_list": [{"counter_party_name": "康宁（江苏）健康科技有限公司"}],
        "amount": 5000, "currency_name": "人民币元",
        "submitter_user_name": "王思远",
    })
    assert line.startswith("- [process] 申请函V2（")  # status_name 是英文原样展示
    assert "编号CT2026-001" in line
    assert "对方康宁（江苏）健康科技有限公司" in line
    assert "5000 人民币元" in line
    assert "发起人王思远" in line


def test_zhishu_line_minimal():
    line = tools._zhishu_line({"contract_name": "", "contract_status_code": None})
    assert line == "- [] （无名称）（对方—）"  # counter_party 永远显示（空就是 —）


def test_zhishu_line_no_counterparty():
    line = tools._zhishu_line({
        "contract_name": "x", "contract_status_name": "signed",
        "counter_party_list": [], "amount": None, "currency_name": "",
        "submitter_user_name": "",
    })
    assert "对方—" in line
    assert "发起人" not in line


def test_zhishu_line_no_counterparty():
    line = tools._zhishu_line({
        "contract_name": "x", "contract_status_name": "signed",
        "counter_party_list": [], "amount": None, "currency_name": "",
        "submitter_user_name": "",
    })
    assert "对方—" in line
    assert "发起人" not in line


# ---------- zhishu token 缓存 ----------

@pytest.mark.asyncio
async def test_zhishu_token_cached(_fake_zhishu, monkeypatch):
    from app.zhishu import client as zc
    # 重置缓存
    zc._token_cache["token"] = ""
    zc._token_cache["expires_at"] = 0.0
    calls = []

    async def _post(url, **kw):
        calls.append(url)
        return _FakeResp({"code": 0, "expire": 7200, "tenant_access_token": "tok_1"})

    c = _FakeZhishuClient()
    c.post = _post
    t1 = await zc.zhishu_token(c)
    t2 = await zc.zhishu_token(c)
    assert t1 == "tok_1" and t2 == "tok_1"
    assert len(calls) == 1  # 第二次命中缓存


@pytest.mark.asyncio
async def test_zhishu_token_no_creds(_fake_zhishu, monkeypatch):
    from app.zhishu import client as zc
    zc._token_cache["token"] = ""
    zc._token_cache["expires_at"] = 0.0
    monkeypatch.setattr(zc, "get_settings", lambda: SimpleNamespace(ZHISHU_APP_ID="", ZHISHU_APP_SECRET=""))
    with pytest.raises(tools.ZhishuApiError, match="未配置智书凭证"):
        await zc.zhishu_token(_FakeZhishuClient())
