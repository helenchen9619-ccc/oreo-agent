"""M3.10 逻辑测试：schedule_* 工具（schema / 校验 / 上限 / validate-then-swap / 归属隔离）。"""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.models.scheduler import ScheduledTask
from app.runtime.tools import (
    SCHED_TOOL_NAMES,
    SCHED_TOOL_SCHEMAS,
    TOOL_SCHEMAS,
    ToolContext,
    _schedule_create,
    _schedule_delete,
    _schedule_list,
    _schedule_update,
)


class _FakeResult:
    def __init__(self, scalar_one=None, one_or_none=None, all_rows=None):
        self._scalar_one = scalar_one
        self._one_or_none = one_or_none
        self._all = all_rows if all_rows is not None else []

    def scalar_one(self):
        return self._scalar_one

    def scalar_one_or_none(self):
        return self._one_or_none

    def scalars(self):
        return SimpleNamespace(all=lambda: self._all)


class _FakeDB:
    """按调用顺序弹出预设结果（schedule 工具每个函数查询次数固定）。"""

    def __init__(self, results=()):
        self.results = list(results)
        self.added = []
        self.commits = 0

    async def execute(self, *a, **k):
        return self.results.pop(0)

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        self.commits += 1


def _ctx(db, default_model_id=3) -> ToolContext:
    return ToolContext(db=db, user=SimpleNamespace(id=1, role="member"), agent=SimpleNamespace(id=1, default_model_id=default_model_id))


def _task(**kw) -> SimpleNamespace:
    base = dict(
        id=5, name="每日简报", prompt="收集新闻", cron_expr="30 13 * * *", timezone="Asia/Shanghai",
        enabled=True, next_run_at=datetime(2026, 8, 15, 5, 30, tzinfo=UTC), last_status=None, last_error=None, run_count=0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class TestSchemas:
    def test_valid_and_required(self):
        by_name = {s["function"]["name"]: s["function"]["parameters"] for s in SCHED_TOOL_SCHEMAS}
        assert by_name.keys() == {"schedule_create", "schedule_list", "schedule_update", "schedule_delete"}
        assert by_name["schedule_create"]["required"] == ["name", "prompt", "cron_expr"]
        assert by_name["schedule_update"]["required"] == ["task_id"]
        assert by_name["schedule_delete"]["required"] == ["task_id"]

    def test_not_in_base_schemas(self):
        base_names = {s["function"]["name"] for s in TOOL_SCHEMAS}
        assert not (SCHED_TOOL_NAMES & base_names)


class TestCreate:
    @pytest.mark.asyncio
    async def test_rejects_bad_cron_and_tz(self):
        r = await _schedule_create(_ctx(_FakeDB()), {"name": "x", "prompt": "y", "cron_expr": "bad"})
        assert "cron 表达式无效" in r
        r = await _schedule_create(_ctx(_FakeDB()), {"name": "x", "prompt": "y", "cron_expr": "0 9 * * *", "timezone": "Beijing"})
        assert "时区无效" in r

    @pytest.mark.asyncio
    async def test_creates_with_future_next_run(self):
        db = _FakeDB([_FakeResult(scalar_one=0)])
        r = await _schedule_create(_ctx(db), {"name": "每日简报", "prompt": "收集新闻", "cron_expr": "30 13 * * *"})
        assert "已创建" in r and "下次触发" in r
        task = next(x for x in db.added if isinstance(x, ScheduledTask))
        assert task.user_id == 1 and task.agent_id == 1 and task.enabled is True
        assert task.next_run_at is not None and task.next_run_at.tzinfo is not None
        assert task.next_run_at > datetime.now(UTC)
        assert db.commits == 1

    @pytest.mark.asyncio
    async def test_rejects_when_agent_has_no_default_model(self):
        """数字人没配默认模型时拒建并给指引（任务到点必失败，不造"必死"任务）。"""
        db = _FakeDB()
        r = await _schedule_create(_ctx(db, default_model_id=None), {"name": "x", "prompt": "y", "cron_expr": "0 9 * * *"})
        assert "默认模型" in r
        assert not [x for x in db.added if isinstance(x, ScheduledTask)]

    @pytest.mark.asyncio
    async def test_cap_at_10(self):
        db = _FakeDB([_FakeResult(scalar_one=10)])
        r = await _schedule_create(_ctx(db), {"name": "x", "prompt": "y", "cron_expr": "0 9 * * *"})
        assert "上限" in r
        assert not [x for x in db.added if isinstance(x, ScheduledTask)]


class TestUpdate:
    @pytest.mark.asyncio
    async def test_not_found_hides_existence(self):
        db = _FakeDB([_FakeResult(one_or_none=None)])
        r = await _schedule_update(_ctx(db), {"task_id": 999, "enabled": False})
        assert "不存在" in r

    @pytest.mark.asyncio
    async def test_invalid_cron_rejected_before_mutation(self):
        t = _task()
        db = _FakeDB([_FakeResult(one_or_none=t)])
        r = await _schedule_update(_ctx(db), {"task_id": 5, "cron_expr": "nope"})
        assert "cron 表达式无效" in r
        assert t.cron_expr == "30 13 * * *"  # validate-then-swap：原值未被碰

    @pytest.mark.asyncio
    async def test_disable_clears_next_run(self):
        t = _task()
        db = _FakeDB([_FakeResult(one_or_none=t)])
        r = await _schedule_update(_ctx(db), {"task_id": 5, "enabled": False})
        assert "停用" in r
        assert t.enabled is False and t.next_run_at is None

    @pytest.mark.asyncio
    async def test_enable_recomputes_next_run(self):
        t = _task(enabled=False, next_run_at=None)
        db = _FakeDB([_FakeResult(one_or_none=t)])
        r = await _schedule_update(_ctx(db), {"task_id": 5, "enabled": True})
        assert "启用" in r
        assert t.next_run_at is not None and t.next_run_at > datetime.now(UTC)


class TestDelete:
    @pytest.mark.asyncio
    async def test_soft_delete(self):
        t = _task()
        db = _FakeDB([_FakeResult(one_or_none=t)])
        r = await _schedule_delete(_ctx(db), {"task_id": 5})
        assert "已删除" in r
        assert t.deleted_at is not None and db.commits == 1

    @pytest.mark.asyncio
    async def test_not_found(self):
        db = _FakeDB([_FakeResult(one_or_none=None)])
        r = await _schedule_delete(_ctx(db), {"task_id": 999})
        assert "不存在" in r


class TestList:
    @pytest.mark.asyncio
    async def test_formats_rows(self):
        db = _FakeDB([_FakeResult(all_rows=[_task(), _task(id=6, name="周报", enabled=False, next_run_at=None)])])
        out = await _schedule_list(_ctx(db))
        assert "#5 每日简报" in out and "30 13 * * *" in out and "启用" in out
        assert "#6 周报" in out and "停用" in out

    @pytest.mark.asyncio
    async def test_empty(self):
        db = _FakeDB([_FakeResult(all_rows=[])])
        assert "没有定时任务" in await _schedule_list(_ctx(db))
