"""AGENT-OPS 逻辑测试：健康度模型（health_of）+ 轮次事件统计（TurnCounters）。"""
import inspect
from datetime import UTC, datetime, timedelta

from app.chat.router import TurnCounters
from app.ops.router import (
    HEALTH_ERR_COUNT,
    HEALTH_ERR_RATE_RED,
    HEALTH_ERR_RATE_WARN,
    HEALTH_SLOW_MS,
    health_of,
)
from app.runtime.engine import TOOL_ERROR_PREFIX

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
RECENT = NOW - timedelta(hours=2)   # 7 天内有活动
STALE = NOW - timedelta(days=8)     # 超 7 天无活动


def h(turns, errors, avg_ms=None, last_active=RECENT):
    return health_of(turns=turns, errors=errors, avg_ms=avg_ms, last_active=last_active, now=NOW)


class TestHealth:
    def test_idle_when_no_activity_ever(self):
        assert h(0, 0, last_active=None) == "idle"

    def test_idle_when_stale_over_7d(self):
        assert h(10, 0, last_active=STALE) == "idle"

    def test_ok_when_no_turns_but_recent_activity(self):
        # 24h 窗口无轮次、但 7 天内有活动：没有错误可报
        assert h(0, 0) == "ok"

    def test_red_by_error_count(self):
        assert h(100, HEALTH_ERR_COUNT) == "error"  # 错误≥3 即红（哪怕错误率低）

    def test_red_by_error_rate(self):
        assert h(4, 1) == "error"  # 25% > 20%

    def test_red_boundary_rate_exactly_20pct_is_not_red(self):
        assert h(10, 2) == "warn"  # 恰 20% 不红（规则是 >20%），但 ≥5% 黄

    def test_warn_by_error_rate(self):
        assert h(20, 1) == "warn"  # 5%

    def test_warn_boundary_below_5pct(self):
        assert h(21, 1) == "ok"  # ~4.8% < 5%

    def test_warn_by_slow_avg(self):
        assert h(5, 0, avg_ms=HEALTH_SLOW_MS + 1) == "warn"

    def test_ok_normal(self):
        assert h(10, 0, avg_ms=5000) == "ok"

    def test_threshold_constants_sane(self):
        assert HEALTH_ERR_COUNT == 3
        assert HEALTH_ERR_RATE_WARN < HEALTH_ERR_RATE_RED
        assert HEALTH_SLOW_MS == 60_000


class TestTurnCounters:
    def test_round_meta_event_counted_not_forwarded(self):
        c = TurnCounters()
        assert c.observe({"_round": 1}) is False
        assert c.observe({"_round": 3}) is False
        assert c.observe({"_round": 2}) is False  # 乱序取最大
        assert c.rounds == 3

    def test_tool_calls_and_errors(self):
        c = TurnCounters()
        c.observe({"tool": {"status": "start", "name": "run_code"}})
        c.observe({"tool": {"status": "done", "result_preview": "ok result"}})
        c.observe({"tool": {"status": "start", "name": "web_fetch"}})
        c.observe({"tool": {"status": "done", "result_preview": f"{TOOL_ERROR_PREFIX}: boom"}})
        assert c.tool_calls == 2
        assert c.tool_errors == 1

    def test_tool_done_without_preview_not_error(self):
        c = TurnCounters()
        c.observe({"tool": {"status": "done"}})
        assert c.tool_errors == 0

    def test_error_event_recorded_and_truncated(self):
        c = TurnCounters()
        assert c.observe({"error": "x" * 600}) is True  # error 事件仍转发给客户端
        assert c.error_msg is not None and len(c.error_msg) == 500

    def test_normal_events_forwarded(self):
        c = TurnCounters()
        assert c.observe({"delta": "你好"}) is True
        assert c.observe({"done": {"message_id": 1}}) is True
        assert c.observe({"tool": {"status": "start", "name": "t"}}) is True
        assert c.error_msg is None


class TestRecentTurnsModelName:
    """M24.1：recent_turns 必须带 model_name——排障时要看到失败轮次用的哪个模型。"""

    def test_detail_endpoint_resolves_model_name(self):
        from app.ops import router as ops_router

        src = inspect.getsource(ops_router)
        assert '"model_name"' in src
        # model_id 为空的轮次不能进 IN 查询（否则生成 Model.id IN (NULL) 之类的脏 SQL）
        assert "if t.model_id" in src
        assert "display_name" in src
