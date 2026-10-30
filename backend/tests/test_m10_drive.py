"""M10a 逻辑测试：飞书云盘读取生态——folder 列表、sheet/bitable 读取、file 下载落工作区。

契约全部来自 API 文档 + 既有模式（2026-09-02；云上探针 check_feishu_drive.py 复核）：
- folder 列表：GET /drive/v1/files?folder_token= → files[{name,type,token}]，next_page_token 翻页
- sheet meta：GET /sheets/v3/spreadsheets/{t}/sheets/query → sheets[{sheet_id,title,grid_properties}]
- sheet values：GET /sheets/v2/spreadsheets/{t}/values/{range} → valueRange.values 二维数组
- bitable：GET /bitable/v1/apps/{t}/tables → items[{table_id,name}]；
  GET .../tables/{tid}/records → items[{record_id,fields}]
- file 下载：GET /drive/v1/files/{t}/download → 二进制（Content-Disposition 带文件名）
- scope 守卫：sheet 需 sheets:spreadsheet:readonly、base 需 bitable:app:readonly、
  file/folder 需 drive:drive:readonly（写 scope 互认），缺失返回授权引导
"""
from types import SimpleNamespace

import pytest

from app.runtime import tools
from app.runtime.tools import ToolContext, dispatch_tool, parse_feishu_url


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


def _ctx(allow=True, scopes=_ALL_SCOPES):
    return ToolContext(
        db=_FakeDB(),
        user=SimpleNamespace(id=1, username="u1", feishu_scopes=scopes),
        agent=SimpleNamespace(id=1),
        allow_feishu=allow,
    )


# ---------- 假飞书 API（URL 前缀路由；更具体的前缀先注册） ----------

class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _FakeBinResp:
    """文件下载的二进制响应（非 JSON）。"""

    def __init__(self, data: bytes, filename: str = "test.xlsx"):
        self.content = data
        self.headers = {
            "content-type": "application/octet-stream",
            "content-disposition": f"attachment; filename*=UTF-8''{filename}",
        }

    def raise_for_status(self):
        pass


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
                payload = payload(extra) if callable(payload) else payload
                return payload if isinstance(payload, _FakeBinResp) else _FakeResp(payload)
        return _FakeResp({"code": 999, "msg": "no route"})

    async def get(self, url, headers=None, params=None):
        return self._match(url, params)

    async def post(self, url, headers=None, json=None, params=None, data=None, files=None):
        # json 请求保持原样（既有用例的 callable payload 直接吃 json dict）；
        # multipart（M29 文件上传）把表单字段与文件打包传出去供断言
        return self._match(url, json if json is not None else {"data": data, "files": files})


@pytest.fixture
def _fake_feishu(monkeypatch, tmp_path):
    _FakeFeishuClient.routes = {}
    _FakeFeishuClient.calls = []
    monkeypatch.setattr(tools.httpx, "AsyncClient", _FakeFeishuClient)

    async def _token(db, user):
        return "user_tok"

    monkeypatch.setattr(tools, "get_feishu_user_token", _token)
    monkeypatch.setattr(tools, "_workspace_root", lambda uid, aid: tmp_path)
    yield
    _FakeFeishuClient.routes = {}
    _FakeFeishuClient.calls = {}


# ---------- 路由注册辅助 ----------

def _sheet_routes():
    _FakeFeishuClient.routes["/sheets/query"] = {"code": 0, "data": {"sheets": [
        {"sheet_id": "s1", "title": "销售明细", "grid_properties": {"row_count": 500, "column_count": 40}},
        {"sheet_id": "s2", "title": "汇总", "grid_properties": {"row_count": 10, "column_count": 5}},
    ]}}
    _FakeFeishuClient.routes["/values/"] = {"code": 0, "data": {"valueRange": {
        "range": "s1!A1:T200",
        "values": [["菜品", "销量", "单价"], ["宫保鸡丁", 120, 28.5], ["鱼香肉丝", 98, None]],
    }}}


def _base_routes():
    _FakeFeishuClient.routes["/records"] = {"code": 0, "data": {
        "total": 2, "has_more": False,  # M34：翻页拉取要这两个字段
        "items": [
        {"record_id": "rec1", "fields": {"标题": [{"text": "合同A", "type": "text"}],
                                          "金额": 5000, "负责人": [{"name": "王思远"}]}},
        {"record_id": "rec2", "fields": {"标题": [{"text": "合同B", "type": "text"}], "金额": 300}},
    ]}}
    _FakeFeishuClient.routes["/bitable/v1/apps"] = {"code": 0, "data": {"items": [
        {"table_id": "tbl1", "name": "合同台账"}, {"table_id": "tbl2", "name": "归档"},
    ]}}


def _folder_routes():
    _FakeFeishuClient.routes["/drive/v1/files"] = {"code": 0, "data": {
        "files": [
            {"name": "九月报表.xlsx", "type": "sheet", "token": "SheetTok1234567890ab"},
            {"name": "合同台账", "type": "bitable", "token": "BaseTok1234567890abc"},
            {"name": "子文件夹", "type": "folder", "token": "FoldTok1234567890abc"},
        ],
        "has_more": False, "next_page_token": "",
    }}


# ---------- parse_feishu_url：folder 形态 ----------

@pytest.mark.parametrize("raw,expected", [
    ("https://xxx.feishu.cn/drive/folder/FoldTok1234567890ab", ("folder", "FoldTok1234567890ab")),
    ("https://xxx.feishu.cn/folder/FoldTok1234567890ab", ("folder", "FoldTok1234567890ab")),
    ("https://xxx.larksuite.com/drive/folder/FoldTok1234567890ab", ("folder", "FoldTok1234567890ab")),
])
def test_parse_folder_urls(raw, expected):
    assert parse_feishu_url(raw) == expected


# ---------- folder_list ----------

@pytest.mark.asyncio
async def test_folder_list_renders_items(_fake_feishu):
    _folder_routes()
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_folder_list",
                              {"url": "https://xxx.feishu.cn/drive/folder/FoldTok1234567890ab"})
    assert "共 3 项" in out
    assert "[表格] 九月报表.xlsx" in out and "[多维表格] 合同台账" in out and "[文件夹] 子文件夹" in out
    assert "sheets/SheetTok" in out and "base/BaseTok" in out and "drive/folder/FoldTok" in out
    audit = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.folder_list"]
    assert len(audit) == 1 and '"items": 3' in audit[0].detail


@pytest.mark.asyncio
async def test_folder_list_empty(_fake_feishu):
    _FakeFeishuClient.routes["/drive/v1/files"] = {"code": 0, "data": {"files": [], "has_more": False}}
    out = await dispatch_tool(_ctx(), "feishu_folder_list",
                              {"url": "https://xxx.feishu.cn/folder/FoldTok1234567890ab"})
    assert "空的" in out


@pytest.mark.asyncio
async def test_folder_list_bad_url(_fake_feishu):
    out = await dispatch_tool(_ctx(), "feishu_folder_list", {"url": "https://example.com/x"})
    assert "无法识别" in out


# ---------- doc_read：sheet ----------

@pytest.mark.asyncio
async def test_read_sheet_tsv_and_audit(_fake_feishu):
    _sheet_routes()
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_doc_read",
                              {"url": "https://xxx.feishu.cn/sheets/SheetTok1234567890ab"})
    assert "销售明细" in out and "汇总" in out
    assert "宫保鸡丁\t120\t28.5" in out
    assert "已截断" in out  # 500行×40列 > 200×30 上限
    audit = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.doc_read"]
    assert audit and '"kind": "sheet"' in audit[0].detail


@pytest.mark.asyncio
async def test_read_sheet_pick_by_name(_fake_feishu):
    _sheet_routes()
    await dispatch_tool(_ctx(), "feishu_doc_read",
                        {"url": "https://xxx.feishu.cn/sheets/SheetTok1234567890ab", "sheet": "汇总"})
    val_calls = [u for u, _ in _FakeFeishuClient.calls if "/values/" in u]
    assert val_calls and "s2!" in val_calls[0]


@pytest.mark.asyncio
async def test_read_sheet_no_scope_guides(_fake_feishu):
    _sheet_routes()
    ctx = _ctx(scopes="docx:document:readonly drive:drive:readonly")
    out = await dispatch_tool(ctx, "feishu_doc_read",
                              {"url": "https://xxx.feishu.cn/sheets/SheetTok1234567890ab"})
    assert "/login?upgrade=1" in out


# ---------- doc_read：bitable ----------

@pytest.mark.asyncio
async def test_read_bitable_records(_fake_feishu):
    _base_routes()
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_doc_read",
                              {"url": "https://xxx.feishu.cn/base/BaseTok1234567890abc"})
    assert "合同台账" in out and "归档" in out
    assert "合同A" in out and "5000" in out and "王思远" in out  # 文本段/人员字段平铺
    assert "record_id: rec1" in out
    audit = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.doc_read"]
    assert audit and '"kind": "base"' in audit[0].detail


@pytest.mark.asyncio
async def test_read_bitable_pick_table(_fake_feishu):
    _base_routes()
    await dispatch_tool(_ctx(), "feishu_doc_read",
                        {"url": "https://xxx.feishu.cn/base/BaseTok1234567890abc", "table": "归档"})
    rec_calls = [u for u, _ in _FakeFeishuClient.calls if "/records" in u]
    assert rec_calls and "tbl2/records" in rec_calls[0]


# ---------- doc_read：file 下载落工作区 ----------

@pytest.mark.asyncio
async def test_read_file_downloads_to_workspace(_fake_feishu):
    _FakeFeishuClient.routes["/download"] = _FakeBinResp(b"PK\x03\x04fake-xlsx", "九月报表.xlsx")
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_doc_read",
                              {"url": "https://xxx.feishu.cn/file/FileTok1234567890abc"})
    assert "已下载到工作区" in out and "feishu_files/" in out and "九月报表.xlsx" in out
    ws_rows = [r for r in ctx.db.added if r.__class__.__name__ == "WorkspaceFile"]
    assert len(ws_rows) == 1
    assert ws_rows[0].filename.startswith("feishu_files/") and "九月报表.xlsx" in ws_rows[0].filename
    audit = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.doc_read"]
    assert audit and '"kind": "file"' in audit[0].detail


@pytest.mark.asyncio
async def test_read_file_sanitizes_special_chars(_fake_feishu):
    """M10a-1→M21：括号/空格已是合法字符，原名保留落盘（不再洗成下划线）。"""
    _FakeFeishuClient.routes["/download"] = _FakeBinResp(b"%PDF-fake", "报告 2026(1).pdf")
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_doc_read",
                              {"url": "https://xxx.feishu.cn/file/FileTok1234567890abc"})
    assert "报告 2026(1).pdf" in out  # 原名保留
    assert "已清洗" not in out
    ws_rows = [r for r in ctx.db.added if r.__class__.__name__ == "WorkspaceFile"]
    assert len(ws_rows) == 1 and "报告 2026(1).pdf" in ws_rows[0].filename


@pytest.mark.asyncio
async def test_read_file_sanitizes_truly_bad_chars(_fake_feishu):
    """M21：黑名单字符（* ? 等）仍被清洗，且文案注明。"""
    _FakeFeishuClient.routes["/download"] = _FakeBinResp(b"%PDF-fake", "报告*2026?.pdf")
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_doc_read",
                              {"url": "https://xxx.feishu.cn/file/FileTok1234567890abc"})
    assert "报告_2026_.pdf" in out
    assert "已清洗" in out  # 文件名变了要在文案里注明
    ws_rows = [r for r in ctx.db.added if r.__class__.__name__ == "WorkspaceFile"]
    assert len(ws_rows) == 1 and "报告_2026_.pdf" in ws_rows[0].filename


@pytest.mark.asyncio
async def test_read_file_clean_name_no_note(_fake_feishu):
    """文件名本身合法时不出现清洗说明（回归）。"""
    _FakeFeishuClient.routes["/download"] = _FakeBinResp(b"PKfake", "九月报表.xlsx")
    out = await dispatch_tool(_ctx(), "feishu_doc_read",
                              {"url": "https://xxx.feishu.cn/file/FileTok1234567890abc"})
    assert "九月报表.xlsx" in out and "已清洗" not in out


# ---------- doc_read：folder/wiki 分支 ----------

@pytest.mark.asyncio
async def test_doc_read_folder_guides_to_folder_list(_fake_feishu):
    out = await dispatch_tool(_ctx(), "feishu_doc_read",
                              {"url": "https://xxx.feishu.cn/drive/folder/FoldTok1234567890ab"})
    assert "feishu_folder_list" in out


@pytest.mark.asyncio
async def test_wiki_resolves_to_sheet(_fake_feishu):
    _sheet_routes()
    _FakeFeishuClient.routes["/wiki/v2/spaces/get_node"] = {
        "code": 0, "data": {"node": {"obj_token": "SheetTok1234567890ab",
                                      "obj_type": "sheet", "title": "九月报表"}}}
    out = await dispatch_tool(_ctx(), "feishu_doc_read",
                              {"url": "https://xxx.feishu.cn/wiki/WikiTok1234567890abcd"})
    assert "九月报表" in out and "宫保鸡丁" in out


@pytest.mark.asyncio
async def test_wiki_unsupported_type(_fake_feishu):
    _FakeFeishuClient.routes["/wiki/v2/spaces/get_node"] = {
        "code": 0, "data": {"node": {"obj_token": "x", "obj_type": "mindnote", "title": "脑图"}}}
    out = await dispatch_tool(_ctx(), "feishu_doc_read",
                              {"url": "https://xxx.feishu.cn/wiki/WikiTok1234567890abcd"})
    assert "mindnote" in out and "暂不支持" in out


# ---------- docx 回归 ----------

@pytest.mark.asyncio
async def test_docx_unchanged(_fake_feishu):
    _FakeFeishuClient.routes["/docx/v1/documents"] = {"code": 0, "data": {"content": "正文内容"}}
    out = await dispatch_tool(_ctx(), "feishu_doc_read",
                              {"url": "https://xxx.feishu.cn/docx/DocxTok1234567890abcd"})
    assert "正文内容" in out


# ---------- M29：工作区文件上传飞书云盘 ----------

_UPLOAD_SCOPES = _ALL_SCOPES + " drive:file:upload"


@pytest.mark.asyncio
async def test_file_upload_success(_fake_feishu, tmp_path):
    (tmp_path / "report.html").write_bytes(b"<html>hi</html>")

    def _upload(form):
        assert form["data"]["parent_type"] == "explorer"
        assert form["data"]["parent_node"] == "FoldTok1234567890ab"
        assert form["data"]["file_name"] == "report.html"
        assert form["data"]["size"] == str(len(b"<html>hi</html>"))
        fname, content, mime = form["files"]["file"]
        assert fname == "report.html" and content == b"<html>hi</html>" and mime == "text/html"
        return {"code": 0, "data": {"file_token": "boxcnUploadOk123"}}

    _FakeFeishuClient.routes["/drive/v1/files/upload_all"] = _upload
    ctx = _ctx(scopes=_UPLOAD_SCOPES)
    out = await dispatch_tool(ctx, "feishu_file_upload", {
        "path": "report.html", "folder": "https://xxx.feishu.cn/drive/folder/FoldTok1234567890ab"})
    assert "report.html" in out and "https://feishu.cn/file/boxcnUploadOk123" in out
    audits = [x for x in ctx.db.added if getattr(x, "action", "") == "feishu.file_upload"]
    assert len(audits) == 1
    import json as _json
    detail = _json.loads(audits[0].detail)
    assert detail["path"] == "report.html" and detail["size"] == len(b"<html>hi</html>")
    assert "<html>" not in audits[0].detail  # 审计不落文件内容


@pytest.mark.asyncio
async def test_file_upload_default_root_folder(_fake_feishu, tmp_path):
    """不传 folder → 先取「我的空间」根目录 token 再上传（需 metadata scope，M29.1）。"""
    (tmp_path / "a.txt").write_bytes(b"hello")
    _FakeFeishuClient.routes["/drive/explorer/v2/root_folder/meta"] = {
        "code": 0, "data": {"token": "RootTok1234567890abc"}}

    def _upload(form):
        assert form["data"]["parent_node"] == "RootTok1234567890abc"
        return {"code": 0, "data": {"file_token": "boxcnRoot1234567"}}

    _FakeFeishuClient.routes["/drive/v1/files/upload_all"] = _upload
    out = await dispatch_tool(_ctx(scopes=_UPLOAD_SCOPES + " drive:drive.metadata:readonly"),
                              "feishu_file_upload", {"path": "a.txt"})
    assert "boxcnRoot1234567" in out


@pytest.mark.asyncio
async def test_file_upload_root_without_metadata_scope_guides(_fake_feishu, tmp_path):
    """M29.1：不传 folder 且 token 无 metadata scope → 确定性引导（重授权或给文件夹链接），零 API 调用。"""
    (tmp_path / "a.txt").write_bytes(b"hello")
    out = await dispatch_tool(_ctx(scopes=_UPLOAD_SCOPES), "feishu_file_upload", {"path": "a.txt"})
    assert "文件夹链接" in out and "/login?upgrade=1" in out
    assert _FakeFeishuClient.calls == []


@pytest.mark.asyncio
async def test_file_upload_without_scope_guides(_fake_feishu, tmp_path):
    (tmp_path / "a.txt").write_bytes(b"hello")
    out = await dispatch_tool(_ctx(), "feishu_file_upload", {"path": "a.txt"})  # _ALL_SCOPES 无上传 scope
    assert "/login?upgrade=1" in out
    assert _FakeFeishuClient.calls == []  # 缺 scope 不打任何飞书 API


@pytest.mark.asyncio
async def test_file_upload_bad_path_rejected(_fake_feishu, tmp_path):
    out = await dispatch_tool(_ctx(scopes=_UPLOAD_SCOPES), "feishu_file_upload", {"path": "../secret.txt"})
    assert "不合法" in out  # valid_rel 黑名单段拦截（.. 段）
    assert _FakeFeishuClient.calls == []


@pytest.mark.asyncio
async def test_file_upload_missing_file(_fake_feishu):
    out = await dispatch_tool(_ctx(scopes=_UPLOAD_SCOPES), "feishu_file_upload", {"path": "nope.txt"})
    assert "没找到" in out
    assert _FakeFeishuClient.calls == []


@pytest.mark.asyncio
async def test_file_upload_too_large(_fake_feishu, tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_FEISHU_UPLOAD_MAX", 10)
    (tmp_path / "big.bin").write_bytes(b"x" * 100)
    out = await dispatch_tool(_ctx(scopes=_UPLOAD_SCOPES), "feishu_file_upload", {"path": "big.bin"})
    assert "上限" in out
    assert _FakeFeishuClient.calls == []


@pytest.mark.asyncio
async def test_file_upload_bad_folder_link(_fake_feishu, tmp_path):
    (tmp_path / "a.txt").write_bytes(b"hello")
    out = await dispatch_tool(_ctx(scopes=_UPLOAD_SCOPES), "feishu_file_upload",
                              {"path": "a.txt", "folder": "随便一段文字"})
    assert "无法识别" in out
    assert _FakeFeishuClient.calls == []
