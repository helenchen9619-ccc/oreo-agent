"""M32 飞书读写体验修复包测试：sheet 浮动图片 / docx GFM 表格 / bitable 建数据表+关联字段 / 表情回复。

- M32a：float_images 列表→drive 媒体下载→feishu_imgs/ 落盘+标注；无图/下载失败降级
- M32b：_md_to_blocks 表格→哨兵块；写入方分段下发（children + descendant 嵌套接口）；
  descendants payload=31/32/2 三层；30 行×10 列截断
- M32c：feishu_bitable_create 传 app_token=已有多维表格里建数据表；关联(18)/双向关联(21)
  必须带 table_id，离线校验不过=不碰网络
- M32d：chat_history 行尾渲染表情回复（人名解析/计数降级/接口失败静默）；cap 20 防爆量

fake 坑沿用：前缀路由更具体的前缀先注册（"/descendant" 先于 "/blocks/"）；
下载类函数直接 monkeypatch tools 模块名（二进制不走 json fake）。
"""
import json
from types import SimpleNamespace

import pytest

from app.feishu.client import FeishuApiError
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


_SCOPES = ("docx:document:readonly docx:document wiki:wiki:readonly drive:drive:readonly "
           "sheets:spreadsheet:readonly sheets:spreadsheet im:message "
           "bitable:app:readonly bitable:app contact:contact.base:readonly")


def _ctx(scopes=_SCOPES):
    return ToolContext(
        db=_FakeDB(),
        user=SimpleNamespace(id=1, username="u1", feishu_open_id="ou_self", feishu_scopes=scopes),
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


def _sheet_routes(floats=None):
    _FakeFeishuClient.routes["/sheets/query"] = {"code": 0, "data": {"sheets": [
        {"sheet_id": "s1", "title": "明细", "grid_properties": {"row_count": 10, "column_count": 5}},
    ]}}
    _FakeFeishuClient.routes["/values/"] = {"code": 0, "data": {
        "valueRange": {"values": [["名称", "数量"], ["苹果", 3]]}}}
    _FakeFeishuClient.routes["/float_images"] = {"code": 0, "data": {"items": floats or []}}


# ---------- M32a sheet 浮动图片 ----------

class TestSheetFloatImages:
    async def test_float_image_downloaded_and_annotated(self, _fake_feishu, monkeypatch, tmp_path):
        _sheet_routes(floats=[{"float_image_token": "ftAAA", "range": "s1!B2:C3"}])
        monkeypatch.setattr(tools, "_workspace_root", lambda uid, aid: tmp_path)
        calls = []

        async def _dl(client, token, ft):
            calls.append(ft)
            return b"\x89PNG-fake"

        monkeypatch.setattr(tools, "drive_download_media", _dl)
        ctx = _ctx()
        r = await dispatch_tool(ctx, "feishu_doc_read",
                                {"url": "https://x.feishu.cn/sheets/Shtoken1234567890abcd"})
        assert calls == ["ftAAA"]
        assert "本页含 1 张浮动图片" in r and "s1!B2:C3" in r and "feishu_imgs/" in r and "run_code" in r
        assert "单元格内嵌图片无接口可读" in r
        rows = [x for x in ctx.db.added if getattr(x, "filename", "").startswith("feishu_imgs/")]
        assert len(rows) == 1
        audits = [x for x in ctx.db.added if getattr(x, "action", "") == "feishu.doc_read"]
        assert json.loads(audits[0].detail)["images_saved"] == 1

    async def test_no_float_image_zero_download(self, _fake_feishu, monkeypatch, tmp_path):
        _sheet_routes(floats=[])
        monkeypatch.setattr(tools, "_workspace_root", lambda uid, aid: tmp_path)

        async def _boom(*a):
            raise AssertionError("没有浮动图片不应下载")

        monkeypatch.setattr(tools, "drive_download_media", _boom)
        r = await dispatch_tool(_ctx(), "feishu_doc_read",
                                {"url": "https://x.feishu.cn/sheets/Shtoken1234567890abcd"})
        assert "苹果" in r and "浮动图片" not in r

    async def test_download_failure_degrades(self, _fake_feishu, monkeypatch, tmp_path):
        _sheet_routes(floats=[{"float_image_token": "ftAAA", "range": "s1!A1"}])
        monkeypatch.setattr(tools, "_workspace_root", lambda uid, aid: tmp_path)

        async def _fail(*a):
            raise FeishuApiError("99991679 no scope")

        monkeypatch.setattr(tools, "drive_download_media", _fail)
        r = await dispatch_tool(_ctx(), "feishu_doc_read",
                                {"url": "https://x.feishu.cn/sheets/Shtoken1234567890abcd"})
        assert "苹果" in r  # 表格数据照常
        assert "本页含 1 张浮动图片" in r and "下载失败" in r

    async def test_float_list_failure_silent(self, _fake_feishu, monkeypatch, tmp_path):
        """float_images 接口本身失败（缺权限）→ 无标注、不阻断读表。"""
        _sheet_routes()
        _FakeFeishuClient.routes["/float_images"] = {"code": 99991679, "msg": "no scope"}
        monkeypatch.setattr(tools, "_workspace_root", lambda uid, aid: tmp_path)
        r = await dispatch_tool(_ctx(), "feishu_doc_read",
                                {"url": "https://x.feishu.cn/sheets/Shtoken1234567890abcd"})
        assert "苹果" in r and "浮动图片" not in r


# ---------- M32b docx GFM 表格 ----------

class TestMdTable:
    async def test_table_to_sentinel(self):
        blocks = tools._md_to_blocks("| 名称 | 数量 |\n| --- | --- |\n| 苹果 | 3 |")
        assert len(blocks) == 1
        assert blocks[0]["_table_rows"] == [["名称", "数量"], ["苹果", "3"]]
        assert blocks[0]["_table_truncated"] is False

    async def test_mixed_order_preserved(self):
        blocks = tools._md_to_blocks("# 标题\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n结尾段")
        assert [b.get("block_type") or "table" for b in blocks] == [3, "table", 2]

    async def test_row_cap_truncates(self):
        lines = ["| h1 | h2 |", "|---|---|"] + [f"| r{i} | v{i} |" for i in range(40)]
        blocks = tools._md_to_blocks("\n".join(lines))
        assert len(blocks) == 1
        assert len(blocks[0]["_table_rows"]) == tools._DOCX_TABLE_ROWS_MAX
        assert blocks[0]["_table_truncated"] is True

    async def test_lone_pipe_line_not_table(self):
        blocks = tools._md_to_blocks("配置项形如 a | b 二选一")
        assert len(blocks) == 1 and blocks[0]["block_type"] == 2
        assert "|" in blocks[0]["text"]["elements"][0]["text_run"]["content"]

    async def test_table_descendants_structure(self):
        d = tools._table_descendants([["h1", "h2"], ["a", "b"]], "t1")
        assert d[0]["block_type"] == 31
        assert d[0]["table"]["property"] == {"row_size": 2, "column_size": 2}
        assert len(d[0]["children"]) == 4
        cells = [b for b in d if b["block_type"] == 32]
        texts = [b for b in d if b["block_type"] == 2]
        assert len(cells) == 4 and len(texts) == 4
        # 每个 cell 恰好挂一个 text 子块，文本是单元格内容
        cell_children = {c["block_id"]: c["children"] for c in cells}
        for c in cells:
            tid = cell_children[c["block_id"]][0]
            t = next(x for x in texts if x["block_id"] == tid)
            assert t["text"]["elements"][0]["text_run"]["content"] in ("h1", "h2", "a", "b")

    async def test_blocks_plain_len_counts_cells(self):
        blocks = tools._md_to_blocks("| ab | cd |\n|---|---|\n| ef | gh |")
        assert tools._blocks_plain_len(blocks) == 8


class TestDocWriteTable:
    async def test_append_table_goes_descendant(self, _fake_feishu):
        _FakeFeishuClient.routes["/descendant"] = {"code": 0, "data": {}}
        _FakeFeishuClient.routes["/blocks/"] = {"code": 0, "data": {}}
        r = await dispatch_tool(_ctx(), "feishu_doc_append", {
            "url": "https://x.feishu.cn/docx/Doc1234567890abcdef",
            "text": "开头段\n\n| 名称 | 数量 |\n|---|---|\n| 苹果 | 3 |\n\n结尾段",
        })
        assert "3 个内容块" in r
        desc = next((u, b) for u, b in _FakeFeishuClient.calls if "descendant" in u)
        payload = desc[1]
        assert payload["children_id"] == ["t1"]
        assert payload["descendants"][0]["block_type"] == 31
        assert payload["descendants"][0]["table"]["property"] == {"row_size": 2, "column_size": 2}
        children_calls = [b for u, b in _FakeFeishuClient.calls if u.endswith("/children")]
        assert len(children_calls) == 2  # 表格前后的普通块分两段下发
        # 顺序保持：先「开头段」，再 descendant 表格，最后「结尾段」
        seq = [("desc" if "descendant" in u else "children") for u, _ in _FakeFeishuClient.calls]
        assert seq == ["children", "desc", "children"]

    async def test_edit_insert_with_table(self, _fake_feishu):
        _FakeFeishuClient.routes["/descendant"] = {"code": 0, "data": {}}

        def _children(params):
            return {"code": 0, "data": {"items": [
                {"block_id": "b0", "block_type": 2, "text": {"elements": [{"text_run": {"content": "旧段"}}]}},
            ], "has_more": False}} if params is not None and "page_size" in (params or {}) else {"code": 0, "data": {}}

        _FakeFeishuClient.routes["/blocks/"] = _children
        r = await dispatch_tool(_ctx(), "feishu_doc_edit", {
            "url": "https://x.feishu.cn/docx/Doc1234567890abcdef",
            "action": "insert", "index": 1,
            "text": "| a | b |\n|---|---|\n| 1 | 2 |",
        })
        assert "插入 1 个内容块" in r
        desc = next((u, b) for u, b in _FakeFeishuClient.calls if "descendant" in u)
        assert desc[1]["index"] == 1  # 插入位次透传


# ---------- M32c bitable 建数据表 + 关联字段 ----------

_BASE_URL = "https://x.feishu.cn/base/AppTok1234567890abcde"


class TestBitableCreateTable:
    async def test_create_table_in_existing_app(self, _fake_feishu):
        _FakeFeishuClient.routes["/bitable/v1/apps/"] = lambda body: {
            "code": 0, "data": {"table_id": "tblNew999"}}
        ctx = _ctx()
        r = await dispatch_tool(ctx, "feishu_bitable_create",
                                {"title": "门店清单", "app_token": _BASE_URL})
        assert "tblNew999" in r and "仪表盘" in r  # 顺带提醒仪表盘需手动
        body = next(b for u, b in _FakeFeishuClient.calls if b and "table" in b)
        assert body == {"table": {"name": "门店清单"}}
        audits = [x for x in ctx.db.added if getattr(x, "action", "") == "feishu.bitable_create"]
        assert json.loads(audits[0].detail)["kind"] == "table"

    async def test_create_table_with_fields_incl_link(self, _fake_feishu):
        _FakeFeishuClient.routes["/bitable/v1/apps/"] = {"code": 0, "data": {"table_id": "tblX"}}
        r = await dispatch_tool(_ctx(), "feishu_bitable_create", {
            "title": "订单", "app_token": _BASE_URL,
            "fields": [{"name": "金额", "type": "number"},
                       {"name": "门店", "type": "关联", "table_id": "tblShop", "multiple": True}],
        })
        assert "tblX" in r and "含 2 个字段" in r
        body = next(b for u, b in _FakeFeishuClient.calls if b and "table" in b)
        fields = body["table"]["fields"]
        assert fields[0] == {"field_name": "金额", "type": 2}
        assert fields[1] == {"field_name": "门店", "type": 18,
                             "property": {"table_id": "tblShop", "multiple": True}}

    async def test_link_field_without_table_id_blocked_offline(self, _fake_feishu):
        r = await dispatch_tool(_ctx(), "feishu_bitable_create", {
            "title": "订单", "app_token": _BASE_URL,
            "fields": [{"name": "门店", "type": "双向关联"}],
        })
        assert "table_id" in r
        assert not _FakeFeishuClient.calls  # 离线校验不过 = 不碰网络

    async def test_create_app_flow_regression(self, _fake_feishu):
        """不传 app_token = 原建 app 流程不变。"""
        _FakeFeishuClient.routes["/bitable/v1/apps"] = {"code": 0, "data": {
            "app": {"app_token": "appNew123", "name": "新表", "url": "",
                    "default_table_id": "tblDft"}}}
        r = await dispatch_tool(_ctx(), "feishu_bitable_create", {"title": "新表"})
        assert "多维表格《新表》" in r and "tblDft" in r

    async def test_spec_validation(self):
        assert tools._bitable_spec_to_payload({"name": "金额", "type": "数字"}) == ("金额", 2, {})
        assert tools._bitable_spec_to_payload(
            {"name": "客户", "type": "关联", "table_id": "tbl1"}) == ("客户", 18, {"table_id": "tbl1"})
        err = tools._bitable_spec_to_payload({"name": "客户", "type": "link"})
        assert isinstance(err, str) and "table_id" in err
        err = tools._bitable_spec_to_payload({"name": "x", "type": "不存在类型"})
        assert isinstance(err, str) and "type 只支持" in err


class TestBitableFieldLink:
    async def test_add_link_field(self, _fake_feishu):
        _FakeFeishuClient.routes["/fields"] = {"code": 0, "data": {"field": {"field_id": "fld9"}}}
        _FakeFeishuClient.routes["/tables"] = {"code": 0, "data": {"items": [
            {"table_id": "tbl1", "name": "订单"}]}}
        r = await dispatch_tool(_ctx(), "feishu_bitable_field", {
            "url": _BASE_URL, "action": "add",
            "fields": [{"name": "门店", "type": "双向关联", "table_id": "tblShop"}],
        })
        assert "新建 1 列" in r
        body = next(b for u, b in _FakeFeishuClient.calls
                    if b and u.endswith("/fields"))
        assert body["type"] == 21 and body["property"]["table_id"] == "tblShop"

    async def test_add_link_field_missing_table_id(self, _fake_feishu):
        r = await dispatch_tool(_ctx(), "feishu_bitable_field", {
            "url": _BASE_URL, "action": "add", "fields": [{"name": "门店", "type": "关联"}]})
        assert "table_id" in r and not _FakeFeishuClient.calls

    async def test_add_plain_field_regression(self, _fake_feishu):
        _FakeFeishuClient.routes["/fields"] = {"code": 0, "data": {"field": {"field_id": "fld1"}}}
        _FakeFeishuClient.routes["/tables"] = {"code": 0, "data": {"items": [
            {"table_id": "tbl1", "name": "订单"}]}}
        r = await dispatch_tool(_ctx(), "feishu_bitable_field", {
            "url": _BASE_URL, "action": "add", "fields": [{"name": "金额", "type": "number"}]})
        assert "新建 1 列" in r


# ---------- M32d 表情回复 ----------

def _text_msg(mid, sender, text, ts=1760000000000):
    return {"sender": {"id": sender}, "create_time": str(ts), "msg_type": "text",
            "body": {"content": json.dumps({"text": text})}, "message_id": mid}


def _chat_routes(msgs, names=None):
    _FakeFeishuClient.routes["/im/v1/chats"] = {"code": 0, "data": {
        "items": [{"chat_id": "oc_aaa", "name": "测试群"}]}}
    _FakeFeishuClient.routes["/im/v1/messages"] = {"code": 0, "data": {"items": msgs}}
    _FakeFeishuClient.routes["/contact/v3/users/batch"] = {"code": 0, "data": {
        "items": [{"open_id": k, "name": v} for k, v in (names or {}).items()]}}


class TestReactions:
    async def test_reaction_rendered_with_name(self, _fake_feishu, monkeypatch):
        _chat_routes([_text_msg("om_1", "ou_1", "方案发你了")],
                     names={"ou_1": "张三", "ou_2": "李四"})

        async def _rx(client, token, mid):
            return [{"emoji": "THUMBSUP", "operator": "ou_2"}]

        monkeypatch.setattr(tools, "message_reactions", _rx)
        r = await dispatch_tool(_ctx(), "feishu_chat_history", {"chat": "测试群"})
        assert "[表情回复: 👍 李四]" in r

    async def test_reaction_count_fallback_without_contact(self, _fake_feishu, monkeypatch):
        _chat_routes([_text_msg("om_1", "ou_1", "看下")], names={"ou_1": "张三"})

        async def _rx(client, token, mid):
            return [{"emoji": "OK", "operator": "ou_x"}, {"emoji": "OK", "operator": "ou_y"}]

        monkeypatch.setattr(tools, "message_reactions", _rx)
        r = await dispatch_tool(_ctx(), "feishu_chat_history", {"chat": "测试群"})
        assert "[表情回复: 👌×2]" in r

    async def test_reactions_failure_degrades_silently(self, _fake_feishu, monkeypatch):
        _chat_routes([_text_msg("om_1", "ou_1", "在吗")], names={"ou_1": "张三"})

        async def _boom(client, token, mid):
            raise FeishuApiError("99991679 no scope")

        monkeypatch.setattr(tools, "message_reactions", _boom)
        r = await dispatch_tool(_ctx(), "feishu_chat_history", {"chat": "测试群"})
        assert "在吗" in r and "表情回复" not in r

    async def test_reactions_cap(self, _fake_feishu, monkeypatch):
        msgs = [_text_msg(f"om_{i}", "ou_1", f"消息{i}", 1760000000000 + i * 1000)
                for i in range(25)]
        _chat_routes(msgs, names={"ou_1": "张三"})
        calls = []

        async def _rx(client, token, mid):
            calls.append(mid)
            return []

        monkeypatch.setattr(tools, "message_reactions", _rx)
        await dispatch_tool(_ctx(), "feishu_chat_history", {"chat": "测试群"})
        assert len(calls) == tools._FEISHU_REACTION_CAP

    async def test_emoji_map_fallback(self):
        assert tools._FEISHU_EMOJI["THUMBSUP"] == "👍"
        assert tools._FEISHU_EMOJI["DONE"] == "✅"
