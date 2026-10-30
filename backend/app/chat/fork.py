"""M6a 会话分叉（fork）：前缀截取 + tool 配对校验 + compaction 决策（纯函数，不碰 DB）。

设计要点：
- fork 点限定 role=user 的消息 ⇒ 前缀以 user 消息结尾；tool 响应永远在下一条 user
  消息之前产生（DB 不变量：assistant 带 tool_calls 必有紧随的连续 tool 配对），
  所以边界天然不落在 tool 配对中间（验证标准③由构造保证）。
- 防御脏数据：仍按 engine._repair_dangling_tool_calls 同款规则扫前缀，发现未配齐的
  assistant.tool_calls 时在 ForkPlan.dangling 里报告缺失的 tool_call_id——由端点在
  新会话里补占位 tool 行（与中断兜底同款的写侧修复），不做截断（截断会丢掉用户
  选定的 fork 点消息，反而破坏"从此处分叉"的语义）。
- compaction：fork 点之后仍有 compacted 消息 ⇒ 源会话的 compact_summary 覆盖了 fork
  点之后的内容（被污染），不能带过去；前缀副本全部"解压"（compacted=False，原消息
  从未物理删除）。否则前缀原样保留 compacted 标志并带上 summary，与源会话状态一致。
"""
from dataclasses import dataclass, field
import json

# 与 engine._repair_dangling_tool_calls 的合成 tool 文案保持一致（不含"中断/重做"字眼）
FORK_TOOL_PLACEHOLDER = "[系统] 该次工具调用无结果记录（任务可能已成功完成）。不要向用户提及，不要默认重做。"


@dataclass
class ForkPlan:
    prefix: list                              # 要复制的消息行（原顺序）
    copy_summary: bool = False                # 是否复制 compact_summary
    uncompact_prefix: bool = False            # True 时前缀副本的 compacted 一律改 False
    dangling: list[tuple[object, list[str]]] = field(default_factory=list)  # (assistant 行, 缺失的 tool_call_id)


def _tool_call_ids(m) -> list[str]:
    """assistant 消息的 tool_calls（JSON 字符串或已解析的 list）→ id 列表。"""
    tcs = getattr(m, "tool_calls", None)
    if not tcs:
        return []
    if isinstance(tcs, str):
        try:
            tcs = json.loads(tcs)
        except Exception:
            return []
    return [tc["id"] for tc in tcs if isinstance(tc, dict) and tc.get("id")]


def _find_dangling(msgs: list) -> list[tuple[object, list[str]]]:
    """前缀内未配齐 tool 响应的 assistant（配对规则与 engine._repair_dangling_tool_calls
    一致：只认紧随其后连续的 role=tool 消息）。"""
    out: list[tuple[object, list[str]]] = []
    i = 0
    while i < len(msgs):
        m = msgs[i]
        ids = _tool_call_ids(m) if getattr(m, "role", None) == "assistant" else []
        if ids:
            answered: set[str] = set()
            j = i + 1
            while j < len(msgs) and getattr(msgs[j], "role", None) == "tool":
                tcid = getattr(msgs[j], "tool_call_id", None)
                if tcid:
                    answered.add(tcid)
                j += 1
            missing = [t for t in ids if t not in answered]
            if missing:
                out.append((m, missing))
            i = j
        else:
            i += 1
    return out


def plan_fork(messages: list, fork_message_id: int, has_summary: bool) -> ForkPlan:
    """输入按 id ASC 排好的全量消息行，输出复制计划。消息不存在/不是 user 消息 → ValueError。"""
    idx = next((i for i, m in enumerate(messages) if m.id == fork_message_id), None)
    if idx is None:
        raise ValueError("消息不存在")
    if getattr(messages[idx], "role", None) != "user":
        raise ValueError("只能从用户消息分叉")

    prefix = list(messages[: idx + 1])
    dangling = _find_dangling(prefix)

    compacted_beyond = any(getattr(m, "compacted", False) for m in messages[idx + 1 :])
    compacted_within = any(getattr(m, "compacted", False) for m in prefix)
    if compacted_within and (compacted_beyond or not has_summary):
        # summary 被 fork 点之后的内容污染（或根本没有 summary）→ 不带 summary，前缀副本解压还原
        copy_summary, uncompact_prefix = False, True
    else:
        copy_summary, uncompact_prefix = compacted_within, False

    return ForkPlan(prefix=prefix, copy_summary=copy_summary, uncompact_prefix=uncompact_prefix, dangling=dangling)
