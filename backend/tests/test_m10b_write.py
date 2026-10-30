"""M10b 写入生态测试：docx 文末追加 / sheet 写范围+追加行 / bitable 增改记录。

契约来自飞书 open-apis 文档（2026-09-02）：
- docx 追加：POST /docx/v1/documents/{id}/blocks/{root}/children，body children[text 块]
- sheet 写值：PUT /sheets/v2/spreadsheets/{t}/values，body valueRange{range,values}
- sheet 追加：POST .../values_append，body 同上
- bitable 新增：POST /bitable/v1/apps/{t}/tables/{tid}/records/batch_create
- bitable 更新：PUT .../records/{record_id}，body {fields}
- 写 scope 守卫：docx:document / sheets:spreadsheet / bitable:app（M10a 已随重授权发全）
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


_WRITE_SCOPES = ("docx:document:readonly docx:document wiki:wiki:readonly drive:drive:readonly "
                 "sheets:spreadsheet:readonly sheets:spreadsheet "
                 "bitable:app:readonly bitable:app")


def _ctx(scopes=_WRITE_SCOPES):
    return ToolContext(
        db=_FakeDB(),
        user=SimpleNamespace(id=1, username="u1", feishu_scopes=scopes),
        agent=SimpleNamespace(id=1),
        allow_feishu=True,
    )


# ---------- 假飞书 API（URL 前缀路由；记录请求体供断言） ----------

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

    async def put(self, url, headers=None, json=None, params=None):
        return self._match(url, json)

    async def request(self, method, url, headers=None, json=None, params=None):
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


def _sheet_meta_route():
    _FakeFeishuClient.routes["/sheets/query"] = {"code": 0, "data": {"sheets": [
        {"sheet_id": "s1", "title": "销售明细", "grid_properties": {"row_count": 500, "column_count": 40}},
    ]}}


def _base_tables_route():
    _FakeFeishuClient.routes["/bitable/v1/apps"] = {"code": 0, "data": {"items": [
        {"table_id": "tbl1", "name": "合同台账"}, {"table_id": "tbl2", "name": "归档"},
    ]}}


# ---------- docx 追加 ----------

@pytest.mark.asyncio
async def test_doc_append_body_and_audit(_fake_feishu):
    _FakeFeishuClient.routes["/blocks/"] = {"code": 0, "data": {}}
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_doc_append",
                              {"url": "https://x.feishu.cn/docx/DocxTok1234567890abcd",
                               "text": "第一段\n\n第二段"})
    assert "追加 2 个内容块" in out
    calls = [c for c in _FakeFeishuClient.calls if "/blocks/" in c[0]]
    assert len(calls) == 1
    url, body = calls[0]
    # root block_id = doc_id，追加到文末
    assert "/docx/v1/documents/DocxTok1234567890abcd/blocks/DocxTok1234567890abcd/children" in url
    assert body["children"] == [
        {"block_type": 2, "text": {"elements": [{"text_run": {"content": "第一段"}}]}},
        {"block_type": 2, "text": {"elements": [{"text_run": {"content": "第二段"}}]}},
    ]
    audit = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.doc_append"]
    assert len(audit) == 1
    detail = json.loads(audit[0].detail)
    assert detail["paragraphs"] == 2 and detail["chars"] == 6
    assert "第一段" not in audit[0].detail  # 审计不落写入内容


@pytest.mark.asyncio
async def test_doc_append_no_write_scope_guides():
    ctx = _ctx(scopes="docx:document:readonly")  # 只有读 scope
    out = await dispatch_tool(ctx, "feishu_doc_append",
                              {"url": "https://x.feishu.cn/docx/DocxTok1234567890abcd", "text": "x"})
    assert "/login?upgrade=1" in out


@pytest.mark.asyncio
async def test_doc_append_wiki_resolves(_fake_feishu):
    _FakeFeishuClient.routes["/wiki/v2/spaces/get_node"] = {
        "code": 0, "data": {"node": {"obj_token": "RealDoc1234567890ab",
                                      "obj_type": "docx", "title": "小龙虾入职指南"}}}
    _FakeFeishuClient.routes["/blocks/"] = {"code": 0, "data": {}}
    out = await dispatch_tool(_ctx(), "feishu_doc_append",
                              {"url": "https://x.feishu.cn/wiki/WikiTok1234567890abcd", "text": "一段"})
    assert "《小龙虾入职指南》" in out
    assert any("RealDoc1234567890ab/blocks" in u for u, _ in _FakeFeishuClient.calls)


@pytest.mark.asyncio
async def test_doc_append_rejects_sheet_url():
    out = await dispatch_tool(_ctx(), "feishu_doc_append",
                              {"url": "https://x.feishu.cn/sheets/SheetTok1234567890", "text": "x"})
    assert "feishu_sheet_write" in out


@pytest.mark.asyncio
async def test_doc_append_empty_text():
    out = await dispatch_tool(_ctx(), "feishu_doc_append",
                              {"url": "https://x.feishu.cn/docx/DocxTok1234567890abcd", "text": "  "})
    assert "text 为空" in out


# ---------- sheet 写入 ----------

@pytest.mark.asyncio
async def test_sheet_update_prefixes_first_sheet(_fake_feishu):
    _sheet_meta_route()
    _FakeFeishuClient.routes["/values"] = {"code": 0, "data": {"updatedRange": "s1!A1:B2"}}
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_sheet_write",
                              {"url": "https://x.feishu.cn/sheets/SheetTok1234567890",
                               "range": "A1:B2", "values": [["姓名", "数量"], ["小龙虾", 3]]})
    assert "写入范围 2 行" in out and "s1!A1:B2" in out  # 无 sheetId 前缀 → 补首页
    calls = [c for c in _FakeFeishuClient.calls if "/values" in c[0]]
    assert calls[0][1] == {"valueRange": {"range": "s1!A1:B2",
                                          "values": [["姓名", "数量"], ["小龙虾", 3]]}}
    audit = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.sheet_write"]
    assert audit and '"mode": "update"' in audit[0].detail and '"rows": 2' in audit[0].detail
    assert "小龙虾" not in audit[0].detail  # 审计不落内容


@pytest.mark.asyncio
async def test_sheet_append_default_range(_fake_feishu):
    _sheet_meta_route()
    _FakeFeishuClient.routes["/values_append"] = {"code": 0, "data": {}}
    out = await dispatch_tool(_ctx(), "feishu_sheet_write",
                              {"url": "https://x.feishu.cn/sheets/SheetTok1234567890",
                               "mode": "append", "values": [["鱼香肉丝", 98]]})
    assert "末行追加 1 行" in out
    calls = [c for c in _FakeFeishuClient.calls if "/values_append" in c[0]]
    assert calls[0][1]["valueRange"]["range"] == "s1!A1"  # append 默认首页 A1


@pytest.mark.asyncio
async def test_sheet_update_requires_range():
    out = await dispatch_tool(_ctx(), "feishu_sheet_write",
                              {"url": "https://x.feishu.cn/sheets/SheetTok1234567890",
                               "values": [["a"]]})
    assert "必须传 range" in out


@pytest.mark.asyncio
async def test_sheet_write_bad_values():
    out = await dispatch_tool(_ctx(), "feishu_sheet_write",
                              {"url": "https://x.feishu.cn/sheets/SheetTok1234567890",
                               "range": "A1", "values": "不是数组"})
    assert "二维数组" in out


@pytest.mark.asyncio
async def test_sheet_write_no_scope_guides():
    ctx = _ctx(scopes="sheets:spreadsheet:readonly")  # 只有读 scope
    out = await dispatch_tool(ctx, "feishu_sheet_write",
                              {"url": "https://x.feishu.cn/sheets/SheetTok1234567890",
                               "range": "A1", "values": [["a"]]})
    assert "/login?upgrade=1" in out


# ---------- bitable 写入 ----------

@pytest.mark.asyncio
async def test_bitable_add_records(_fake_feishu):
    # 更具体的前缀先注册（_match 按注册顺序前缀匹配）
    _FakeFeishuClient.routes["/records/batch_create"] = {"code": 0, "data": {
        "records": [{"record_id": "recNew1"}, {"record_id": "recNew2"}]}}
    _base_tables_route()
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_bitable_write",
                              {"url": "https://x.feishu.cn/base/BaseTok1234567890abc",
                               "table": "归档",
                               "records": [{"标题": "合同C", "金额": 800}, {"标题": "合同D"}]})
    assert "「归档」表新增 2 条记录" in out
    calls = [c for c in _FakeFeishuClient.calls if "/batch_create" in c[0]]
    assert "tbl2/records/batch_create" in calls[0][0]  # 表名解析到 tbl2
    assert calls[0][1] == {"records": [{"fields": {"标题": "合同C", "金额": 800}},
                                       {"fields": {"标题": "合同D"}}]}
    audit = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.bitable_write"]
    assert audit and '"mode": "add"' in audit[0].detail and "合同C" not in audit[0].detail


@pytest.mark.asyncio
async def test_bitable_update_needs_record_id(_fake_feishu):
    _base_tables_route()
    out = await dispatch_tool(_ctx(), "feishu_bitable_write",
                              {"url": "https://x.feishu.cn/base/BaseTok1234567890abc",
                               "mode": "update", "records": [{"标题": "无id"}]})
    assert "record_id" in out and "feishu_doc_read" in out


@pytest.mark.asyncio
async def test_bitable_update_calls_put(_fake_feishu):
    _FakeFeishuClient.routes["/records/rec1"] = {"code": 0, "data": {}}
    _base_tables_route()
    out = await dispatch_tool(_ctx(), "feishu_bitable_write",
                              {"url": "https://x.feishu.cn/base/BaseTok1234567890abc",
                               "mode": "update",
                               "records": [{"record_id": "rec1", "金额": 123}]})
    assert "更新 1 条记录" in out
    calls = [c for c in _FakeFeishuClient.calls if "/records/rec1" in c[0]]
    assert calls[0][1] == {"fields": {"金额": 123}}  # record_id 不进 fields


@pytest.mark.asyncio
async def test_bitable_write_no_scope_guides():
    ctx = _ctx(scopes="bitable:app:readonly")  # 只有读 scope
    out = await dispatch_tool(ctx, "feishu_bitable_write",
                              {"url": "https://x.feishu.cn/base/BaseTok1234567890abc",
                               "records": [{"标题": "x"}]})
    assert "/login?upgrade=1" in out


# ---------- docx 末尾删除 ----------

def _text_block(s):
    return {"block_id": f"blk_{s}", "block_type": 2,
            "text": {"elements": [{"text_run": {"content": s}}]}}


def _doc_blocks_route(texts):
    # 更具体的前缀先注册（batch_delete 的 URL 也含 /children）
    _FakeFeishuClient.routes["/children/batch_delete"] = {"code": 0, "data": {}}
    _FakeFeishuClient.routes["/children"] = {"code": 0, "data": {
        "items": [_text_block(t) for t in texts], "has_more": False}}


@pytest.mark.asyncio
async def test_doc_delete_tail_range_and_preview(_fake_feishu):
    _doc_blocks_route(["开头段", "第二段", "第三段", "要删的总结A", "要删的总结B"])
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_doc_delete_tail",
                              {"url": "https://x.feishu.cn/docx/DocxTok1234567890abcd", "count": 2})
    assert "已删除" in out and "文末 2 段" in out
    assert "要删的总结A" in out  # 删除预览（只回给模型/用户，不进审计）
    calls = [c for c in _FakeFeishuClient.calls if "batch_delete" in c[0]]
    assert len(calls) == 1
    assert calls[0][1] == {"start_index": 3, "end_index": 5}  # 删末尾 2 块 [3,5)
    audit = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.doc_delete"]
    assert len(audit) == 1 and '"count": 2' in audit[0].detail
    assert "要删的总结" not in audit[0].detail  # 审计不落内容


@pytest.mark.asyncio
async def test_doc_delete_tail_count_exceeds_total(_fake_feishu):
    _doc_blocks_route(["只有一段"])
    out = await dispatch_tool(_ctx(), "feishu_doc_delete_tail",
                              {"url": "https://x.feishu.cn/docx/DocxTok1234567890abcd", "count": 5})
    assert "只有 1 个内容块" in out
    assert not any("batch_delete" in u for u, _ in _FakeFeishuClient.calls)  # 没执行删除


@pytest.mark.asyncio
async def test_doc_delete_tail_empty_doc(_fake_feishu):
    _doc_blocks_route([])
    out = await dispatch_tool(_ctx(), "feishu_doc_delete_tail",
                              {"url": "https://x.feishu.cn/docx/DocxTok1234567890abcd"})
    assert "无需删除" in out


@pytest.mark.asyncio
async def test_doc_delete_tail_no_scope_guides():
    ctx = _ctx(scopes="docx:document:readonly")  # 只有读 scope
    out = await dispatch_tool(ctx, "feishu_doc_delete_tail",
                              {"url": "https://x.feishu.cn/docx/DocxTok1234567890abcd"})
    assert "/login?upgrade=1" in out


# ---------- docx 新建 ----------

@pytest.mark.asyncio
async def test_doc_create_in_folder_with_text(_fake_feishu):
    # 更具体的前缀先注册（blocks URL 也含 /docx/v1/documents）
    _FakeFeishuClient.routes["/blocks/"] = {"code": 0, "data": {}}
    _FakeFeishuClient.routes["/docx/v1/documents"] = {"code": 0, "data": {
        "document": {"document_id": "NewDoc1234567890abcd", "title": "改进版指南"}}}
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_doc_create",
                              {"title": "改进版指南",
                               "folder": "https://x.feishu.cn/drive/folder/FoldTok1234567890ab",
                               "text": "第一章\n第二章"})
    assert "《改进版指南》" in out and "https://feishu.cn/docx/NewDoc1234567890abcd" in out
    assert "已写入 2 个内容块" in out
    create_calls = [c for c in _FakeFeishuClient.calls if "/blocks/" not in c[0]]
    assert create_calls[0][1] == {"title": "改进版指南", "folder_token": "FoldTok1234567890ab"}
    block_calls = [c for c in _FakeFeishuClient.calls if "/blocks/" in c[0]]
    assert "NewDoc1234567890abcd/blocks/NewDoc1234567890abcd/children" in block_calls[0][0]
    audit = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.doc_create"]
    assert len(audit) == 1 and '"paragraphs": 2' in audit[0].detail
    assert "第一章" not in audit[0].detail  # 审计不落内容


@pytest.mark.asyncio
async def test_doc_create_root_no_text(_fake_feishu):
    _FakeFeishuClient.routes["/docx/v1/documents"] = {"code": 0, "data": {
        "document": {"document_id": "NewDoc1234567890abcd", "title": "空文档"}}}
    out = await dispatch_tool(_ctx(), "feishu_doc_create", {"title": "空文档"})
    assert "我的空间根目录" in out and "已写入" not in out
    assert _FakeFeishuClient.calls[0][1] == {"title": "空文档"}  # 无 folder 不传 folder_token


@pytest.mark.asyncio
async def test_doc_create_bad_folder():
    out = await dispatch_tool(_ctx(), "feishu_doc_create",
                              {"title": "x", "folder": "https://example.com/not-a-folder"})
    assert "无法识别的文件夹链接" in out


@pytest.mark.asyncio
async def test_doc_create_no_scope_guides():
    ctx = _ctx(scopes="docx:document:readonly")  # 只有读 scope
    out = await dispatch_tool(ctx, "feishu_doc_create", {"title": "x"})
    assert "/login?upgrade=1" in out


# ---------- M10b.4：markdown → 飞书原生块 ----------

@pytest.mark.asyncio
async def test_doc_append_markdown_blocks(_fake_feishu):
    """混合 markdown 转原生块：标题/列表/引用/粗体/分割线/代码块/待办。"""
    _FakeFeishuClient.routes["/blocks/"] = {"code": 0, "data": {}}
    md = ("# 一级标题\n## 二级标题\n- 列表项\n1. 第一项\n> 引用一句\n"
          "普通**加粗**混合\n---\n```\nprint(1)\n```\n- [x] 已完成待办")
    await dispatch_tool(_ctx(), "feishu_doc_append",
                        {"url": "https://x.feishu.cn/docx/DocxTok1234567890abcd", "text": md})
    calls = [c for c in _FakeFeishuClient.calls if "/blocks/" in c[0]]
    children = calls[0][1]["children"]
    types = [b["block_type"] for b in children]
    assert types == [3, 4, 12, 13, 15, 2, 22, 14, 17]  # h1 h2 bullet ordered quote text divider code todo
    assert children[0]["heading1"]["elements"][0]["text_run"]["content"] == "一级标题"
    assert children[2]["bullet"]["elements"][0]["text_run"]["content"] == "列表项"
    # 粗体行内样式
    text_els = children[5]["text"]["elements"]
    assert text_els[0]["text_run"]["content"] == "普通"
    assert text_els[1]["text_run"]["content"] == "加粗"
    assert text_els[1]["text_run"]["text_element_style"] == {"bold": True}
    assert text_els[2]["text_run"]["content"] == "混合"
    # code 块不解析行内 markdown
    assert children[7]["code"]["elements"][0]["text_run"]["content"] == "print(1)"
    assert children[8]["todo"]["style"] == {"done": True}


@pytest.mark.asyncio
async def test_doc_append_unclosed_code_fence(_fake_feishu):
    """未闭合的 ``` 围栏兜底成 code 块（不丢内容）。"""
    _FakeFeishuClient.routes["/blocks/"] = {"code": 0, "data": {}}
    await dispatch_tool(_ctx(), "feishu_doc_append",
                        {"url": "https://x.feishu.cn/docx/DocxTok1234567890abcd",
                         "text": "正文一段\n```python\n没闭合的代码"})
    calls = [c for c in _FakeFeishuClient.calls if "/blocks/" in c[0]]
    children = calls[0][1]["children"]
    assert [b["block_type"] for b in children] == [2, 14]
    assert children[1]["code"]["elements"][0]["text_run"]["content"] == "没闭合的代码"


@pytest.mark.asyncio
async def test_doc_append_empty_blocks_guard(_fake_feishu):
    """M10b.5：转换后 0 块（如裸 ```）直接提示，不把空 children 打给飞书。"""
    _FakeFeishuClient.routes["/blocks/"] = {"code": 0, "data": {}}
    out = await dispatch_tool(_ctx(), "feishu_doc_append",
                              {"url": "https://x.feishu.cn/docx/DocxTok1234567890abcd",
                               "text": "```"})
    assert "内容为空" in out
    assert not any("/blocks/" in u for u, _ in _FakeFeishuClient.calls)


@pytest.mark.asyncio
async def test_doc_create_empty_body_still_returns_link(_fake_feishu):
    """M10b.5：建文档时正文转换为空块——文档已建出就回链接，并注明正文未写入。"""
    _FakeFeishuClient.routes["/docx/v1/documents"] = {"code": 0, "data": {
        "document": {"document_id": "NewDoc1234567890abcd", "title": "x"}}}
    out = await dispatch_tool(_ctx(), "feishu_doc_create", {"title": "x", "text": "```"})
    assert "https://feishu.cn/docx/NewDoc1234567890abcd" in out and "正文为空" in out
    assert not any("/blocks/" in u for u, _ in _FakeFeishuClient.calls)


# ---------- M10b.6：sheet / bitable 从零新建 ----------

@pytest.mark.asyncio
async def test_sheet_create_in_folder(_fake_feishu):
    _FakeFeishuClient.routes["/sheets/v3/spreadsheets"] = {"code": 0, "data": {
        "spreadsheet": {"spreadsheet_token": "NewSs1234567890abcdef",
                        "title": "门店台账", "url": "https://x.feishu.cn/sheets/NewSs1234567890abcdef"}}}
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_sheet_create",
                              {"title": "门店台账",
                               "folder": "https://x.feishu.cn/drive/folder/FoldTok1234567890ab"})
    assert "《门店台账》" in out and "https://x.feishu.cn/sheets/NewSs1234567890abcdef" in out
    assert "feishu_sheet_write" in out
    assert _FakeFeishuClient.calls[0][1] == {"title": "门店台账", "folder_token": "FoldTok1234567890ab"}
    audits = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.sheet_create"]
    assert len(audits) == 1 and "NewSs1234567890abcdef" in audits[0].detail


@pytest.mark.asyncio
async def test_sheet_create_root_fallback_url(_fake_feishu):
    _FakeFeishuClient.routes["/sheets/v3/spreadsheets"] = {"code": 0, "data": {
        "spreadsheet": {"spreadsheet_token": "NewSs1234567890abcdef", "title": "x"}}}  # 无 url 字段
    out = await dispatch_tool(_ctx(), "feishu_sheet_create", {"title": "x"})
    assert "我的空间根目录" in out
    assert "https://feishu.cn/sheets/NewSs1234567890abcdef" in out  # 兜底拼链接
    assert _FakeFeishuClient.calls[0][1] == {"title": "x"}  # 不传 folder_token


@pytest.mark.asyncio
async def test_sheet_create_no_scope_guides():
    ctx = _ctx(scopes="sheets:spreadsheet:readonly")  # 只读 scope 不满足写守卫
    out = await dispatch_tool(ctx, "feishu_sheet_create", {"title": "x"})
    assert "/login?upgrade=1" in out


@pytest.mark.asyncio
async def test_bitable_create_in_folder(_fake_feishu):
    _FakeFeishuClient.routes["/bitable/v1/apps"] = {"code": 0, "data": {
        "app": {"app_token": "NewBase1234567890abcd", "name": "合同台账",
                "url": "https://x.feishu.cn/base/NewBase1234567890abcd",
                "default_table_id": "tblDefault1"}}}
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_bitable_create",
                              {"title": "合同台账",
                               "folder": "FoldTok1234567890abc"})  # 裸 token 也认
    assert "《合同台账》" in out and "https://x.feishu.cn/base/NewBase1234567890abcd" in out
    assert "tblDefault1" in out and "feishu_bitable_write" in out
    assert _FakeFeishuClient.calls[0][1] == {"name": "合同台账", "folder_token": "FoldTok1234567890abc"}
    audits = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.bitable_create"]
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_bitable_create_bad_folder():
    out = await dispatch_tool(_ctx(), "feishu_bitable_create",
                              {"title": "x", "folder": "https://example.com/nope"})
    assert "无法识别的文件夹链接" in out


@pytest.mark.asyncio
async def test_bitable_create_no_scope_guides():
    ctx = _ctx(scopes="bitable:app:readonly")
    out = await dispatch_tool(ctx, "feishu_bitable_create", {"title": "x"})
    assert "/login?upgrade=1" in out


# ---------- M10b.7：bitable 字段（列）查看/新建 ----------

_BASE_URL = "https://x.feishu.cn/base/BaseTok1234567890abcde"


def _field_routes():
    # 更具体的前缀先注册（fields URL 也含 /tables 和 /bitable/v1/apps）
    _FakeFeishuClient.routes["/fields"] = {"code": 0, "data": {"items": [
        {"field_id": "fld1", "field_name": "标题", "type": 1},
        {"field_id": "fld2", "field_name": "状态", "type": 3,
         "property": {"options": [{"name": "进行中"}, {"name": "已完成"}]}},
        {"field_id": "fld3", "field_name": "自定义类型", "type": 99},
    ]}}
    _FakeFeishuClient.routes["/tables"] = {"code": 0, "data": {"items": [
        {"table_id": "tbl1", "name": "台账"}]}}


@pytest.mark.asyncio
async def test_bitable_field_list(_fake_feishu):
    _field_routes()
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_bitable_field", {"url": _BASE_URL, "action": "list"})
    assert "「台账」表现有 3 列" in out
    assert "标题（文本）" in out
    assert "状态（单选）：选项 [进行中、已完成]" in out
    assert "类型99" in out  # 未收录类型显示编号
    audits = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.bitable_field"]
    assert len(audits) == 1 and '"action": "list"' in audits[0].detail
    assert "标题" not in audits[0].detail  # 审计不落字段名


@pytest.mark.asyncio
async def test_bitable_field_add_batch(_fake_feishu):
    _FakeFeishuClient.routes["/fields"] = {"code": 0, "data": {"field": {"field_id": "fldNew"}}}
    _FakeFeishuClient.routes["/tables"] = {"code": 0, "data": {"items": [
        {"table_id": "tbl1", "name": "台账"}]}}
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_bitable_field", {
        "url": _BASE_URL, "action": "add",
        "fields": [{"name": "金额", "type": "number"},
                   {"name": "状态", "type": "单选", "options": ["进行中", "已完成"]},
                   {"name": "截止日期", "type": "date"}]})
    assert "新建 3 列：金额、状态、截止日期" in out and "feishu_bitable_write" in out
    posts = [c for c in _FakeFeishuClient.calls if "/fields" in c[0]]
    assert [p[1]["type"] for p in posts] == [2, 3, 5]
    assert posts[0][1] == {"field_name": "金额", "type": 2}  # 无 property 不带该键
    assert posts[1][1]["property"]["options"] == [{"name": "进行中"}, {"name": "已完成"}]
    assert posts[2][1]["property"]["date_formatter"] == "yyyy/MM/dd"
    audits = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.bitable_field"]
    assert '"action": "add"' in audits[0].detail and '"count": 3' in audits[0].detail


@pytest.mark.asyncio
async def test_bitable_field_add_select_needs_options(_fake_feishu):
    _field_routes()
    out = await dispatch_tool(_ctx(), "feishu_bitable_field", {
        "url": _BASE_URL, "action": "add", "fields": [{"name": "状态", "type": "multi_select"}]})
    assert "必须带 options" in out
    assert not any("/fields" in u for u, _ in _FakeFeishuClient.calls)


@pytest.mark.asyncio
async def test_bitable_field_add_bad_type():
    out = await dispatch_tool(_ctx(), "feishu_bitable_field", {
        "url": _BASE_URL, "action": "add", "fields": [{"name": "x", "type": "formula"}]})
    assert "字段定义无效" in out


@pytest.mark.asyncio
async def test_bitable_field_no_scope_guides():
    ctx = _ctx(scopes="bitable:app:readonly")
    out = await dispatch_tool(ctx, "feishu_bitable_field", {"url": _BASE_URL, "action": "list"})
    assert "/login?upgrade=1" in out


# ---------- M10b.8：bitable 删列 / 删记录 ----------

@pytest.mark.asyncio
async def test_bitable_field_delete_by_name(_fake_feishu):
    _field_routes()  # GET /fields 返回 fld1 标题 / fld2 状态 / fld3 自定义类型
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_bitable_field", {
        "url": _BASE_URL, "action": "delete", "fields": ["状态", "fld3"]})  # 列名 + field_id 混用
    assert "删除 2 列：状态、fld3" in out
    dels = [u for u, _ in _FakeFeishuClient.calls if "/fields/fld" in u]
    assert len(dels) == 2 and "/fields/fld2" in dels[0] and "/fields/fld3" in dels[1]
    audits = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.bitable_field"]
    assert '"action": "delete"' in audits[0].detail and '"count": 2' in audits[0].detail
    assert "状态" not in audits[0].detail  # 审计不落列名


@pytest.mark.asyncio
async def test_bitable_field_delete_unknown_name(_fake_feishu):
    _field_routes()
    out = await dispatch_tool(_ctx(), "feishu_bitable_field", {
        "url": _BASE_URL, "action": "delete", "fields": ["不存在"]})
    assert "找不到这些列" in out and "未删除任何列" in out
    assert not any("/fields/fld" in u for u, _ in _FakeFeishuClient.calls)  # 一列都没删


@pytest.mark.asyncio
async def test_bitable_field_delete_bad_args():
    out = await dispatch_tool(_ctx(), "feishu_bitable_field", {
        "url": _BASE_URL, "action": "delete", "fields": [{"name": "x"}]})  # 要字符串数组
    assert "列名" in out


@pytest.mark.asyncio
async def test_bitable_delete_records(_fake_feishu):
    # 更具体的前缀先注册（batch_delete URL 也含 /tables）
    _FakeFeishuClient.routes["/records/batch_delete"] = {"code": 0, "data": {
        "records": [{"record_id": "rec1"}, {"record_id": "rec2"}]}}
    _FakeFeishuClient.routes["/tables"] = {"code": 0, "data": {"items": [
        {"table_id": "tbl1", "name": "台账"}]}}
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_bitable_write", {
        "url": _BASE_URL, "mode": "delete",
        "records": [{"record_id": "rec1"}, {"record_id": "rec2"}]})
    assert "「台账」表删除 2 条记录" in out
    calls = [c for c in _FakeFeishuClient.calls if "/records/batch_delete" in c[0]]
    assert calls[0][1] == {"records": ["rec1", "rec2"]}
    audits = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.bitable_write"]
    assert '"mode": "delete"' in audits[0].detail and "rec1" not in audits[0].detail


@pytest.mark.asyncio
async def test_bitable_delete_records_needs_record_id(_fake_feishu):
    _FakeFeishuClient.routes["/tables"] = {"code": 0, "data": {"items": [
        {"table_id": "tbl1", "name": "台账"}]}}
    out = await dispatch_tool(_ctx(), "feishu_bitable_write", {
        "url": _BASE_URL, "mode": "delete", "records": [{"标题": "x"}]})
    assert "record_id" in out
    assert not any("/records/batch_delete" in u for u, _ in _FakeFeishuClient.calls)


# ---------- M10b.9：bitable 改列（rename/retype，主列改造用） ----------

@pytest.mark.asyncio
async def test_bitable_field_update_rename_and_retype(_fake_feishu):
    _field_routes()  # fld1 标题(文本) / fld2 状态(单选) / fld3 自定义类型(99)
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_bitable_field", {
        "url": _BASE_URL, "action": "update",
        "fields": [{"name": "标题", "new_name": "门店名称"},          # 主列改名
                   {"name": "fld3", "type": "多选", "options": ["A", "B"]}]})  # field_id 定位 + 改型
    assert "修改 2 列：门店名称、fld3" in out
    puts = [c for c in _FakeFeishuClient.calls if "/fields/fld" in c[0]]
    assert puts[0][1] == {"field_name": "门店名称"}  # 只改名不带 type
    assert puts[1][0].endswith("/fields/fld3")
    assert puts[1][1]["type"] == 4 and puts[1][1]["property"]["options"] == [{"name": "A"}, {"name": "B"}]
    audits = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.bitable_field"]
    assert '"action": "update"' in audits[0].detail and '"count": 2' in audits[0].detail


@pytest.mark.asyncio
async def test_bitable_field_update_nothing_to_change():
    out = await dispatch_tool(_ctx(), "feishu_bitable_field", {
        "url": _BASE_URL, "action": "update", "fields": [{"name": "标题"}]})
    assert "至少给一个" in out


@pytest.mark.asyncio
async def test_bitable_field_update_unknown_column(_fake_feishu):
    _field_routes()
    out = await dispatch_tool(_ctx(), "feishu_bitable_field", {
        "url": _BASE_URL, "action": "update", "fields": [{"name": "不存在", "new_name": "x"}]})
    assert "找不到这些列" in out and "未改动任何列" in out
    assert not any("/fields/fld" in u for u, _ in _FakeFeishuClient.calls)


@pytest.mark.asyncio
async def test_bitable_field_update_select_needs_options():
    out = await dispatch_tool(_ctx(), "feishu_bitable_field", {
        "url": _BASE_URL, "action": "update", "fields": [{"name": "状态", "type": "单选"}]})
    assert "必须带 options" in out


# ---------- M10c：docx 段落级编辑（feishu_doc_edit） ----------

_EDIT_DOC_URL = "https://x.feishu.cn/docx/DocxTok1234567890abcd"
_EDIT_BLOCKS = [
    {"block_type": 4, "heading2": {"elements": [{"text_run": {"content": "第一章 总则"}}]}},
    {"block_type": 2, "text": {"elements": [{"text_run": {"content": "这是第一段正文"}}]}},
    {"block_type": 12, "bullet": {"elements": [{"text_run": {"content": "列表项甲"}}]}},
    {"block_type": 22},  # 分割线，无文本
]


def _edit_routes(children_payload=None):
    """注册 doc_edit 路由：/children/batch_delete 必须先于 /children（URL 前缀路由）。
    GET children（extra=params 无 children 键）→ 块清单；POST children（extra=body 有 children 键）→ 插入。"""
    _FakeFeishuClient.routes["/children/batch_delete"] = {"code": 0, "data": {}}

    def _children(extra):
        if isinstance(extra, dict) and "children" in extra:
            return children_payload(extra) if children_payload else {"code": 0, "data": {}}
        return {"code": 0, "data": {"items": _EDIT_BLOCKS, "has_more": False}}

    _FakeFeishuClient.routes["/children"] = _children


@pytest.mark.asyncio
async def test_doc_edit_list(_fake_feishu):
    _edit_routes()
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_doc_edit", {"url": _EDIT_DOC_URL, "action": "list"})
    assert "共 4 个内容块" in out
    assert "[0] 标题2 | 第一章 总则" in out
    assert "[1] 文本 | 这是第一段正文" in out
    assert "[2] 无序列表 | 列表项甲" in out
    assert "[3] 分割线" in out and "[3] 分割线 |" not in out  # 无文本块不带预览
    audits = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.doc_edit"]
    assert len(audits) == 1 and '"action": "list"' in audits[0].detail
    assert "第一章" not in audits[0].detail  # 审计不落内容


@pytest.mark.asyncio
async def test_doc_edit_replace(_fake_feishu):
    _edit_routes()
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_doc_edit", {
        "url": _EDIT_DOC_URL, "action": "replace", "index": 1,
        "text": "## 新标题\n- 要点一"})
    assert "已替换" in out and "第 1 块" in out and "这是第一段正文" in out  # 原文预览
    dels = [c for c in _FakeFeishuClient.calls if "/children/batch_delete" in c[0]]
    assert dels[0][1] == {"start_index": 1, "end_index": 2}
    posts = [c for c in _FakeFeishuClient.calls
             if "/children" in c[0] and "/batch_delete" not in c[0] and isinstance(c[1], dict) and "children" in c[1]]
    assert posts[0][1]["index"] == 1  # 插回原位
    assert [b["block_type"] for b in posts[0][1]["children"]] == [4, 12]  # markdown 转原生块
    audits = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.doc_edit"]
    assert '"action": "replace"' in audits[0].detail and '"blocks": 2' in audits[0].detail


@pytest.mark.asyncio
async def test_doc_edit_replace_insert_fails_restores(_fake_feishu):
    def _fail_new(extra):
        joined = str(extra["children"])
        if "新标题" in joined:
            return {"code": 999, "msg": "boom"}  # 新内容插入失败
        return {"code": 0, "data": {}}  # 恢复插入成功

    _edit_routes(children_payload=_fail_new)
    out = await dispatch_tool(_ctx(), "feishu_doc_edit", {
        "url": _EDIT_DOC_URL, "action": "replace", "index": 1, "text": "新标题"})
    assert "替换失败" in out and "已把原内容恢复原位" in out
    restores = [c for c in _FakeFeishuClient.calls
                if isinstance(c[1], dict) and "children" in c[1] and "这是第一段正文" in str(c[1]["children"])]
    assert len(restores) == 1 and restores[0][1]["index"] == 1  # 原位恢复


@pytest.mark.asyncio
async def test_doc_edit_insert_at_end(_fake_feishu):
    _edit_routes()
    out = await dispatch_tool(_ctx(), "feishu_doc_edit", {
        "url": _EDIT_DOC_URL, "action": "insert", "index": 4, "text": "末尾插一段"})  # index=总数=文末
    assert "第 4 块前插入 1 个内容块" in out
    assert not any("/batch_delete" in u for u, _ in _FakeFeishuClient.calls)  # insert 不删
    posts = [c for c in _FakeFeishuClient.calls if isinstance(c[1], dict) and "children" in c[1]]
    assert posts[0][1]["index"] == 4


@pytest.mark.asyncio
async def test_doc_edit_delete_mid(_fake_feishu):
    _edit_routes()
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_doc_edit", {
        "url": _EDIT_DOC_URL, "action": "delete", "index": 1, "count": 2})
    assert "已删除" in out and "第 1 块起 2 块" in out and "这是第一段正文" in out  # 删除预览
    dels = [c for c in _FakeFeishuClient.calls if "/children/batch_delete" in c[0]]
    assert dels[0][1] == {"start_index": 1, "end_index": 3}
    audits = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.doc_edit"]
    assert '"action": "delete"' in audits[0].detail and '"count": 2' in audits[0].detail


@pytest.mark.asyncio
async def test_doc_edit_index_out_of_range(_fake_feishu):
    _edit_routes()
    out = await dispatch_tool(_ctx(), "feishu_doc_edit", {
        "url": _EDIT_DOC_URL, "action": "replace", "index": 9, "text": "x"})
    assert "index 范围 0~3" in out
    out = await dispatch_tool(_ctx(), "feishu_doc_edit", {
        "url": _EDIT_DOC_URL, "action": "delete", "index": 3, "count": 5})
    assert "超出文档末尾" in out
    out = await dispatch_tool(_ctx(), "feishu_doc_edit", {
        "url": _EDIT_DOC_URL, "action": "insert", "index": 5, "text": "x"})
    assert "index 范围 0~4" in out
    assert not any("/batch_delete" in u for u, _ in _FakeFeishuClient.calls)


@pytest.mark.asyncio
async def test_doc_edit_empty_text_guard():
    out = await dispatch_tool(_ctx(), "feishu_doc_edit", {
        "url": _EDIT_DOC_URL, "action": "replace", "index": 0, "text": "```"})
    assert "内容为空" in out


@pytest.mark.asyncio
async def test_doc_edit_wiki_resolves(_fake_feishu):
    _FakeFeishuClient.routes["/wiki/v2/spaces/get_node"] = {"code": 0, "data": {
        "node": {"obj_token": "RealDoc1234567890ab", "obj_type": "docx", "title": "指南"}}}
    _edit_routes()
    out = await dispatch_tool(_ctx(), "feishu_doc_edit", {
        "url": "https://x.feishu.cn/wiki/WikiTok1234567890abcd", "action": "list"})
    assert "《指南》" in out and "共 4 个内容块" in out
    assert any("RealDoc1234567890ab/blocks" in u for u, _ in _FakeFeishuClient.calls)


@pytest.mark.asyncio
async def test_doc_edit_no_scope_guides():
    ctx = _ctx(scopes="docx:document:readonly")
    out = await dispatch_tool(ctx, "feishu_doc_edit", {"url": _EDIT_DOC_URL, "action": "list"})
    assert "/login?upgrade=1" in out
