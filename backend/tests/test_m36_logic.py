"""M3.6 逻辑测试：_prune_old_tool_results（session pruning，学 OpenClaw——旧 tool 输出在发给模型的副本里截断）。"""
from app.runtime.engine import (
    _PRUNE_HEAD,
    _PRUNE_KEEP_RECENT_TOOL,
    _neutralize_interrupted_markers,
    _prune_old_tool_results,
)

LONG = "x" * 2000
SHORT = "已覆盖更新 workspace 原文件：菜品数据/a.parquet"


def _tool(tid: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tid, "content": content}


def test_old_long_tool_results_truncated_recent_kept():
    # 8 条长 tool 输出：前 2 条超出保留窗口应被截断，最近 6 条完整
    msgs = [_tool(f"c{i}", f"{LONG}{i}") for i in range(8)]
    out = _prune_old_tool_results(msgs)
    for i, m in enumerate(out):
        if i < 8 - _PRUNE_KEEP_RECENT_TOOL:
            assert "已省略" in m["content"]
            assert len(m["content"]) < _PRUNE_HEAD + 100
        else:
            assert m["content"] == f"{LONG}{i}"  # 最近 6 条原样
    assert out[0]["tool_call_id"] == "c0"  # 配对字段保留


def test_old_short_results_kept_intact():
    """短的旧结果（状态结论）是证据不是噪音，原样保留。"""
    msgs = [_tool("old_short", SHORT)] + [_tool(f"c{i}", LONG) for i in range(7)]
    out = _prune_old_tool_results(msgs)
    assert out[0]["content"] == SHORT
    assert "已省略" not in out[0]["content"]


def test_non_tool_and_edge_cases_safe():
    assert _prune_old_tool_results([]) == []
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": LONG}, {"role": "tool", "tool_call_id": "c", "content": None}]
    out = _prune_old_tool_results(msgs)
    assert out[1]["content"] == LONG  # assistant 长消息不动（只修剪 tool 角色）
    assert out[2]["content"] is None  # content 为 None 不炸


def test_original_dict_not_mutated():
    original = _tool("c", LONG)
    _prune_old_tool_results([original] + [_tool(f"c{i}", LONG) for i in range(6)])
    assert original["content"] == LONG  # 只改副本


def test_composes_with_tombstone_neutralization():
    """接线顺序：先中性化再修剪——墓碑（短文本）不被截断，长噪音被截断。"""
    msgs = [
        _tool("tomb", "[已中断] 连接断开，本次工具执行未完成；如需结果请重新发起。"),
        _tool("noise", LONG),
    ] + [_tool(f"c{i}", LONG) for i in range(6)]
    out = _prune_old_tool_results(_neutralize_interrupted_markers(msgs))
    assert "不要向用户提及" in out[0]["content"] and "已省略" not in out[0]["content"]  # 墓碑已中性化且不被截
    assert "已省略" in out[1]["content"]  # 长噪音被截
