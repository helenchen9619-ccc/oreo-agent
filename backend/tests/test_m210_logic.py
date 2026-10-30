"""M2.10 逻辑测试：100MB 上限、workspace_delete 工具、技能总览/派发放开、tool 事件 id。"""
from types import SimpleNamespace

import pytest

from app.runtime.tools import TOOL_SCHEMAS, ToolContext, dispatch_tool


def test_workspace_max_size_100mb():
    from app.workspace.router import MAX_SIZE

    assert MAX_SIZE == 100 * 1024 * 1024


def test_workspace_delete_schema_and_prompt():
    names = {s["function"]["name"] for s in TOOL_SCHEMAS}
    assert "workspace_delete" in names
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "workspace_delete")
    assert "path" in schema["function"]["parameters"]["required"]
    from app.runtime.prompt import PLATFORM_LAYER

    assert "workspace_delete" in PLATFORM_LAYER


class _FakeScalars:
    def all(self):
        return []

    def scalar_one_or_none(self):
        return None


class _FakeResult:
    def scalars(self):
        return _FakeScalars()


class _FakeDB:
    async def execute(self, *a, **k):
        return _FakeResult()


def _ctx():
    return ToolContext(db=_FakeDB(), user=SimpleNamespace(id=1), agent=SimpleNamespace(id=1))


@pytest.mark.asyncio
async def test_workspace_delete_rejects_bad_path():
    r = await dispatch_tool(_ctx(), "workspace_delete", {"path": "../x"})
    assert "不合法" in r
    r = await dispatch_tool(_ctx(), "workspace_delete", {"path": ""})
    assert "不合法" in r


@pytest.mark.asyncio
async def test_workspace_delete_not_found():
    r = await dispatch_tool(_ctx(), "workspace_delete", {"path": "reports"})
    assert "未找到" in r


def test_engine_tool_events_carry_id():
    """engine.py 的 tool 事件必须带 id（前端按 id 匹配 start/done）。"""
    import inspect

    from app.runtime import engine

    src = inspect.getsource(engine)
    assert '"id": call_id' in src


def test_skills_admin_overview_includes_personal():
    """技能总览不过滤个人技能；M3.3 起 dispatch 拒绝个人技能（须先收录为平台技能）。"""
    import inspect

    from app.skills import router as skills_router

    list_src = inspect.getsource(skills_router.list_platform_skills)
    assert "owner_user_id.is_(None)" not in list_src
    assert "owner_name" in list_src
    dispatch_src = inspect.getsource(skills_router.dispatch_skill)
    assert "owner_user_id is not None" in dispatch_src  # 个人技能 → 400 先收录
    assert "收录" in dispatch_src
