"""M4c 逻辑测试：_maybe_compact 的 force 模式（手动压缩按钮后端）。"""
from types import SimpleNamespace

import pytest

from app.runtime import engine


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
    def __init__(self, messages):
        self._messages = messages
        self.added = []

    async def execute(self, *a, **k):
        return _FakeResult(self._messages)

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        pass


class _FakeCompletions:
    async def create(self, model=None, messages=None, **kw):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="压缩后的摘要"))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
        )


class _FakeClient:
    def __init__(self, **kw):
        self.chat = SimpleNamespace(completions=_FakeCompletions())


@pytest.fixture
def _mock_llm(monkeypatch):
    monkeypatch.setattr(engine, "AsyncOpenAI", _FakeClient)
    monkeypatch.setattr(engine, "decrypt_secret", lambda s: "key")

    async def _noop_record(*a, **k):
        return None

    monkeypatch.setattr(engine, "record_usage", _noop_record)


def _conv():
    return SimpleNamespace(id=1, agent_id=1, compact_summary="")


def _msgs(n):
    return [
        SimpleNamespace(id=i + 1, role="user" if i % 2 == 0 else "assistant", content=f"消息{i}", attachments=None, compacted=False)
        for i in range(n)
    ]


def _kwargs(db, conv):
    return dict(
        db=db,
        user=SimpleNamespace(id=1),
        conversation=conv,
        model=SimpleNamespace(id=1, model_id="m"),
        provider=SimpleNamespace(base_url="http://x", api_key_encrypted="e"),
        context_window=10**9,  # 巨大窗口：不 force 时必然不达阈值
    )


@pytest.mark.asyncio
async def test_compact_below_threshold_no_force(_mock_llm):
    """未达 70% 阈值且不 force → 不压缩。"""
    db, conv = _FakeDB(_msgs(20)), _conv()
    r = await engine._maybe_compact(**_kwargs(db, conv), force=False)
    assert r["compacted"] is False and r["context_tokens"] > 0
    assert all(not m.compacted for m in db._messages)


@pytest.mark.asyncio
async def test_compact_force_skips_threshold(_mock_llm):
    """force=True 跳过阈值：旧消息标记 compacted、写入摘要、返回 summary_len。"""
    msgs = _msgs(12)  # > COMPACT_KEEP_RECENT(10) → old = 前 2 条
    db, conv = _FakeDB(msgs), _conv()
    r = await engine._maybe_compact(**_kwargs(db, conv), force=True)
    assert r["compacted"] is True
    assert r["summary_len"] == len("压缩后的摘要")
    assert conv.compact_summary == "压缩后的摘要"
    assert [m.compacted for m in msgs] == [True, True] + [False] * 10


@pytest.mark.asyncio
async def test_compact_force_too_few_messages(_mock_llm):
    """消息 ≤ 保留数时 force 也不压（压了就没上下文了）。"""
    db, conv = _FakeDB(_msgs(5)), _conv()
    r = await engine._maybe_compact(**_kwargs(db, conv), force=True)
    assert r["compacted"] is False and r["reason"] == "消息太少，无需压缩"
    assert all(not m.compacted for m in db._messages)
