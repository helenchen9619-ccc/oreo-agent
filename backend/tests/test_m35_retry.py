"""M35 模型调用 429/过载 应用层自动重试：分类 / 退避 / 流式重试安全边界 / compaction 加固 / 前端接线断言。

会话 1466 现场：Kimi K3 间歇过载，第 7 轮撞 429，SDK 默认 2 次重试没扛住，前 6 轮工作全废。
fake 范式复用 test_run_turn_history.py（monkeypatch engine.AsyncOpenAI + fake DB）。
"""
import asyncio
import inspect
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError, BadRequestError, InternalServerError, RateLimitError

from app.chat.router import TurnCounters
from app.runtime import engine
from app.runtime.engine import (
    RETRY_DELAYS,
    ModelBusyError,
    _create_with_retry,
    _is_retryable_llm_error,
    _retry_delay,
)

_REQ = httpx.Request("POST", "http://x/v1/chat/completions")


def _status_err(cls, code, msg="boom"):
    return cls(msg, response=httpx.Response(code, request=_REQ), body=None)


def _429():
    return _status_err(RateLimitError, 429, "Error code: 429 - {'error': {'message': 'The engine is currently overloaded, please try again later', 'type': 'engine_overloaded_error'}}")


# ---------- 分类与退避（纯函数） ----------

def test_retryable_classification():
    assert _is_retryable_llm_error(_429()) is True
    assert _is_retryable_llm_error(_status_err(InternalServerError, 500)) is True
    assert _is_retryable_llm_error(APIConnectionError(message="conn", request=_REQ)) is True
    assert _is_retryable_llm_error(APITimeoutError(request=_REQ)) is True
    assert _is_retryable_llm_error(_status_err(BadRequestError, 400)) is False
    assert _is_retryable_llm_error(_status_err(BadRequestError, 401)) is False
    assert _is_retryable_llm_error(ValueError("x")) is False


def test_retry_delay_jitter_bounds():
    for attempt, base in enumerate(RETRY_DELAYS):
        for _ in range(200):
            d = _retry_delay(attempt)
            assert base * 0.7 <= d <= base * 1.3
    # 越界 attempt 夹到最后一档
    for _ in range(50):
        assert RETRY_DELAYS[-1] * 0.7 <= _retry_delay(99) <= RETRY_DELAYS[-1] * 1.3


@pytest.mark.asyncio
async def test_create_with_retry_non_stream():
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _429()
        return "ok"

    monkey_sleeps = []
    orig_sleep = asyncio.sleep

    async def fake_sleep(d):
        monkey_sleeps.append(d)

    asyncio.sleep = fake_sleep
    try:
        assert await _create_with_retry(flaky) == "ok"
    finally:
        asyncio.sleep = orig_sleep
    assert calls == 3
    assert len(monkey_sleeps) == 2


def test_model_busy_error_dual_channel():
    e = ModelBusyError("Error code: 429 ...")
    assert str(e) == "模型服务繁忙，请稍后重发"
    assert "429" in e.raw


# ---------- run_turn 集成（fake 流） ----------

class _FakeStream:
    """按剧本吐 chunk；剧本项是异常则在迭代到它时抛出。"""

    def __init__(self, script):
        self._script = script

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for item in self._script:
            if isinstance(item, Exception):
                raise item
            yield item


def _chunk(content=None, reasoning=None, usage=None):
    delta = SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=None)
    choices = [] if usage is not None else [SimpleNamespace(delta=delta)]
    return SimpleNamespace(usage=usage, choices=choices)


def _ok_stream(text="你好"):
    return _FakeStream([
        _chunk(content=text),
        _chunk(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5)),
    ])


class _FakeCompletions:
    """script：每次 create 弹一项（异常→抛出；_FakeStream→返回）。callable 项用于按 kwargs 分流。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if callable(item):
            item = item(kwargs)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, messages):
        self._messages = messages
        self.added = []

    async def execute(self, stmt, *a, **k):
        return _FakeResult(list(self._messages))

    def add(self, row):
        self.added.append(row)
        if getattr(row, "id", None) is None:
            row.id = 9000 + len(self.added)

    async def flush(self):
        pass


def _msg(mid, role, content):
    return SimpleNamespace(
        id=mid, conversation_id=7, role=role, content=content, attachments=None,
        tool_calls=None, reasoning_content=None, tool_call_id=None, compacted=False,
    )


def _harness(monkeypatch, completions, messages=None, context_window=10**9):
    """返回 (db, 收集 run_turn 事件的协程工厂, sleeps 记录)。"""
    sleeps = []

    async def _no_notes(*a, **k):
        return []

    async def _no_prompt(*a, **k):
        return "system-prompt"

    async def _no_usage(*a, **k):
        return None

    async def _fake_sleep(d):
        sleeps.append(d)

    class _FakeClient:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=completions)

    monkeypatch.setattr(engine, "daily_notes_for_turn", _no_notes)
    monkeypatch.setattr(engine, "build_system_prompt", _no_prompt)
    monkeypatch.setattr(engine, "decrypt_secret", lambda v: "key")
    monkeypatch.setattr(engine, "record_usage", _no_usage)
    monkeypatch.setattr(engine, "AsyncOpenAI", _FakeClient)
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    db = _FakeDB(messages if messages is not None else [_msg(1, "user", "hi")])

    async def collect():
        events = []
        async for ev in engine.run_turn(
            db=db,
            user=SimpleNamespace(id=1, role="user"),
            agent=SimpleNamespace(
                id=2, is_master=False, max_loop_rounds=3,
                allow_code_exec=False, allow_web_search=False, allow_api_request=False,
                allow_feishu=False, allow_zhishu=False, allow_image_generate=False,
            ),
            conversation=SimpleNamespace(id=7, agent_id=2, compact_summary="", last_context_tokens=0, channel="web"),
            model=SimpleNamespace(model_id="kimi-k3", context_window=context_window, supports_vision=False),
            provider=SimpleNamespace(base_url="http://x", api_key_encrypted="e"),
        ):
            events.append(ev)
        return events

    return db, collect, sleeps


@pytest.mark.asyncio
async def test_429_twice_then_success(monkeypatch):
    comp = _FakeCompletions([_429(), _429(), _ok_stream()])
    _, collect, sleeps = _harness(monkeypatch, comp)
    events = await collect()
    assert len(comp.calls) == 3  # 1 + 2 次重试
    assert len(sleeps) == 2
    assert RETRY_DELAYS[0] * 0.7 <= sleeps[0] <= RETRY_DELAYS[0] * 1.3
    assert RETRY_DELAYS[1] * 0.7 <= sleeps[1] <= RETRY_DELAYS[1] * 1.3
    retrying = [e["retrying"] for e in events if "retrying" in e]
    assert [r["attempt"] for r in retrying] == [1, 2]
    assert all(r["max"] == 3 for r in retrying)
    assert any(e.get("delta") == "你好" for e in events)
    assert any("done" in e for e in events)


@pytest.mark.asyncio
async def test_no_retry_on_bad_request(monkeypatch):
    comp = _FakeCompletions([_status_err(BadRequestError, 400, "Error code: 400 - {'error': {'code': 'content_filter'}}")])
    _, collect, sleeps = _harness(monkeypatch, comp)
    events = await collect()
    assert len(comp.calls) == 1  # 400 不重试
    assert sleeps == []
    err = next(e["error"] for e in events if "error" in e)
    assert "内容安全审查" in err


@pytest.mark.asyncio
async def test_reasoning_heal_not_counted_as_m35_retry(monkeypatch):
    comp = _FakeCompletions([
        _status_err(BadRequestError, 400, "Error code: 400 - reasoning_content is required"),
        _ok_stream(),
    ])
    _, collect, sleeps = _harness(monkeypatch, comp)
    events = await collect()
    assert len(comp.calls) == 2  # M24.3 自愈重发 1 次
    assert sleeps == []  # 不走 M35 退避
    assert not any("retrying" in e for e in events)
    assert any("done" in e for e in events)


@pytest.mark.asyncio
async def test_exhaustion_raises_model_busy(monkeypatch):
    comp = _FakeCompletions([_429(), _429(), _429(), _429()])
    _, collect, sleeps = _harness(monkeypatch, comp)
    with pytest.raises(ModelBusyError) as ei:
        await collect()
    assert len(comp.calls) == 1 + len(RETRY_DELAYS)  # 1 + 3 次重试
    assert len(sleeps) == len(RETRY_DELAYS)
    assert str(ei.value) == "模型服务繁忙，请稍后重发"
    assert "overloaded" in ei.value.raw  # 原始错误留痕（进 turn_records.error_msg）


@pytest.mark.asyncio
async def test_midstream_after_content_no_retry(monkeypatch):
    comp = _FakeCompletions([_FakeStream([_chunk(content="部分输出"), APIConnectionError(message="reset", request=_REQ)])])
    _, collect, sleeps = _harness(monkeypatch, comp)
    with pytest.raises(APIConnectionError):
        await collect()
    assert len(comp.calls) == 1  # 已吐字 → 不重试（防重复输出）
    assert sleeps == []


@pytest.mark.asyncio
async def test_midstream_before_content_retries(monkeypatch):
    # 只有 reasoning 累积、零 content 零 tool_calls 时断流 → 安全重试
    comp = _FakeCompletions([
        _FakeStream([_chunk(reasoning="思考片段"), APIConnectionError(message="reset", request=_REQ)]),
        _ok_stream(),
    ])
    _, collect, sleeps = _harness(monkeypatch, comp)
    events = await collect()
    assert len(comp.calls) == 2
    assert len(sleeps) == 1
    assert [e.get("delta") for e in events if "delta" in e] == ["你好"]  # delta 只出现一次
    assert any("done" in e for e in events)


@pytest.mark.asyncio
async def test_compaction_429_does_not_kill_turn(monkeypatch):
    # 触发 compaction：>10 条活跃消息 + 超阈值（context_window 调极小）
    messages = [_msg(i, "user" if i % 2 else "assistant", f"消息{i} " * 20) for i in range(1, 14)]

    def _by_kind(kwargs):
        if kwargs.get("stream"):
            return _ok_stream()
        return _429()  # 摘要调用永远 429

    comp = _FakeCompletions([_by_kind] * 10)
    _, collect, sleeps = _harness(monkeypatch, comp, messages=messages, context_window=100)
    events = await collect()
    summary_calls = [c for c in comp.calls if not c.get("stream")]
    assert len(summary_calls) == 3  # 1 + _create_with_retry 的 2 次重试
    assert any("done" in e for e in events)  # 跳过压缩，turn 正常完成


# ---------- SSE 事件消费 ----------

def test_turn_counters_passthrough_retrying():
    c = TurnCounters()
    assert c.observe({"retrying": {"attempt": 1, "max": 3, "delay": 2.1}}) is True  # 转发给前端
    assert c.error_msg is None and c.rounds == 0  # 不污染指标


# ---------- 源码断言（守护关键不变式） ----------

def test_sdk_retries_disabled_and_m35_wired():
    src = inspect.getsource(engine)
    assert src.count("max_retries=0") == 2  # 主 client + compaction client 都收权
    assert "ModelBusyError" in src and '"retrying"' in src


def test_router_dual_channel_and_frontend_wired():
    from pathlib import Path

    import app.chat.router as chat_router
    rsrc = inspect.getsource(chat_router)
    assert "except ModelBusyError" in rsrc
    assert "e.raw" in rsrc  # turn_records 留原文
    root = Path(__file__).resolve().parents[2]
    assert "data.retrying" in (root / "frontend/src/api.js").read_text(encoding="utf-8")
    chat_page = (root / "frontend/src/pages/ChatPage.jsx").read_text(encoding="utf-8")
    assert "onRetrying" in chat_page and "正在自动重试" in chat_page
