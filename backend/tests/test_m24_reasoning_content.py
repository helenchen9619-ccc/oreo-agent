"""M24.0 热修回归：思考模型（Kimi thinking）reasoning_content 必须随历史回传。

事故背景：reasoning_content 不落库不回传 → 第二轮起供应商 400
「The reasoning_content in the thinking mode must be passed back」→ 数字人全灭。
本测试锁住三个点：有值必带、无值不带（非思考模型不受影响）、tool 消息不受影响。
"""
from types import SimpleNamespace

from app.runtime.engine import _row_to_llm_message


def _row(**kw):
    defaults = dict(
        role="assistant",
        content="你好",
        tool_calls=None,
        reasoning_content=None,
        attachments=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_assistant_with_reasoning_passed_back():
    row = _row(reasoning_content="先想三步再答")
    msg = _row_to_llm_message(row)
    assert msg["reasoning_content"] == "先想三步再答"
    assert msg["content"] == "你好"


def test_assistant_tool_call_round_keeps_reasoning():
    row = _row(
        content="",
        tool_calls='[{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]',
        reasoning_content="需要调工具",
    )
    msg = _row_to_llm_message(row)
    assert msg["reasoning_content"] == "需要调工具"
    assert msg["tool_calls"][0]["id"] == "c1"


def test_no_reasoning_key_absent():
    """无值时不带 key——供应商对未知字段可能校验，不能让普通模型平白带上。"""
    msg = _row_to_llm_message(_row())
    assert "reasoning_content" not in msg


def test_empty_string_reasoning_key_absent():
    msg = _row_to_llm_message(_row(reasoning_content=""))
    assert "reasoning_content" not in msg


def test_tool_message_unaffected():
    row = _row(role="tool", content="结果", tool_call_id="c1", reasoning_content="x")
    msg = _row_to_llm_message(row)
    assert msg == {"role": "tool", "tool_call_id": "c1", "content": "结果"}


def test_engine_persists_reasoning_on_assistant_rows():
    """引擎落库与回传链路：reasoning_parts 累积、两处 assistant 落库、回传注入。"""
    import inspect

    from app.runtime import engine

    src = inspect.getsource(engine)
    assert "reasoning_parts" in src
    assert 'getattr(delta, "reasoning_content", None)' in src
    assert "reasoning_content=reasoning or None" in src  # 两处 assistant Message 落库
    assert 'asst_msg["reasoning_content"] = reasoning' in src  # 循环内回传下一轮


class TestFillMissingReasoning:
    """M24.3 自愈：存量历史/fork 丢列会话缺 reasoning_content 时补空串（供应商只做存在性校验）。"""

    def test_fills_only_assistant_rows_missing_key(self):
        from app.runtime.engine import _fill_missing_reasoning

        msgs = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a1"},
            {"role": "assistant", "content": "a2", "tool_calls": []},
            {"role": "tool", "tool_call_id": "c1", "content": "t"},
            {"role": "assistant", "content": "a3", "reasoning_content": "已有推理"},
        ]
        n = _fill_missing_reasoning(msgs)
        assert n == 2
        assert msgs[2]["reasoning_content"] == ""
        assert msgs[3]["reasoning_content"] == ""
        assert msgs[5]["reasoning_content"] == "已有推理"  # 已有值不被覆盖
        assert "reasoning_content" not in msgs[0]  # system/user/tool 不动
        assert "reasoning_content" not in msgs[1]
        assert "reasoning_content" not in msgs[4]

    def test_nothing_to_fill_returns_zero(self):
        from app.runtime.engine import _fill_missing_reasoning

        assert _fill_missing_reasoning([{"role": "assistant", "reasoning_content": "x"}]) == 0
        assert _fill_missing_reasoning([]) == 0

    def test_engine_has_one_shot_heal_retry(self):
        """400 含 reasoning_content 时自愈重试一次（reasoning_healed 防死循环）。"""
        import inspect

        src = inspect.getsource(__import__("app.runtime.engine", fromlist=["x"]))
        assert "reasoning_healed" in src
        assert '"reasoning_content" in raw and not reasoning_healed' in src
        # 自愈分支成功后不得落到 raise：400 处理必须是完整 if/elif/else 链
        assert src.count("elif \"context length\"") == 1

    def test_fork_copies_reasoning_content(self):
        """M24.3：会话分叉逐列复制消息，必须带 reasoning_content，否则分叉会话永久带毒。"""
        import inspect

        from app.chat import router as chat_router

        assert "reasoning_content=m.reasoning_content" in inspect.getsource(chat_router)
