"""M34 测试：飞书表格读取翻页 + 大表 CSV 导出通道（bitable + sheet）。

契约（2026-10-13）：
- bitable list records：page_size≤100、page_token 翻页、响应带 total/has_more；
  接口限流 100 次/分钟 → CSV 大批量导出页间 sleep 0.3s 节流（测试中 monkeypatch 掉）
- sheet values：按 range 分块读（500 行/块），飞书裁掉块尾空行 → 短块/空块收尾
- text 模式 = 预览（如实报真实总条数，「只能读 50 条」是平台自己的旧预算、不是接口限制）；
  format=csv = 全量（≤1 万行）落工作区 feishu_files/（utf-8 BOM），引导 run_code(pandas)
fake 坑沿用：前缀路由更具体的先注册（"/records"、"/fields" 先于 "/tables"）。
"""
import csv
import io
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

from app.feishu import client as feishu_client
from app.runtime import tools
from app.runtime.tools import ToolContext, dispatch_tool

pytestmark = pytest.mark.asyncio


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
def _fake_feishu(monkeypatch, tmp_path):
    _FakeFeishuClient.routes = {}
    _FakeFeishuClient.calls = []
    monkeypatch.setattr(tools.httpx, "AsyncClient", _FakeFeishuClient)

    async def _token(db, user):
        return "user_tok"

    monkeypatch.setattr(tools, "get_feishu_user_token", _token)
    monkeypatch.setattr(tools, "_workspace_root", lambda uid, aid: tmp_path)
    yield tmp_path
    _FakeFeishuClient.routes = {}
    _FakeFeishuClient.calls = []


@pytest.fixture
def _no_sleep(monkeypatch):
    """翻页节流 sleep 打桩为计数（不影响事件循环）。"""
    calls = []

    async def _sleep(sec):
        calls.append(sec)

    monkeypatch.setattr(tools.asyncio, "sleep", _sleep)
    return calls


# ---------- 路由辅助 ----------

def _paged_records(pages: list[list[dict]], total: int):
    """按 page_token（=页序号字符串）翻页的记录假数据。"""

    def _payload(params):
        idx = int((params or {}).get("page_token") or 0)
        nxt = str(idx + 1) if idx + 1 < len(pages) else ""
        return {"code": 0, "data": {
            "total": total, "has_more": bool(nxt), "page_token": nxt,
            "items": pages[idx],
        }}

    return _payload


def _rec(rid, **fields):
    return {"record_id": rid, "fields": fields}


_BASE_TABLES = {"code": 0, "data": {"items": [
    {"table_id": "tbl1", "name": "合同台账"}, {"table_id": "tbl2", "name": "归档"},
]}}


# ---------- client.bitable_records 翻页 ----------

async def test_bitable_records_pagination_and_total(_fake_feishu):
    pages = [[_rec("r1"), _rec("r2")], [_rec("r3"), _rec("r4")], [_rec("r5")]]
    _FakeFeishuClient.routes["/records"] = _paged_records(pages, total=5)
    records, total = await feishu_client.bitable_records(
        _FakeFeishuClient(), "tok", "app1", "tbl1", max_items=100)
    assert [r["record_id"] for r in records] == ["r1", "r2", "r3", "r4", "r5"]
    assert total == 5
    tokens = [p.get("page_token") for _, p in _FakeFeishuClient.calls]
    assert tokens == [None, "1", "2"]  # 首页不带 token，后续带


async def test_bitable_records_max_items_stops(_fake_feishu):
    pages = [[_rec("r1"), _rec("r2"), _rec("r3")], [_rec("r4")]]
    _FakeFeishuClient.routes["/records"] = _paged_records(pages, total=4)
    records, total = await feishu_client.bitable_records(
        _FakeFeishuClient(), "tok", "app1", "tbl1", max_items=3)
    assert len(records) == 3 and total == 4
    assert len(_FakeFeishuClient.calls) == 1  # 攒够即停，不再翻页
    assert _FakeFeishuClient.calls[0][1]["page_size"] == 3


# ---------- _read_bitable text 预览 ----------

async def test_read_bitable_text_reports_real_total(_fake_feishu):
    _FakeFeishuClient.routes["/records"] = {"code": 0, "data": {
        "total": 230, "has_more": False,
        "items": [_rec("rec1", 标题=[{"text": "合同A"}], 金额=5000),
                  _rec("rec2", 标题=[{"text": "合同B"}], 金额=300),
                  _rec("rec3", 标题=[{"text": "合同C"}], 金额=100)],
    }}
    _FakeFeishuClient.routes["/tables"] = _BASE_TABLES
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_doc_read",
                              {"url": "https://xxx.feishu.cn/base/BaseTok1234567890abc"})
    assert "共 230 条记录，预览前 3 条" in out
    assert "还有 227 条未读" in out and "format='csv'" in out
    assert "record_id: rec1" in out
    audit = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.doc_read"]
    detail = json.loads(audit[0].detail)
    assert detail["total"] == 230 and detail["format"] == "text" and detail["rows"] == 3
    assert "合同A" not in audit[0].detail  # 审计不落记录正文


async def test_read_bitable_max_records_clamp(_fake_feishu):
    seen = []

    def _records(params):
        seen.append(params.get("page_size"))
        return {"code": 0, "data": {"total": 1, "has_more": False,
                                    "items": [_rec("rec1", 标题="x")]}}

    _FakeFeishuClient.routes["/records"] = _records
    _FakeFeishuClient.routes["/tables"] = _BASE_TABLES
    await dispatch_tool(_ctx(), "feishu_doc_read",
                        {"url": "https://xxx.feishu.cn/base/BaseTok1234567890abc",
                         "max_records": 5000})
    assert seen == [100]  # 夹到 1000 预算后 page_size 仍为接口上限 100


async def test_read_bitable_max_records_invalid(_fake_feishu):
    _FakeFeishuClient.routes["/tables"] = _BASE_TABLES
    out = await dispatch_tool(_ctx(), "feishu_doc_read",
                              {"url": "https://xxx.feishu.cn/base/BaseTok1234567890abc",
                               "max_records": "abc"})
    assert "max_records 必须是整数" in out


# ---------- _read_bitable csv 导出 ----------

async def test_bitable_csv_export(_fake_feishu, _no_sleep, tmp_path):
    page1 = [_rec(f"rec{i}", 标题=[{"text": f"合同{i}"}], 金额=i * 100,
                  备注='含,逗号"引号"') for i in range(3)]
    page2 = [_rec("rec9", 标题=[{"text": "合同尾"}], 金额=900, 系统列="公式值")]
    _FakeFeishuClient.routes["/records"] = _paged_records([page1, page2], total=4)
    _FakeFeishuClient.routes["/fields"] = {"code": 0, "data": {"items": [
        {"field_id": "f1", "field_name": "标题", "type": 1},
        {"field_id": "f2", "field_name": "金额", "type": 2},
        {"field_id": "f3", "field_name": "备注", "type": 1},
    ]}}
    _FakeFeishuClient.routes["/tables"] = _BASE_TABLES
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_doc_read",
                              {"url": "https://xxx.feishu.cn/base/BaseTok1234567890abc",
                               "format": "csv"})
    assert "已导出到工作区：feishu_files/" in out and "run_code" in out
    assert "共 4 行全量" in out
    assert _no_sleep == [tools._CSV_PAGE_INTERVAL]  # 两页之间节流一次
    rows = [x for x in ctx.db.added if getattr(x, "filename", "").startswith("feishu_files/")]
    assert len(rows) == 1 and rows[0].mime == "text/csv"
    data = Path(rows[0].storage_path).read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")  # utf-8 BOM
    table = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    assert table[0] == ["标题", "金额", "备注", "系统列"]  # 字段清单顺序 + 清单外列补齐
    assert len(table) == 5  # 表头 + 4 行
    assert table[1][2] == '含,逗号"引号"'  # CSV 转义往返无损
    audit = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.doc_read"]
    detail = json.loads(audit[0].detail)
    assert detail["format"] == "csv" and detail["total"] == 4 and detail["rows"] == 4


async def test_bitable_csv_truncation_note(_fake_feishu, _no_sleep, monkeypatch):
    monkeypatch.setattr(tools, "_BITABLE_CSV_MAX", 2)  # 缩小上限验证截断提示
    pages = [[_rec("r1"), _rec("r2")], [_rec("r3")]]
    _FakeFeishuClient.routes["/records"] = _paged_records(pages, total=3)
    _FakeFeishuClient.routes["/fields"] = {"code": 0, "data": {"items": []}}
    _FakeFeishuClient.routes["/tables"] = _BASE_TABLES
    out = await dispatch_tool(_ctx(), "feishu_doc_read",
                              {"url": "https://xxx.feishu.cn/base/BaseTok1234567890abc",
                               "format": "csv"})
    assert "已达单次 2 行上限只导出前 2 行" in out and "分表处理" in out


# ---------- _read_sheet csv 导出 ----------

def _sheet_meta_route():
    _FakeFeishuClient.routes["/sheets/query"] = {"code": 0, "data": {"sheets": [
        {"sheet_id": "s1", "title": "销售明细", "grid_properties": {"row_count": 600, "column_count": 5}},
    ]}}


async def test_sheet_csv_export_chunk_chain(_fake_feishu, _no_sleep, tmp_path):
    _sheet_meta_route()
    seen_ranges = []
    state = {"n": 0}

    def _values(params):
        url = _FakeFeishuClient.calls[-1][0]
        rng = unquote(url.split("/values/")[-1])  # range 里的冒号被 quote 成 %3A
        seen_ranges.append(rng)
        state["n"] += 1
        if state["n"] == 1:
            return {"code": 0, "data": {"valueRange": {
                "values": [[f"菜品{i}", i, i * 0.5] for i in range(500)]}}}
        return {"code": 0, "data": {"valueRange": {"values": []}}}  # 空块收尾

    _FakeFeishuClient.routes["/values/"] = _values
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_doc_read",
                              {"url": "https://xxx.feishu.cn/sheets/SheetTok1234567890ab",
                               "format": "csv"})
    assert "已导出到工作区：feishu_files/" in out and "run_code" in out
    assert "共 500 行全量" in out
    assert seen_ranges == ["s1!A1:AD500", "s1!A501:AD1000"]
    assert len(_no_sleep) == 1
    rows = [x for x in ctx.db.added if getattr(x, "filename", "").startswith("feishu_files/")]
    assert len(rows) == 1
    data = Path(rows[0].storage_path).read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")
    table = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    assert len(table) == 500 and table[0] == ["菜品0", "0", "0.0"]
    audit = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.doc_read"]
    detail = json.loads(audit[0].detail)
    assert detail["format"] == "csv" and detail["rows"] == 500


async def test_sheet_csv_short_block_stops(_fake_feishu, _no_sleep):
    _sheet_meta_route()
    _FakeFeishuClient.routes["/values/"] = {"code": 0, "data": {"valueRange": {
        "values": [["a", 1], ["b", 2]], }}}  # 短块（2 < 500）→ 读完即停
    out = await dispatch_tool(_ctx(), "feishu_doc_read",
                              {"url": "https://xxx.feishu.cn/sheets/SheetTok1234567890ab",
                               "format": "csv"})
    assert "共 2 行全量" in out
    assert not _no_sleep  # 单块读完不节流


# ---------- 内嵌多维表格回归（M10c 元组解包不炸） ----------

async def test_embedded_bitable_tuple_unpack(_fake_feishu):
    _FakeFeishuClient.routes["/blocks"] = {"code": 0, "data": {"items": [
        {"block_id": "h1", "heading1": {"elements": [{"text_run": {"content": "合同区"}}]}},
        {"block_id": "b1", "bitable": {"token": "appE_tblE"}},
    ]}}
    _FakeFeishuClient.routes["/records"] = {"code": 0, "data": {
        "total": 2, "has_more": False,
        "items": [_rec("rec1", 标题="合同X"), _rec("rec2", 标题="合同Y")],
    }}
    sections, ok = await tools._embedded_sections(_ctx(), _FakeFeishuClient(), "tok", "doc1")
    assert ok == {"sheet": 0, "bitable": 1}
    assert "合同X" in sections[0] and "位于「合同区」" in sections[0]
