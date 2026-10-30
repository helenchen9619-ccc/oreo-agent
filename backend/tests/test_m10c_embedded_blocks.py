"""M10c 逻辑测试：feishu_doc_read 自动发现 docx 内嵌电子表格/多维表格并附带读取。

契约：
- 内嵌块发现：GET /docx/v1/documents/{id}/blocks → items[{block_type, sheet:{token}, bitable:{token}}]
- token 格式：sheet.token={spreadsheet_token}_{sheet_id}，bitable.token={app_token}_{table_id}
  （按字段存在性判定，不写死 block_type 数值——各来源口径不一）
- 内嵌 sheet 读取复用 /sheets/v2/.../values/{sheet_id}!A1:..；bitable 复用 .../records
- 逐块降级：单块失败只换提示行不拖垮正文；缺对应 scope 不发起请求直接给授权提示
- 有内嵌块时正文截到 5000 给内嵌腾预算，整体仍封顶 _FEISHU_TEXT_MAX
"""
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


_ALL_SCOPES = ("docx:document:readonly wiki:wiki:readonly drive:drive:readonly "
               "sheets:spreadsheet:readonly bitable:app:readonly")


def _ctx(scopes=_ALL_SCOPES):
    return ToolContext(
        db=_FakeDB(),
        user=SimpleNamespace(id=1, username="u1", feishu_scopes=scopes),
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


_DOC_URL = "https://x.feishu.cn/docx/DocToken1234567890abcd"


def _register_doc(content="文档正文内容", blocks=None):
    """注册 docx 的 raw_content + blocks 两条路由（更具体的前缀先注册防子串误配）。"""
    _FakeFeishuClient.routes["/raw_content"] = {"code": 0, "data": {"content": content}}
    _FakeFeishuClient.routes["/blocks"] = {"code": 0, "data": {"items": blocks or [], "has_more": False}}


def _sheet_block(ss_token, sheet_id):
    return {"block_id": "b1", "block_type": 30, "sheet": {"token": f"{ss_token}_{sheet_id}"}}


def _bitable_block(app_token, table_id):
    return {"block_id": "b2", "block_type": 18, "bitable": {"token": f"{app_token}_{table_id}"}}


_VALUES = {"code": 0, "data": {"valueRange": {"values": [
    ["物料", "用量", "单位"], ["鸡腿肉", 120, "kg"], ["生菜", 45, "kg"],
]}}}


# ---------- 内嵌 sheet ----------

@pytest.mark.asyncio
async def test_embedded_sheet_appended_after_text(_fake_feishu):
    _register_doc(blocks=[_sheet_block("SSabc123", "sheetX"), {"block_id": "b9", "block_type": 2}])
    _FakeFeishuClient.routes["/values/"] = _VALUES
    ctx = _ctx()
    r = await dispatch_tool(ctx, "feishu_doc_read", {"url": _DOC_URL})
    assert "文档正文内容" in r                      # 正文在前
    assert "【内嵌电子表格 1】" in r                 # 节标
    assert "鸡腿肉" in r and "生菜" in r            # TSV 内容
    assert "feishu.cn/sheets/SSabc123" in r         # 独立链接提示（可分页读全量）
    val_calls = [u for u in _FakeFeishuClient.calls if "/values/" in u]
    assert len(val_calls) == 1 and "sheetX!" in val_calls[0]   # 用内嵌 sheet_id 直接读
    audits = [x for x in ctx.db.added if getattr(x, "action", "") == "feishu.doc_read"]
    detail = json.loads(audits[0].detail)
    assert detail["embedded"] == {"sheet": 1, "bitable": 0}
    assert "user_tok" not in audits[0].detail


@pytest.mark.asyncio
async def test_embedded_sheet_without_scope_no_request(_fake_feishu):
    _register_doc(blocks=[_sheet_block("SSabc123", "sheetX")])
    _FakeFeishuClient.routes["/values/"] = _VALUES
    ctx = _ctx(scopes="docx:document:readonly")  # 缺 sheets scope
    r = await dispatch_tool(ctx, "feishu_doc_read", {"url": _DOC_URL})
    assert "文档正文内容" in r                       # 正文不受影响
    assert "/login?upgrade=1" in r                   # 授权提示
    assert not any("/values/" in u for u in _FakeFeishuClient.calls)  # 不发起读值请求


@pytest.mark.asyncio
async def test_embedded_sheet_error_degrades_not_fails(_fake_feishu):
    _register_doc(blocks=[_sheet_block("SSabc123", "sheetX")])
    # 不注册 /values/ → code 999 no route → FeishuApiError
    r = await dispatch_tool(_ctx(), "feishu_doc_read", {"url": _DOC_URL})
    assert "文档正文内容" in r
    assert "读取失败" in r and "feishu.cn/sheets/SSabc123" in r


@pytest.mark.asyncio
async def test_embedded_sheet_cap_and_overflow_note(_fake_feishu):
    blocks = [_sheet_block(f"SS{i:03d}", f"sheet{i}") for i in range(4)]
    _register_doc(blocks=blocks)
    _FakeFeishuClient.routes["/values/"] = _VALUES
    r = await dispatch_tool(_ctx(), "feishu_doc_read", {"url": _DOC_URL})
    val_calls = [u for u in _FakeFeishuClient.calls if "/values/" in u]
    assert len(val_calls) == tools._EMBED_SHEET_MAX == 3
    assert "还有 1 个内嵌电子表格未自动读取" in r
    assert "feishu.cn/sheets/SS003" in r  # 未读的也给链接


# ---------- 内嵌 bitable ----------

@pytest.mark.asyncio
async def test_embedded_bitable_appended(_fake_feishu):
    _register_doc(blocks=[_bitable_block("AppTok456", "tblY")])
    _FakeFeishuClient.routes["/records"] = {"code": 0, "data": {"items": [
        {"record_id": "rec1", "fields": {"品名": [{"text": "鸭脖"}], "库存": 88}},
    ]}}
    r = await dispatch_tool(_ctx(), "feishu_doc_read", {"url": _DOC_URL})
    assert "【内嵌多维表格 1】" in r
    assert "鸭脖" in r and "库存: 88" in r
    assert "feishu.cn/base/AppTok456" in r


# ---------- 边界 ----------

@pytest.mark.asyncio
async def test_no_embedded_blocks_behaves_as_before(_fake_feishu):
    _register_doc(blocks=[{"block_id": "p1", "block_type": 2}, {"block_id": "i1", "block_type": 27}])
    r = await dispatch_tool(_ctx(), "feishu_doc_read", {"url": _DOC_URL})
    assert r == "文档正文内容"  # 与 M10c 之前完全一致（无标题、无内嵌节）


@pytest.mark.asyncio
async def test_empty_text_with_embedded_still_returns(_fake_feishu):
    _register_doc(content="", blocks=[_sheet_block("SSabc123", "sheetX")])
    _FakeFeishuClient.routes["/values/"] = _VALUES
    r = await dispatch_tool(_ctx(), "feishu_doc_read", {"url": _DOC_URL})
    assert "文档内容为空" not in r     # 不再报空文档兜底
    assert "鸡腿肉" in r


@pytest.mark.asyncio
async def test_embedded_truncates_text_to_leave_budget(_fake_feishu):
    _register_doc(content="长" * (tools._FEISHU_TEXT_MAX + 100),
                  blocks=[_sheet_block("SSabc123", "sheetX")])
    _FakeFeishuClient.routes["/values/"] = _VALUES
    r = await dispatch_tool(_ctx(), "feishu_doc_read", {"url": _DOC_URL})
    assert len(r) <= tools._FEISHU_TEXT_MAX + 50
    assert "正文过长已截断" in r and "鸡腿肉" in r


# ---------- M10c.1：内嵌块章节归属标注 ----------

def _heading(level, text, extra_elements=None):
    """构造标题块：elements 为 text_run 列表（可混入非 text_run 元素）。"""
    els = [{"text_run": {"content": t}} for t in ([text] if isinstance(text, str) else text)]
    els += extra_elements or []
    return {"block_id": f"h{text!r}", "block_type": 2 + level, f"heading{level}": {"elements": els}}


@pytest.mark.asyncio
async def test_embedded_section_labeled_with_heading_path(_fake_feishu):
    """标题栈：同级覆盖、深层随同级标题出现而失效（模拟早会文档多区域结构）。"""
    blocks = [
        _heading(1, "26年9月14日"),
        _heading(2, "运营"),
        _heading(2, "周震区域"),          # 同级覆盖「运营」？不——同级覆盖的是上一个 heading2
        _heading(3, "上周总结"),
        _sheet_block("SSzhou", "sZ"),
        _heading(2, "付媛区域"),          # heading2 出现 → heading3「上周总结」失效
        _sheet_block("SSfu", "sF"),
    ]
    _register_doc(blocks=blocks)
    _FakeFeishuClient.routes["/values/"] = _VALUES
    r = await dispatch_tool(_ctx(), "feishu_doc_read", {"url": _DOC_URL})
    assert "【内嵌电子表格 1】位于「26年9月14日 > 周震区域 > 上周总结」" in r
    assert "【内嵌电子表格 2】位于「26年9月14日 > 付媛区域」" in r
    assert "付媛区域 > 上周总结" not in r   # 旧深层标题不得残留


@pytest.mark.asyncio
async def test_embedded_without_headings_no_location_label(_fake_feishu):
    _register_doc(blocks=[_sheet_block("SSabc123", "sheetX")])
    _FakeFeishuClient.routes["/values/"] = _VALUES
    r = await dispatch_tool(_ctx(), "feishu_doc_read", {"url": _DOC_URL})
    assert "【内嵌电子表格 1】" in r and "位于" not in r  # 无标题块不硬凑


@pytest.mark.asyncio
async def test_heading_with_non_text_run_elements(_fake_feishu):
    """标题里混入 mention_doc 等非 text_run 元素：不崩，只取 text_run 文本。"""
    blocks = [
        _heading(2, "周震区域", extra_elements=[{"mention_doc": {"token": "x"}}, {"equation": {}}]),
        _sheet_block("SSabc123", "sheetX"),
    ]
    _register_doc(blocks=blocks)
    _FakeFeishuClient.routes["/values/"] = _VALUES
    r = await dispatch_tool(_ctx(), "feishu_doc_read", {"url": _DOC_URL})
    assert "位于「周震区域」" in r
