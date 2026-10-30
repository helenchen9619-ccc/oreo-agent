"""M20 逻辑测试：对话产出文件挂消息附件（_note_created 收录 + ws_path 全链路序列化）。

- _note_created：按 ws_path 去重、上限 10、字段齐全
- workspace_write / run_code 产物：落盘后收录 created_files（含 ws_path）
- engine done 事件 / chat 历史序列化：附件元素带 ws_path（源码契约守护）
"""
import inspect
from types import SimpleNamespace

import pytest

import app.runtime.tools as tools
from app.runtime.tools import ToolContext, _note_created, dispatch_tool


class _FakeResult:
    def scalars(self):
        return self

    def all(self):
        return []

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


def _ctx(**over):
    base = dict(db=_FakeDB(), user=SimpleNamespace(id=1), agent=SimpleNamespace(id=2))
    base.update(over)
    return ToolContext(**base)


# ---------- _note_created ----------

def test_note_created_fields():
    ctx = _ctx()
    _note_created(ctx, "/abs/reports/a.md", "reports/a.md", "text/markdown")
    assert ctx.created_files == [
        {"name": "a.md", "path": "/abs/reports/a.md", "ws_path": "reports/a.md", "mime": "text/markdown"}
    ]


def test_note_created_dedupe_by_ws_path():
    """一轮多次覆写同一文件只挂一条附件（以最后内容为准，附件只是入口）。"""
    ctx = _ctx()
    _note_created(ctx, "/abs/a.md", "a.md", "text/markdown")
    _note_created(ctx, "/abs/a.md", "a.md", "text/markdown")
    _note_created(ctx, "/abs/b.md", "b.md", "text/markdown")
    assert [f["ws_path"] for f in ctx.created_files] == ["a.md", "b.md"]


def test_note_created_cap_10():
    """单轮最多 10 条附件，防爆气泡（工作区面板里全量都在）。"""
    ctx = _ctx()
    for i in range(15):
        _note_created(ctx, f"/abs/f{i}.md", f"f{i}.md", "text/markdown")
    assert len(ctx.created_files) == 10


# ---------- workspace_write 收录 ----------

@pytest.mark.asyncio
async def test_workspace_write_notes_created_file(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "_workspace_root", lambda u, a: tmp_path)
    ctx = _ctx()
    r = await dispatch_tool(ctx, "workspace_write", {"path": "reports/周报.md", "content": "周报内容"})
    assert "已写入" in r
    assert (tmp_path / "reports" / "周报.md").read_text(encoding="utf-8") == "周报内容"
    assert len(ctx.created_files) == 1
    att = ctx.created_files[0]
    assert att["ws_path"] == "reports/周报.md" and att["name"] == "周报.md"
    assert att["mime"] and att["path"].endswith("周报.md")


# ---------- run_code 产物收录 ----------

@pytest.mark.asyncio
async def test_run_code_outputs_noted(monkeypatch, tmp_path):
    from app.runtime import sandbox_client

    async def _fake(code, files, timeout_s=None):
        return {
            "ok": True, "exit_code": 0, "stdout": "done", "stderr": "", "duration_ms": 1,
            "outputs": [
                {"path": "result.txt", "content_b64": "aGVsbG8=", "size": 5},
                {"path": "chart.png", "content_b64": "iVBORw0KGgo=", "size": 8},
            ],
        }

    monkeypatch.setattr(sandbox_client, "exec_code", _fake)
    monkeypatch.setattr(tools, "_workspace_root", lambda u, a: tmp_path)
    monkeypatch.setattr(tools, "_trace_root", lambda u, a: tmp_path / "traces")

    ctx = _ctx(allow_code_exec=True)
    r = await dispatch_tool(ctx, "run_code", {"code": "print('done')"})
    assert "产物已存回 workspace" in r
    by_path = {f["ws_path"]: f for f in ctx.created_files}
    assert set(by_path) == {"result.txt", "chart.png"}
    assert by_path["chart.png"]["mime"] == "image/png"
    assert by_path["result.txt"]["name"] == "result.txt"


# ---------- ws_path 序列化契约（done 事件 + 历史消息） ----------

def test_engine_done_event_carries_ws_path():
    from app.runtime import engine

    src = inspect.getsource(engine)
    assert '"ws_path"' in src or "'ws_path'" in src  # done 事件附件元素带 ws_path


def test_chat_history_serialization_carries_ws_path():
    import app.chat.router as chat_router

    src = inspect.getsource(chat_router)
    assert "ws_path" in src  # GET messages 历史附件带 ws_path（旧数据无此字段，前端兼容 None）
