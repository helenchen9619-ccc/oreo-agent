"""M9b/M9b.2 逻辑测试：feishu_mail_daily 邮件日报（per-user token + 时间窗截停 + 未读/重要标记 + 收发件人）。

契约全部来自 2026-08-31 云上探针（check_feishu_mail.py）：
- 列表 items 是 message_id 字符串（倒序）；folder_id 是 query 参数
- 详情 format=metadata：明文 subject/body_preview + attachments[{filename}] + label_ids + internal_date 毫秒戳

M9b.2 新增：
- 默认同时扫收件箱（INBOX，含发件人 head_from）与发件箱（SENT，含收件人 to）
- 收发件人字段（head_from/to/cc/bcc）需 scope mail:user_mailbox.message.address:read 才下发；缺了 sender/recipients 为空、照常显示不阻塞
"""
import time
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


_MAIL_SCOPES = (
    "mail:user_mailbox.message:readonly mail:user_mailbox.message.subject:read "
    "mail:user_mailbox.message.body:read mail:user_mailbox.message.address:read "
    "mail:user_mailbox.folder:read"
)


def _ctx(allow=True, scopes=_MAIL_SCOPES):
    return ToolContext(
        db=_FakeDB(),
        user=SimpleNamespace(id=1, feishu_open_id="ou_self", username="u1", feishu_scopes=scopes),
        agent=SimpleNamespace(id=1),
        allow_feishu=allow,
    )


# ---------- 假飞书 API（URL 前缀路由，同 test_m8_daily 约定） ----------
# 注意：详情 URL 包含列表 URL 前缀（…/messages/{id} ⊃ …/messages），详情路由必须先注册。

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


def _msg(mid, subject, ms, labels=None, preview="正文预览", atts=None,
         head_from=None, to=None, cc=None, folder_id="INBOX"):
    msg = {
        "message_id": mid, "subject": subject, "body_preview": preview,
        "internal_date": str(ms), "label_ids": labels or [],
        "attachments": [{"filename": a, "attachment_type": 1, "id": f"att_{a}"} for a in (atts or [])],
        "folder_id": folder_id,
    }
    if head_from:
        msg["head_from"] = head_from
    if to:
        msg["to"] = to
    if cc:
        msg["cc"] = cc
    return {"code": 0, "data": {"message": msg}}


def _setup_routes(inbox=None, sent=None, inbox_unread=7, sent_unread=0):
    """inbox/sent = [(message_id, 响应payload), …]，各自按时间倒序（与真实接口一致）。"""
    inbox = inbox or []
    sent = sent or []
    for mid, payload in inbox + sent:  # 详情路由先注册（前缀包含关系）
        _FakeFeishuClient.routes[f"/mail/v1/user_mailboxes/me/messages/{mid}"] = payload

    def _list(params):
        fid = (params or {}).get("folder_id", "INBOX")
        items = [mid for mid, _ in (sent if fid == "SENT" else inbox)]
        return {"code": 0, "data": {"items": items, "has_more": False, "page_token": ""}}

    _FakeFeishuClient.routes["/mail/v1/user_mailboxes/me/messages"] = _list
    _FakeFeishuClient.routes["/mail/v1/user_mailboxes/me/folders"] = {
        "code": 0, "data": {"items": [
            {"id": "INBOX", "name": "收件箱", "unread_message_count": inbox_unread},
            {"id": "SENT", "name": "已发送", "unread_message_count": sent_unread},
        ]}}


def _recent_ms(hours=2):
    return int(time.time() * 1000) - hours * 3600_000


@pytest.mark.asyncio
async def test_gate_off_refused(_fake_feishu):
    out = await dispatch_tool(_ctx(allow=False), "feishu_mail_daily", {})
    assert "未开通飞书内容读取能力" in out


@pytest.mark.asyncio
async def test_no_token_guides_upgrade(_fake_feishu, monkeypatch):
    async def _no_token(db, user):
        return None

    monkeypatch.setattr(tools, "get_feishu_user_token", _no_token)
    out = await dispatch_tool(_ctx(), "feishu_mail_daily", {})
    assert "/login?upgrade=1" in out
    assert not _FakeFeishuClient.calls  # 没授权就不该发任何请求


@pytest.mark.asyncio
async def test_missing_mail_scope_guides_upgrade(_fake_feishu):
    """M9b 修复：普通登录把内容 scope 清成最小权限后，直接给授权引导，不再让 field validation failed 裸奔。"""
    out = await dispatch_tool(_ctx(scopes="auth:user.id:read"), "feishu_mail_daily", {})
    assert "/login?upgrade=1" in out
    assert not _FakeFeishuClient.calls  # scope 不够就不该发任何请求


@pytest.mark.asyncio
async def test_feishu_api_error_mail_message(_fake_feishu):
    """M9b 修复：FeishuApiError 换邮箱专属文案（覆盖邮箱未开通/授权过期），数字人不再瞎猜。"""
    _FakeFeishuClient.routes["/mail/v1/user_mailboxes/me/messages"] = {
        "code": 999, "msg": "field validation failed"}
    out = await dispatch_tool(_ctx(), "feishu_mail_daily", {})
    assert "邮箱读取失败" in out
    assert "field validation failed" in out
    assert "/login?upgrade=1" in out


@pytest.mark.asyncio
async def test_page_size_capped_at_20(_fake_feishu):
    """M9b 修复：邮箱列表 page_size 上限 20，超了报 99992402——即便 max_messages=50 也不得发超限请求。"""
    _setup_routes(inbox=[("m1", _msg("m1", "一封", _recent_ms(1)))])
    await dispatch_tool(_ctx(), "feishu_mail_daily", {"max_messages": 50})
    list_calls = [p for _, p in _FakeFeishuClient.calls if p and "folder_id" in p]
    assert list_calls
    assert all((p.get("page_size") or 0) <= 20 for p in list_calls)


@pytest.mark.asyncio
async def test_daily_success_and_audit(_fake_feishu):
    _setup_routes(
        inbox=[
            ("m1", _msg("m1", "8月数据月报请查收", _recent_ms(1),
                        labels=["UNREAD", "IMPORTANT"], atts=["月报.xlsx"],
                        head_from={"name": "张三", "mail_address": "zhang@x.com"})),
            ("m2", _msg("m2", "系统升级维护通知", _recent_ms(3), labels=["UNREAD"])),
            ("m3", _msg("m3", "下午茶接龙", _recent_ms(5))),
        ],
        sent=[
            ("s1", _msg("s1", "预算审批提交", _recent_ms(2),
                        to=[{"name": "李四", "mail_address": "li@x.com"}])),
        ],
        inbox_unread=7,
    )
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_mail_daily", {})
    # 两段：收件箱 + 发件箱
    assert "收件箱近 1 天共 3 封邮件" in out
    assert "收件箱当前总未读 7 封" in out
    assert "发件箱近 1 天共 1 封邮件" in out
    # 收件箱：发件人来自 head_from
    assert "[未读][重要]8月数据月报请查收" in out
    assert "发件人：张三" in out
    assert "📎1个附件：月报.xlsx" in out
    assert "[未读]系统升级维护通知" in out
    assert "[未读]下午茶" not in out  # 无标记邮件不带旗标
    # 发件箱：显示收件人
    assert "预算审批提交" in out and "收件人：李四" in out
    # 审计：只记计数，不落主题/正文/收发件人
    audit = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.mail_daily"]
    assert len(audit) == 1
    assert '"messages": 4' in audit[0].detail and '"unread": 2' in audit[0].detail
    assert '"folder": "both"' in audit[0].detail
    assert "月报" not in audit[0].detail and "张三" not in audit[0].detail and "user_tok" not in audit[0].detail


@pytest.mark.asyncio
async def test_time_window_cutoff(_fake_feishu):
    """倒序列表撞到窗口边缘即截停：其后的邮件连详情都不应再取。"""
    old_ms = int(time.time() * 1000) - 3 * 86400_000  # 3 天前，days=1 窗口外
    _setup_routes(inbox=[
        ("m1", _msg("m1", "今天的邮件", _recent_ms(1))),
        ("m2", _msg("m2", "三天前的邮件", old_ms)),
        ("m3", _msg("m3", "更早的邮件", old_ms - 86400_000)),
    ])
    out = await dispatch_tool(_ctx(), "feishu_mail_daily", {"days": 1})
    assert "收件箱近 1 天共 1 封邮件" in out
    assert "今天的邮件" in out and "三天前的邮件" not in out
    assert "另有 2 封更早的未列出" in out
    detail_calls = [url for url, _ in _FakeFeishuClient.calls
                    if "/messages/m" in url]
    assert any("messages/m2" in u for u in detail_calls)  # m2 取了才知道超窗
    assert not any("messages/m3" in u for u in detail_calls)  # m3 不该再取


@pytest.mark.asyncio
async def test_detail_failure_degrades(_fake_feishu):
    _setup_routes(inbox=[
        ("m1", _msg("m1", "正常邮件", _recent_ms(1))),
        ("m2", {"code": 999, "msg": "message deleted"}),
        ("m3", _msg("m3", "另一封正常邮件", _recent_ms(4))),
    ])
    out = await dispatch_tool(_ctx(), "feishu_mail_daily", {})
    assert "收件箱近 1 天共 2 封邮件" in out
    assert "正常邮件" in out and "另一封正常邮件" in out
    assert "1 封读取失败已跳过" in out


@pytest.mark.asyncio
async def test_empty_window(_fake_feishu):
    _setup_routes()
    out = await dispatch_tool(_ctx(), "feishu_mail_daily", {})
    assert "收件箱近 1 天共 0 封邮件" in out
    assert "发件箱近 1 天共 0 封邮件" in out
    assert "时间窗内没有邮件" in out


@pytest.mark.asyncio
async def test_folder_failure_not_blocking(_fake_feishu):
    """文件夹列表挂掉只是没有总未读数，主流程照常。"""
    _setup_routes(inbox=[("m1", _msg("m1", "正常邮件", _recent_ms(1)))])
    _FakeFeishuClient.routes["/mail/v1/user_mailboxes/me/folders"] = {
        "code": 999, "msg": "folder scope missing"}
    out = await dispatch_tool(_ctx(), "feishu_mail_daily", {})
    assert "收件箱近 1 天共 1 封邮件" in out and "正常邮件" in out
    assert "总未读" not in out


# ---------- folder 参数 ----------

@pytest.mark.asyncio
async def test_folder_sent_only(_fake_feishu):
    """folder=sent 只扫发件箱，不碰收件箱。"""
    _setup_routes(
        inbox=[("m1", _msg("m1", "收件箱邮件", _recent_ms(1)))],
        sent=[("s1", _msg("s1", "发出去的邮件", _recent_ms(2),
                          to=[{"name": "李四", "mail_address": "li@x.com"}]))],
    )
    out = await dispatch_tool(_ctx(), "feishu_mail_daily", {"folder": "sent"})
    assert "发件箱近 1 天共 1 封邮件" in out
    assert "发出去的邮件" in out and "收件人：李四" in out
    assert "收件箱" not in out  # 只扫发件箱


@pytest.mark.asyncio
async def test_folder_inbox_only(_fake_feishu):
    """folder=inbox 只扫收件箱。"""
    _setup_routes(
        inbox=[("m1", _msg("m1", "收件箱邮件", _recent_ms(1),
                           head_from={"name": "张三", "mail_address": "z@x.com"}))],
        sent=[("s1", _msg("s1", "发出去的邮件", _recent_ms(2)))],
    )
    out = await dispatch_tool(_ctx(), "feishu_mail_daily", {"folder": "inbox"})
    assert "收件箱近 1 天共 1 封邮件" in out and "收件箱邮件" in out
    assert "发件人：张三" in out
    assert "发件箱" not in out


@pytest.mark.asyncio
async def test_invalid_folder_falls_back_to_both(_fake_feishu):
    _setup_routes(inbox=[("m1", _msg("m1", "一封", _recent_ms(1)))])
    out = await dispatch_tool(_ctx(), "feishu_mail_daily", {"folder": "bogus"})
    assert "收件箱近 1 天共 1 封邮件" in out
    assert "发件箱近 1 天共 0 封邮件" in out  # 回退 both


# ---------- _mail_line 单元 ----------

def test_mail_line_flags_and_attachments():
    line = tools._mail_line({
        "subject": "主题", "preview": "  多  行\n预览  ", "date_ms": 1760000000000,
        "labels": ["UNREAD", "IMPORTANT", "STARRED"],  # 未知标签忽略
        "attachments": ["a.csv", "b.pdf"], "sender": "",
    })
    assert line.startswith("- [未读][重要]主题（")
    assert "多 行 预览" in line  # 空白折叠
    assert "📎2个附件：a.csv、b.pdf" in line
    assert "STARRED" not in line


def test_mail_line_minimal_and_truncation():
    line = tools._mail_line({"subject": "", "preview": "", "date_ms": 0,
                             "labels": [], "attachments": [], "sender": ""})
    assert line == "- （无主题）"
    long_prev = {"subject": "s", "preview": "长" * 500, "date_ms": 0,
                 "labels": [], "attachments": [], "sender": ""}
    assert "长" * tools._MAIL_PREVIEW_MAX in tools._mail_line(long_prev)
    assert "长" * (tools._MAIL_PREVIEW_MAX + 1) not in tools._mail_line(long_prev)


def test_mail_line_sender_shown_when_present():
    """收件箱邮件：发件人来自 head_from 解析，有则显示。"""
    line = tools._mail_line({"subject": "s", "preview": "", "date_ms": 1760000000000,
                             "labels": [], "attachments": [], "sender": "王五"})
    assert "发件人：王五" in line


def test_mail_line_sent_shows_recipient_and_cc():
    """发件箱邮件：显示收件人（含抄送），不显示发件人。"""
    line = tools._mail_line({"subject": "s", "preview": "", "date_ms": 1760000000000,
                             "labels": [], "attachments": [], "sender": "自己",
                             "recipients": ["李四", "王五"], "cc": ["赵六"], "kind": "sent"})
    assert "收件人：李四、王五" in line
    assert "抄送：赵六" in line
    assert "发件人" not in line  # 发件箱不显示发件人


def test_mail_line_missing_address_scope_still_renders():
    """M9b.2：缺 address:read scope 时 sender/recipients 为空，行照常渲染、不报错。"""
    line = tools._mail_line({"subject": "s", "preview": "正文", "date_ms": 1760000000000,
                             "labels": [], "attachments": [], "sender": "", "kind": "sent"})
    assert "s" in line and "正文" in line
    assert "收件人" not in line and "发件人" not in line


# ---------- feishu_mail_search（M9b.3：关键词检索） ----------

def _setup_search(ids: list[str]):
    """注册搜索接口路由：POST /user_mailboxes/me/search，返回 ids 作为命中 items[].id。"""
    def _search(body):
        items = [{"id": mid, "display_info": f"命中 {mid}"} for mid in ids]
        return {"code": 0, "data": {"items": items}}

    _FakeFeishuClient.routes["/mail/v1/user_mailboxes/me/search"] = _search


@pytest.mark.asyncio
async def test_mail_search_hits_and_audit(_fake_feishu):
    """关键词命中收件箱（显发件人）+ 发件箱（显收件人）；审计只记计数不落正文。"""
    _setup_routes(
        inbox=[("m1", _msg("m1", "美团合同请查收", _recent_ms(1),
                           head_from={"name": "美团销售", "mail_address": "meituan@x.com"}))],
        sent=[("s1", _msg("s1", "合同续费确认", _recent_ms(2),
                          to=[{"name": "胡莹健", "mail_address": "yingjian.hu@x.com"}],
                          folder_id="SENT"))],
    )
    _setup_search(["m1", "s1"])
    ctx = _ctx()
    out = await dispatch_tool(ctx, "feishu_mail_search", {"query": "合同"})
    assert "邮箱搜索「合同」命中 2 封" in out
    assert "美团合同请查收" in out and "发件人：美团销售" in out
    assert "合同续费确认" in out and "收件人：胡莹健" in out
    audit = [r for r in ctx.db.added if getattr(r, "action", "") == "feishu.mail_search"]
    assert len(audit) == 1
    assert '"query": "合同"' in audit[0].detail and '"results": 2' in audit[0].detail
    assert "user_tok" not in audit[0].detail and "美团" not in audit[0].detail


@pytest.mark.asyncio
async def test_mail_search_empty_query_guides(_fake_feishu):
    out = await dispatch_tool(_ctx(), "feishu_mail_search", {})
    assert "关键词" in out
    assert not _FakeFeishuClient.calls  # 没关键词就不该发请求


@pytest.mark.asyncio
async def test_mail_search_no_scope_guides(_fake_feishu):
    out = await dispatch_tool(_ctx(scopes="auth:user.id:read"), "feishu_mail_search", {"query": "合同"})
    assert "/login?upgrade=1" in out
    assert not _FakeFeishuClient.calls


@pytest.mark.asyncio
async def test_mail_search_no_hits(_fake_feishu):
    _setup_routes()
    _setup_search([])
    out = await dispatch_tool(_ctx(), "feishu_mail_search", {"query": "不存在的词"})
    assert "没找到匹配的邮件" in out


@pytest.mark.asyncio
async def test_mail_search_caps_results(_fake_feishu):
    """max_results 截断：搜索命中 5 封，只取前 max_results 封。"""
    _setup_routes(inbox=[(f"m{i}", _msg(f"m{i}", f"邮件{i}", _recent_ms(i))) for i in range(5)])
    _setup_search([f"m{i}" for i in range(5)])
    out = await dispatch_tool(_ctx(), "feishu_mail_search", {"query": "邮件", "max_results": 3})
    assert "命中 3 封" in out
    assert "邮件4" not in out  # 第 4、5 封被截断


@pytest.mark.asyncio
async def test_mail_search_detail_failure_degrades(_fake_feishu):
    """命中但详情读取失败（如已删除）→ 跳过并标注，不阻塞。"""
    _setup_routes(inbox=[
        ("m1", _msg("m1", "正常", _recent_ms(1))),
        ("bad", {"code": 999, "msg": "message deleted"}),
    ])
    _setup_search(["m1", "bad"])
    out = await dispatch_tool(_ctx(), "feishu_mail_search", {"query": "正常"})
    assert "命中 1 封" in out and "正常" in out
    assert "1 封读取失败已跳过" in out
