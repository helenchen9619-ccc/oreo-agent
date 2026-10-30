"""M37 逻辑测试：定时任务笔记注入隔离——cron 会话不注入今昨笔记（会话 1554 跨任务暗渠定案）。

缝函数 daily_notes_for_turn：cron 短路返回 []（不查库）；web/feishu 透传 recent_daily_notes。
_FakeDB 用查询队列，范式抄 test_m36_folder_ops.py。
"""
from pathlib import Path

import pytest

import app.runtime.memory as mem


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, *queries_rows):
        self._queue = [list(r) for r in queries_rows]
        self.queries = 0

    async def execute(self, *a, **k):
        self.queries += 1
        return _FakeResult(self._queue.pop(0) if self._queue else [])


@pytest.mark.asyncio
async def test_cron_channel_skips_notes(monkeypatch):
    """cron 会话：短路返回空，且不得触碰 recent_daily_notes（断暗渠的关键是不查库）。"""
    async def _boom(*a, **k):
        raise AssertionError("cron 会话不应查询笔记")

    monkeypatch.setattr(mem, "recent_daily_notes", _boom)
    db = _FakeDB(["不该被读到的笔记"])
    r = await mem.daily_notes_for_turn(db, agent_id=1, user_id=2, channel="cron")
    assert r == []
    assert db.queries == 0


@pytest.mark.asyncio
async def test_web_and_feishu_channels_pass_through():
    for channel in ("web", "feishu"):
        db = _FakeDB(["笔记甲", "笔记乙"])
        r = await mem.daily_notes_for_turn(db, agent_id=1, user_id=2, channel=channel)
        assert r == ["笔记甲", "笔记乙"], channel
        assert db.queries == 1


def test_m37_wiring():
    root = Path(__file__).resolve().parents[1]
    engine = (root / "app/runtime/engine.py").read_text(encoding="utf-8")
    assert "channel=conversation.channel" in engine
    assert "daily_notes_for_turn" in engine
    prompt = (root / "app/runtime/prompt.py").read_text(encoding="utf-8")
    assert "cron 会话不注入笔记" in prompt
