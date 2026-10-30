"""M3 逻辑测试：run_code 工具挂载/拦截、files 路径校验、沙箱不可达降级、prompt 代码执行层。"""
from types import SimpleNamespace

import pytest

from app.runtime.tools import CODE_TOOL_SCHEMAS, TOOL_SCHEMAS, ToolContext, dispatch_tool


def test_run_code_schema_grouped_separately():
    """run_code 在 CODE_TOOL_SCHEMAS（按开关挂载），不在默认 TOOL_SCHEMAS。"""
    code_names = {s["function"]["name"] for s in CODE_TOOL_SCHEMAS}
    base_names = {s["function"]["name"] for s in TOOL_SCHEMAS}
    assert "run_code" in code_names
    assert "run_code" not in base_names
    schema = next(s for s in CODE_TOOL_SCHEMAS if s["function"]["name"] == "run_code")
    props = schema["function"]["parameters"]["properties"]
    # code 与 script 二选一（script=工作区 .py 复跑），都不必填、运行时校验
    assert "code" in props and "script" in props


class _FakeScalars:
    def all(self):
        return []

    def scalar_one_or_none(self):
        return None


class _FakeResult:
    def scalars(self):
        return _FakeScalars()

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


def _ctx(allow_code_exec=False):
    return ToolContext(
        db=_FakeDB(),
        user=SimpleNamespace(id=1),
        agent=SimpleNamespace(id=1),
        allow_code_exec=allow_code_exec,
    )


@pytest.mark.asyncio
async def test_run_code_rejected_without_switch():
    """未开通代码执行的数字人，dispatch 层拦截（双保险）。"""
    r = await dispatch_tool(_ctx(allow_code_exec=False), "run_code", {"code": "print(1)"})
    assert "未开通代码执行" in r


@pytest.mark.asyncio
async def test_run_code_rejects_bad_file_path():
    r = await dispatch_tool(_ctx(allow_code_exec=True), "run_code", {"code": "print(1)", "files": ["../etc/passwd"]})
    assert "不合法" in r
    r = await dispatch_tool(_ctx(allow_code_exec=True), "run_code", {"code": "print(1)", "files": "not-a-list"})
    assert "数组" in r


@pytest.mark.asyncio
async def test_run_code_empty_code():
    r = await dispatch_tool(_ctx(allow_code_exec=True), "run_code", {"code": "  "})
    assert "代码为空" in r


@pytest.mark.asyncio
async def test_run_code_sandbox_unavailable(monkeypatch):
    """沙箱服务不可达（client 返回 None）→ 友好提示，不抛异常。"""
    from app.runtime import sandbox_client

    async def _none(code, files, timeout_s=None):
        return None

    monkeypatch.setattr(sandbox_client, "exec_code", _none)
    r = await dispatch_tool(_ctx(allow_code_exec=True), "run_code", {"code": "print(1)"})
    assert "暂不可用" in r


@pytest.mark.asyncio
async def test_run_code_timeout_s_validation():
    """M3.5：timeout_s 旋钮校验（默认 600，模型可调小快速失败）。"""
    r = await dispatch_tool(_ctx(allow_code_exec=True), "run_code", {"code": "print(1)", "timeout_s": 601})
    assert "1-600" in r
    r = await dispatch_tool(_ctx(allow_code_exec=True), "run_code", {"code": "print(1)", "timeout_s": "60"})
    assert "1-600" in r


@pytest.mark.asyncio
async def test_run_code_writes_audit_and_outputs(monkeypatch, tmp_path):
    """沙箱返回产物 → 回写 workspace + 写 audit_logs。"""
    from app.runtime import sandbox_client, tools

    async def _fake(code, files, timeout_s=None):
        return {
            "ok": True,
            "exit_code": 0,
            "stdout": "done",
            "stderr": "",
            "duration_ms": 123,
            "outputs": [
                {"path": "result.txt", "content_b64": "aGVsbG8=", "size": 5},  # "hello"
                {"path": "chart.png", "content_b64": "iVBORw0KGgo=", "size": 8},
            ],
        }

    monkeypatch.setattr(sandbox_client, "exec_code", _fake)
    # 工作区根目录指向临时目录
    monkeypatch.setattr(tools, "_workspace_root", lambda u, a: tmp_path)
    monkeypatch.setattr(tools, "_trace_root", lambda u, a: tmp_path / "traces")

    ctx = _ctx(allow_code_exec=True)
    r = await dispatch_tool(ctx, "run_code", {"code": "print('done')"})
    assert "exit_code=0" in r
    assert "产物已存回 workspace" in r
    assert "result.txt" in r
    # 产物按 ./output/ 相对路径原样落位（2026-08-19 起不再加 sandbox_outputs/ 前缀）
    assert (tmp_path / "result.txt").read_text() == "hello"
    assert (tmp_path / "chart.png").is_file()
    result_row = next(x for x in ctx.db.added if getattr(x, "filename", "") == "result.txt")
    assert result_row.content_text == "hello"  # NUL 守卫不能误伤正常文本产物
    # 审计记录
    audits = [x for x in ctx.db.added if getattr(x, "action", "") == "sandbox.exec"]
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_run_code_empty_zip_output_stays_binary(monkeypatch, tmp_path):
    """2026-10-19 现场：22 字节空 zip 能被 UTF-8 解码但含 0x00，登记 content_text 会让 PostgreSQL 拒绝 INSERT。"""
    from app.runtime import sandbox_client, tools

    empty_zip = b"PK\x05\x06" + b"\x00" * 18

    async def _fake(code, files, timeout_s=None):
        return {
            "ok": True, "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1,
            "outputs": [{
                "path": "盒马/建档20261016/SOP图片_261016v1_8款.zip",
                "content_b64": "UEsFBgAAAAAAAAAAAAAAAAAAAAAAAA==",
                "size": len(empty_zip),
            }],
        }

    monkeypatch.setattr(sandbox_client, "exec_code", _fake)
    monkeypatch.setattr(tools, "_workspace_root", lambda u, a: tmp_path)
    monkeypatch.setattr(tools, "_trace_root", lambda u, a: tmp_path / "traces")

    ctx = _ctx(allow_code_exec=True)
    r = await dispatch_tool(ctx, "run_code", {"code": "pass"})
    assert "exit_code=0" in r
    path = "盒马/建档20261016/SOP图片_261016v1_8款.zip"
    assert (tmp_path / path).read_bytes() == empty_zip
    row = next(x for x in ctx.db.added if getattr(x, "filename", "") == path)
    assert row.size == 22
    assert row.mime in {"application/zip", "application/x-zip-compressed"}  # mimetypes 在 Windows/Linux 命名不同
    assert row.content_text is None


@pytest.mark.asyncio
async def test_workspace_write_strips_nul_from_content_text(monkeypatch, tmp_path):
    """JSON 里的 \\u0000 到 Python 是真实 NUL；磁盘内容保留，登记文本必须去掉（PG text 禁 0x00）。"""
    from app.runtime import tools

    monkeypatch.setattr(tools, "_workspace_root", lambda u, a: tmp_path)
    ctx = _ctx()
    r = await dispatch_tool(ctx, "workspace_write", {"path": "nul-content.txt", "content": "a\x00b"})
    assert "已写入" in r
    assert (tmp_path / "nul-content.txt").read_bytes() == b"a\x00b"  # 热修只保护 DB 字段，不改写用户文件
    row = next(x for x in ctx.db.added if getattr(x, "filename", "") == "nul-content.txt")
    assert row.content_text == "ab"


@pytest.mark.asyncio
async def test_run_code_overwrites_existing_workspace_file(monkeypatch, tmp_path):
    """M3.5：产物相对路径与已有文件相同 → 覆盖更新原文件，不再落 sandbox_outputs。"""
    from app.models.agent import WorkspaceFile
    from app.runtime import sandbox_client, tools

    async def _fake(code, files, timeout_s=None):
        return {
            "ok": True, "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1,
            "outputs": [{"path": "base.parquet", "content_b64": "aGVsbG8=", "size": 5}],
        }

    monkeypatch.setattr(sandbox_client, "exec_code", _fake)
    monkeypatch.setattr(tools, "_workspace_root", lambda u, a: tmp_path)
    monkeypatch.setattr(tools, "_trace_root", lambda u, a: tmp_path / "traces")

    existing = WorkspaceFile(
        user_id=1, agent_id=1, filename="base.parquet", mime="application/octet-stream",
        size=1, storage_path=str(tmp_path / "base.parquet"), content_text=None,
    )

    class _DB(_FakeDB):
        async def execute(self, *a, **k):
            r = _FakeResult()
            r.scalar_one_or_none = lambda: existing  # 第一个查询（同路径已有文件）命中
            return r

    ctx = ToolContext(db=_DB(), user=SimpleNamespace(id=1), agent=SimpleNamespace(id=1), allow_code_exec=True)
    r = await dispatch_tool(ctx, "run_code", {"code": "pass"})
    assert "已覆盖更新 workspace 原文件" in r and "base.parquet" in r
    assert "sandbox_outputs" not in r
    assert existing.size == 5 and (tmp_path / "base.parquet").read_text() == "hello"


def test_prompt_code_exec_layer():
    """CODE_EXEC_LAYER 存在且 PLATFORM_LAYER 默认不含 run_code（按开关拼接）。"""
    from app.runtime.prompt import CODE_EXEC_LAYER, PLATFORM_LAYER

    assert "run_code" in CODE_EXEC_LAYER
    assert "无外网" in CODE_EXEC_LAYER
    assert "run_code" not in PLATFORM_LAYER


# ---------- 代码留痕与 script 复用（M4c） ----------

@pytest.mark.asyncio
async def test_run_code_inline_saves_trace(monkeypatch, tmp_path):
    """inline code 留痕到平台侧 traces/ 目录：落盘 + 审计带路径；不进工作区、不登记 WorkspaceFile。"""
    from app.models.agent import WorkspaceFile
    from app.runtime import sandbox_client, tools

    async def _fake(code, files, timeout_s=None):
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1, "outputs": []}

    monkeypatch.setattr(sandbox_client, "exec_code", _fake)
    monkeypatch.setattr(tools, "_workspace_root", lambda u, a: tmp_path / "ws")
    tdir = tmp_path / "traces"
    monkeypatch.setattr(tools, "_trace_root", lambda u, a: tdir)

    ctx = _ctx(allow_code_exec=True)
    r = await dispatch_tool(ctx, "run_code", {"code": "print('trace me')"})
    assert "exit_code=0" in r
    traces = list(tdir.glob("run_*.py"))
    assert len(traces) == 1
    assert "trace me" in traces[0].read_text(encoding="utf-8")
    # 不污染工作区：无 scripts/ 目录、无 WorkspaceFile 记录
    assert not (tmp_path / "ws" / "scripts").exists()
    assert not [x for x in ctx.db.added if isinstance(x, WorkspaceFile)]
    audits = [x for x in ctx.db.added if getattr(x, "action", "") == "sandbox.exec"]
    assert '"trace"' in audits[0].detail and "run_" in audits[0].detail


@pytest.mark.asyncio
async def test_run_code_failed_exec_still_saves_trace(monkeypatch, tmp_path):
    """执行失败（exit_code != 0）也要留痕——出错的代码正是排查最需要的。"""
    from app.runtime import sandbox_client, tools

    async def _fake(code, files, timeout_s=None):
        return {"ok": False, "exit_code": 1, "stdout": "", "stderr": "boom", "duration_ms": 1, "outputs": []}

    monkeypatch.setattr(sandbox_client, "exec_code", _fake)
    monkeypatch.setattr(tools, "_workspace_root", lambda u, a: tmp_path / "ws")
    tdir = tmp_path / "traces"
    monkeypatch.setattr(tools, "_trace_root", lambda u, a: tdir)

    await dispatch_tool(_ctx(allow_code_exec=True), "run_code", {"code": "raise Exception()"})
    assert len(list(tdir.glob("run_*.py"))) == 1


@pytest.mark.asyncio
async def test_run_code_trace_retention(monkeypatch, tmp_path):
    """留痕只保留最近 N 份，防无限增长。"""
    from app.runtime import sandbox_client, tools

    async def _fake(code, files, timeout_s=None):
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1, "outputs": []}

    monkeypatch.setattr(sandbox_client, "exec_code", _fake)
    monkeypatch.setattr(tools, "_workspace_root", lambda u, a: tmp_path / "ws")
    tdir = tmp_path / "traces"
    monkeypatch.setattr(tools, "_trace_root", lambda u, a: tdir)
    monkeypatch.setattr(tools, "_TRACE_KEEP", 3)

    ctx = _ctx(allow_code_exec=True)
    for i in range(5):
        await dispatch_tool(ctx, "run_code", {"code": f"print({i})"})
    assert len(list(tdir.glob("run_*.py"))) == 3


@pytest.mark.asyncio
async def test_run_code_script_mode(monkeypatch, tmp_path):
    """script 模式：薄壳 code runpy 执行 + 脚本带入 payload + 不留痕。"""
    from app.runtime import sandbox_client, tools

    script = tmp_path / "ws" / "财务数据核对" / "job.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('hi')", encoding="utf-8")

    captured: dict = {}

    async def _fake(code, files, timeout_s=None):
        captured["code"] = code
        captured["files"] = files
        return {"ok": True, "exit_code": 0, "stdout": "hi", "stderr": "", "duration_ms": 1, "outputs": []}

    monkeypatch.setattr(sandbox_client, "exec_code", _fake)
    monkeypatch.setattr(tools, "_workspace_root", lambda u, a: tmp_path / "ws")
    tdir = tmp_path / "traces"
    monkeypatch.setattr(tools, "_trace_root", lambda u, a: tdir)

    r = await dispatch_tool(_ctx(allow_code_exec=True), "run_code", {"script": "财务数据核对/job.py"})
    assert "runpy.run_path('./财务数据核对/job.py'" in captured["code"]
    assert any(f["path"] == "财务数据核对/job.py" for f in captured["files"])
    assert "本次执行的是工作区脚本" in r
    assert not tdir.exists() or not list(tdir.glob("run_*.py"))  # script 模式不留痕


@pytest.mark.asyncio
async def test_run_code_script_validation(monkeypatch, tmp_path):
    """script 路径校验：越界 / 非 .py / 文件不存在 都拒绝。"""
    from app.runtime import tools

    monkeypatch.setattr(tools, "_workspace_root", lambda u, a: tmp_path)
    ctx = _ctx(allow_code_exec=True)
    assert "不合法" in await dispatch_tool(ctx, "run_code", {"script": "../x.py"})
    assert "不合法" in await dispatch_tool(ctx, "run_code", {"script": "scripts/a.txt"})
    assert "不存在" in await dispatch_tool(ctx, "run_code", {"script": "scripts/missing.py"})


# ---------- 断线修复（M3.1）：tool_calls 悬挂自愈 ----------

def test_repair_dangling_tool_calls_fills_missing():
    from app.runtime.engine import _repair_dangling_tool_calls

    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "a1"}, {"id": "a2"}]},
        {"role": "tool", "tool_call_id": "a1", "content": "ok"},  # a2 的响应丢失（中断残留）
        {"role": "user", "content": "next"},
    ]
    out = _repair_dangling_tool_calls(msgs)
    # 合成 tool 消息必须紧跟在已有 tool 响应之后、下一条 user 之前
    kinds = [(m["role"], m.get("tool_call_id")) for m in out]
    assert ("tool", "a2") in kinds
    last_user = len(kinds) - 1 - kinds[::-1].index(("user", None))  # 最后一个 user（"next"）
    assert kinds.index(("tool", "a2")) < last_user
    assert kinds.index(("tool", "a2")) > kinds.index(("tool", "a1"))
    # 原有消息不丢
    assert len(out) == len(msgs) + 1


def test_repair_wellformed_unchanged():
    from app.runtime.engine import _repair_dangling_tool_calls

    msgs = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "a1"}]},
        {"role": "tool", "tool_call_id": "a1", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]
    assert _repair_dangling_tool_calls(msgs) == msgs


def test_repair_trailing_dangling():
    """assistant tool_calls 在末尾且完全没有 tool 响应（中断发生在执行前）。"""
    from app.runtime.engine import _repair_dangling_tool_calls

    msgs = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "a1"}]},
    ]
    out = _repair_dangling_tool_calls(msgs)
    assert out[-1]["role"] == "tool" and out[-1]["tool_call_id"] == "a1"
