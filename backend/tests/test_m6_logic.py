"""M6 逻辑测试：会话分叉 plan_fork 纯函数 + 系统提示词瘦身（M6b）的防双写回归。"""
import json
from types import SimpleNamespace

import pytest

from app.chat.fork import FORK_TOOL_PLACEHOLDER, plan_fork
from app.runtime.prompt import (
    API_REQUEST_LAYER,
    CODE_EXEC_LAYER,
    PLATFORM_LAYER,
    WEB_SEARCH_LAYER,
)
from app.runtime.tools import API_TOOL_SCHEMAS, CODE_TOOL_SCHEMAS, TOOL_SCHEMAS


def _m(mid, role, content="", tool_calls=None, tool_call_id=None, compacted=False, attachments=None):
    return SimpleNamespace(
        id=mid, role=role, content=content, tool_calls=tool_calls,
        tool_call_id=tool_call_id, compacted=compacted, attachments=attachments,
        input_tokens=None, output_tokens=None, trace_id=None, name=None,
    )


def _tc(*ids):
    return json.dumps([{"id": i, "type": "function", "function": {"name": "workspace_list", "arguments": "{}"}} for i in ids])


# ---------- plan_fork：前缀截取与边界 ----------

def test_fork_basic_prefix():
    msgs = [_m(1, "user", "问1"), _m(2, "assistant", "答1"), _m(3, "user", "问2"), _m(4, "assistant", "答2"), _m(5, "user", "问3")]
    plan = plan_fork(msgs, 3, has_summary=False)
    assert [m.id for m in plan.prefix] == [1, 2, 3]
    assert plan.dangling == [] and plan.copy_summary is False and plan.uncompact_prefix is False


def test_fork_first_message():
    msgs = [_m(1, "user", "问1"), _m(2, "assistant", "答1")]
    plan = plan_fork(msgs, 1, has_summary=False)
    assert len(plan.prefix) == 1 and plan.prefix[0].id == 1


def test_fork_rejects_non_user_message():
    msgs = [_m(1, "user", "问"), _m(2, "assistant", "答"), _m(3, "tool", "结果", tool_call_id="t1")]
    with pytest.raises(ValueError, match="只能从用户消息分叉"):
        plan_fork(msgs, 2, has_summary=False)
    with pytest.raises(ValueError, match="只能从用户消息分叉"):
        plan_fork(msgs, 3, has_summary=False)


def test_fork_rejects_unknown_message():
    with pytest.raises(ValueError, match="消息不存在"):
        plan_fork([_m(1, "user", "问")], 999, has_summary=False)


def test_fork_tool_pairing_intact():
    """含工具调用的前缀：assistant(tool_calls) 与其 tool 响应成对进入新会话（验证标准①）。"""
    msgs = [
        _m(1, "user", "开始"),
        _m(2, "assistant", "", tool_calls=_tc("t1", "t2")),
        _m(3, "tool", "r1", tool_call_id="t1"),
        _m(4, "tool", "r2", tool_call_id="t2"),
        _m(5, "assistant", "完成"),
        _m(6, "user", "从这里分叉"),
        _m(7, "assistant", "分叉点之后的回答"),
    ]
    plan = plan_fork(msgs, 6, has_summary=False)
    assert [m.id for m in plan.prefix] == [1, 2, 3, 4, 5, 6]
    assert plan.dangling == []  # 配对完整，无需兜底


def test_fork_dirty_dangling_reported_not_truncated():
    """脏数据（assistant 带 tool_calls 但无配对 tool）：不截断前缀（否则丢 fork 点消息），
    报告缺失的 tool_call_id 由端点补占位行（验证标准③的防御面）。"""
    msgs = [_m(1, "user", "问"), _m(2, "assistant", "", tool_calls=_tc("t1")), _m(3, "user", "从这里分叉")]
    plan = plan_fork(msgs, 3, has_summary=False)
    assert [m.id for m in plan.prefix] == [1, 2, 3]  # 前缀完整，fork 点消息不丢
    assert len(plan.dangling) == 1 and plan.dangling[0][1] == ["t1"]
    assert plan.dangling[0][0].id == 2
    assert FORK_TOOL_PLACEHOLDER.startswith("[系统]")


def test_fork_compacted_within_prefix_copies_summary():
    """压缩全部发生在 fork 点之前：summary 未污染 → 复制 summary、保留 compacted 标志。"""
    msgs = [
        _m(1, "user", "旧问", compacted=True),
        _m(2, "assistant", "旧答", compacted=True),
        _m(3, "user", "从这里分叉"),
        _m(4, "assistant", "答"),
    ]
    plan = plan_fork(msgs, 3, has_summary=True)
    assert plan.copy_summary is True and plan.uncompact_prefix is False


def test_fork_compacted_beyond_drops_summary():
    """fork 点之后仍有 compacted 消息：summary 被点之后内容污染 → 不带 summary，前缀副本解压。"""
    msgs = [
        _m(1, "user", "旧问", compacted=True),
        _m(2, "user", "从这里分叉"),
        _m(3, "assistant", "旧答", compacted=True),  # 极端脏序：点之后还有 compacted
    ]
    plan = plan_fork(msgs, 2, has_summary=True)
    assert plan.copy_summary is False and plan.uncompact_prefix is True


def test_fork_compacted_without_summary_uncompacts():
    """前缀含 compacted 但源会话无 summary（异常态）：解压还原，不丢历史。"""
    msgs = [_m(1, "user", "旧问", compacted=True), _m(2, "user", "从这里分叉")]
    plan = plan_fork(msgs, 2, has_summary=False)
    assert plan.copy_summary is False and plan.uncompact_prefix is True


def test_fork_fields_passthrough():
    """plan.prefix 原引用透传（端点据此复制 tool_calls/attachments 等字段）。"""
    m = _m(1, "user", "看图", attachments='[{"name":"a.png","path":"x","mime":"image/png"}]')
    plan = plan_fork([m], 1, has_summary=False)
    assert plan.prefix[0] is m and plan.prefix[0].attachments == m.attachments


# ---------- M6b：提示词瘦身回归 ----------

def _desc(schemas, name):
    return next(s["function"]["description"] for s in schemas if s["function"]["name"] == name)


def test_platform_layer_clause_count():
    """PLATFORM_LAYER 合并为 ≤7 条（瘦身目标：条款数降 ≥30%）。"""
    n = sum(1 for line in PLATFORM_LAYER.splitlines() if line.startswith("- "))
    assert n <= 7, f"PLATFORM_LAYER 现有 {n} 条"


def test_code_exec_layer_slim_details_in_description():
    """沙箱细节（rapidocr/parquet/output/向量化）只留 run_code description，层文本只留行为导向。"""
    assert len(CODE_EXEC_LAYER) < 400
    for kw in ("RapidOCR()", "./output/", "searchsorted", "read_only=True"):
        assert kw not in CODE_EXEC_LAYER
    desc = _desc(CODE_TOOL_SCHEMAS, "run_code")
    for kw in ("RapidOCR()", "./output/", "searchsorted", "read_only=True"):
        assert kw in desc


def test_no_delete_confirm_contradiction():
    """C1：PLATFORM_LAYER 与 workspace_delete description 口径一致（明确指示=已确认，不二次询问）。"""
    desc = _desc(TOOL_SCHEMAS, "workspace_delete")
    assert "必须先向用户确认" not in desc
    assert "不要二次询问" not in PLATFORM_LAYER or "已明确指示" in PLATFORM_LAYER
    assert "明确指示" in desc and "明确指示" in PLATFORM_LAYER


def test_sentence_uniqueness_across_layer_and_description():
    """防双写回归：关键纪律句在 层+description 合计只出现一次。"""
    pairs = [
        (CODE_EXEC_LAYER, _desc(CODE_TOOL_SCHEMAS, "run_code"), "比反复解析 xlsx 快几十倍"),
        (API_REQUEST_LAYER, _desc(API_TOOL_SCHEMAS, "api_request"), "不要并发轰炸"),
        (API_REQUEST_LAYER, _desc(API_TOOL_SCHEMAS, "api_request"), "分页"),
    ]
    for layer, desc, kw in pairs:
        total = layer.count(kw) + desc.count(kw)
        assert total == 1, f"「{kw}」出现 {total} 次（双写）"


def test_web_search_layer_keeps_output_discipline():
    """WEB_SEARCH_LAYER 删触发条件双写，保留输出纪律（来源 URL / 收敛轮次）。"""
    assert "来源名称和 URL" in WEB_SEARCH_LAYER
    assert "2-4 轮" in WEB_SEARCH_LAYER
    assert "不要凭记忆硬答" not in WEB_SEARCH_LAYER  # 已下沉到 web_search description


def test_platform_layer_priority_clause():
    """瘦身后的防误读护栏：明确指令优先于一般纪律。"""
    assert "明确指令优先" in PLATFORM_LAYER
