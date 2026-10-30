"""长会话历史窗口回归：LIMIT 100 必须取最近消息，而非最早 100 条。

1072 现场：第 100 条是 assistant tool_call，第 101 条真实 tool 结果、
第 102 条最新用户消息都被旧 ASC+LIMIT 截掉，模型每轮从同一旧现场复读。
"""
import json
from types import SimpleNamespace

import pytest

from app.runtime import engine


class _StopAfterCapture(Exception):
    pass


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalars(self._rows)


class _FakeDB:
    """按 SQL 的方向和 LIMIT 真取数，避免固定返回尾部让旧查询误通过。"""

    def __init__(self, messages):
        self._messages = messages

    async def execute(self, stmt, *args, **kwargs):
        sql = str(stmt).upper()
        descending = "ORDER BY MESSAGES.ID DESC" in sql
        rows = sorted(self._messages, key=lambda m: m.id, reverse=descending)
        if " LIMIT " in f" {sql} ":
            rows = rows[:100]
        return _FakeResult(rows)

    async def flush(self):
        pass


def _message(mid, role, content, **extra):
    base = dict(
        id=mid,
        conversation_id=7,
        role=role,
        content=content,
        attachments=None,
        tool_calls=None,
        reasoning_content=None,
        tool_call_id=None,
        compacted=False,
    )
    base.update(extra)
    return SimpleNamespace(**base)


def _history_102():
    rows = [
        _message(i, "user" if i % 2 else "assistant", f"old-{i}")
        for i in range(1, 100)
    ]
    rows.extend([
        _message(
            100,
            "assistant",
            "准备写入脚本",
            tool_calls=json.dumps([{
                "id": "recent-call",
                "type": "function",
                "function": {"name": "workspace_write", "arguments": "{}"},
            }]),
        ),
        _message(101, "tool", "recent-tool-result", tool_call_id="recent-call"),
        _message(102, "user", "latest-user"),
    ])
    return rows


@pytest.mark.asyncio
async def test_run_turn_sends_latest_100_in_chronological_order(monkeypatch):
    db = _FakeDB(_history_102())
    captured = {}

    async def _no_notes(*args, **kwargs):
        return []

    async def _no_prompt(*args, **kwargs):
        return "system-prompt"

    class _FakeCompletions:
        async def create(self, *, messages, **kwargs):
            captured["messages"] = messages
            raise _StopAfterCapture

    class _FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=_FakeCompletions())

    monkeypatch.setattr(engine, "daily_notes_for_turn", _no_notes)
    monkeypatch.setattr(engine, "build_system_prompt", _no_prompt)
    monkeypatch.setattr(engine, "decrypt_secret", lambda value: "key")
    monkeypatch.setattr(engine, "AsyncOpenAI", _FakeClient)

    conversation = SimpleNamespace(
        id=7, agent_id=2, compact_summary="", last_context_tokens=0, channel="web",
    )
    agent = SimpleNamespace(
        id=2, is_master=False, max_loop_rounds=3,
        allow_code_exec=False, allow_web_search=False, allow_api_request=False,
        allow_feishu=False, allow_zhishu=False, allow_image_generate=False,
    )
    with pytest.raises(_StopAfterCapture):
        async for _ in engine.run_turn(
            db=db,
            user=SimpleNamespace(id=1, role="user"),
            agent=agent,
            conversation=conversation,
            model=SimpleNamespace(
                model_id="m", context_window=10**9, supports_vision=False,
            ),
            provider=SimpleNamespace(base_url="http://x", api_key_encrypted="e"),
        ):
            pass

    sent = captured["messages"]
    history = [m for m in sent if m["role"] != "system"]
    assert len(history) == 100
    assert history[0]["content"] == "old-3"  # 最近 100 条 = id 3~102
    assert "old-1" not in [m["content"] for m in history]
    assert [m["role"] for m in history[-3:]] == ["assistant", "tool", "user"]
    assert history[-2]["tool_call_id"] == "recent-call"
    assert history[-2]["content"] == "recent-tool-result"
    assert history[-1]["content"] == "latest-user"
    assert not any("无结果记录" in str(m["content"]) for m in history)
    assert sent[-2] is history[-1]
    assert sent[-1]["role"] == "system"
    assert sent[-1]["content"].startswith("【当前任务】")
