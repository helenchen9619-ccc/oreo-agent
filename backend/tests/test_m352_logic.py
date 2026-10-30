"""M3.5.2/3 逻辑测试：墓碑中性化 + 中断疤痕检测（防"中断了，重试"自我模仿循环）。"""
from types import SimpleNamespace

from app.runtime.engine import (
    _interruption_scarred,
    _neutralize_interrupted_markers,
    _repair_dangling_tool_calls,
    _split_compact_boundary,
)


def test_tombstone_reworded():
    msgs = [
        {"role": "assistant", "content": "开始执行"},
        {"role": "tool", "tool_call_id": "c1", "content": "[已中断] 连接断开，本次工具执行未完成；如需结果请重新发起。"},
        {"role": "tool", "tool_call_id": "c2", "content": "执行完成（exit_code=0）"},
    ]
    out = _neutralize_interrupted_markers(msgs)
    assert "已中断] 连接断开" not in out[1]["content"]
    assert "不要向用户提及" in out[1]["content"]
    assert "重新" not in out[1]["content"]  # 不再含"重新发起/重新执行"等命令式字眼
    assert out[1]["tool_call_id"] == "c1"  # 配对字段保留
    assert out[2]["content"] == "执行完成（exit_code=0）"  # 正常 tool 消息不动
    assert out[0]["role"] == "assistant"


def test_legacy_repair_prefix_also_reworded():
    """旧版"[系统修复]"占位（历史残留）同样被中性化。"""
    msgs = [{"role": "tool", "tool_call_id": "c", "content": "[系统修复] 该次工具执行曾被中断，无结果记录；如需其结果请重新执行。"}]
    out = _neutralize_interrupted_markers(msgs)
    assert "重新执行" not in out[0]["content"]
    assert "不要向用户提及" in out[0]["content"]


def test_repair_synthesized_message_not_imperative():
    """悬挂 tool_calls 的合成消息不再含"重新执行"式诱导。"""
    msgs = [{"role": "assistant", "content": None, "tool_calls": [{"id": "a1"}]}]
    out = _repair_dangling_tool_calls(msgs)
    assert out[-1]["role"] == "tool"
    assert "重新执行" not in out[-1]["content"]
    assert "不要向用户提及" in out[-1]["content"]


def test_empty_and_no_tombstone_safe():
    assert _neutralize_interrupted_markers([]) == []
    msgs = [{"role": "user", "content": "hi"}, {"role": "tool", "tool_call_id": "c", "content": None}]
    out = _neutralize_interrupted_markers(msgs)
    assert out == msgs


def test_original_dict_not_mutated():
    original = {"role": "tool", "tool_call_id": "c1", "content": "[已中断] xxx"}
    _neutralize_interrupted_markers([original])
    assert original["content"] == "[已中断] xxx"  # 只改副本


# ---------- 疤痕检测 ----------

def test_scarred_by_tombstone():
    msgs = [{"role": "tool", "tool_call_id": "c", "content": "[已中断] 连接断开，本次工具执行未完成；如需结果请重新发起。"}]
    assert _interruption_scarred(msgs) is True


def test_scarred_by_own_utterance():
    """模型自己历史发言"中断了，重试…"也是疤痕（自我模仿锚点）。"""
    msgs = [
        {"role": "assistant", "content": "中断了，重试最终保存这步："},
        {"role": "tool", "tool_call_id": "c", "content": "执行完成（exit_code=0）"},
    ]
    assert _interruption_scarred(msgs) is True


def test_not_scarred_clean_history():
    msgs = [
        {"role": "user", "content": "中断了重试一下网络"},  # 用户的话不算（只检 assistant/tool）
        {"role": "assistant", "content": "已覆盖更新 workspace 原文件"},
        {"role": "tool", "tool_call_id": "c", "content": "执行完成（exit_code=0）"},
    ]
    assert _interruption_scarred(msgs) is False
    assert _interruption_scarred([]) is False


# ---------- M24.4：孤儿 tool 消息（压缩切断 tool 链的存量带毒会话） ----------

def test_orphan_tool_at_start_dropped():
    msgs = [
        {"role": "tool", "tool_call_id": "c1", "content": "孤儿"},
        {"role": "user", "content": "你好"},
    ]
    out = _repair_dangling_tool_calls(msgs)
    assert [m["role"] for m in out] == ["user"]


def test_orphan_tool_after_plain_assistant_dropped():
    """assistant 无 tool_calls，紧随的 tool 是孤儿（其父 assistant 已被压缩带走）。"""
    msgs = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "普通回复"},
        {"role": "tool", "tool_call_id": "c9", "content": "孤儿结果"},
        {"role": "user", "content": "u2"},
    ]
    out = _repair_dangling_tool_calls(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]


def test_valid_tool_pair_untouched():
    msgs = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "结果"},
        {"role": "assistant", "content": "完成"},
    ]
    out = _repair_dangling_tool_calls(msgs)
    assert [m["role"] for m in out] == ["assistant", "tool", "assistant"]


def test_mixed_orphan_and_valid_chain():
    msgs = [
        {"role": "tool", "tool_call_id": "orphan", "content": "x"},  # 孤儿
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    out = _repair_dangling_tool_calls(msgs)
    assert [m.get("tool_call_id") for m in out if m["role"] == "tool"] == ["c1"]


# ---------- M24.4：压缩边界不得切断 tool 链 ----------

def _m(role):
    return SimpleNamespace(role=role)


def test_split_boundary_moves_orphan_tools_to_old():
    active = [_m("user"), _m("assistant"), _m("tool"), _m("tool"), _m("user"), _m("assistant")]
    # keep=3 → 裸切片 recent=[tool, user, assistant]，开头的 tool 孤儿须并入 old
    old, recent = _split_compact_boundary(active, keep=3)
    assert [m.role for m in recent] == ["user", "assistant"]
    assert [m.role for m in old] == ["user", "assistant", "tool", "tool"]


def test_split_boundary_clean_cut_unchanged():
    active = [_m("user"), _m("assistant"), _m("user"), _m("assistant")]
    old, recent = _split_compact_boundary(active, keep=2)
    assert [m.role for m in old] == ["user", "assistant"]
    assert [m.role for m in recent] == ["user", "assistant"]


def test_split_boundary_recent_emptied_is_allowed():
    """极端：recent 全是孤儿 tool（assistant 挂 ≥keep 个调用）→ 全部并入 old，recent 空。"""
    active = [_m("assistant")] + [_m("tool")] * 3
    old, recent = _split_compact_boundary(active, keep=3)
    assert recent == []
    assert len(old) == 4
