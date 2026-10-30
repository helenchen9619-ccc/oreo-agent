"""运行时引擎：上下文组装 → compaction → agent loop（流式 + 工具调用）→ 落库。

SSE 事件（dict，由 router 序列化）：
- {"delta": str}                      文本增量
- {"tool": {...status: start/done}}   工具调用过程
- {"done": {message_id, context_tokens, context_window}}
- {"error": str}
- {"retrying": {attempt, max, delay}}   模型 429/过载自动重试中（M35）
- {"_round": int}                     内部元事件：router 统计轮次用，不转发给前端
"""
import asyncio
import base64
import json
import random
import re
import time
from collections.abc import AsyncGenerator
from pathlib import Path

import anyio
from openai import APIConnectionError, APIStatusError, AsyncOpenAI, BadRequestError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret
from app.gateway.service import ChatResult, record_usage
from app.models.agent import Agent
from app.models.chat import Conversation, Message
from app.models.gateway import Model, ModelProvider
from app.models.user import User
from app.runtime.memory import daily_notes_for_turn
from app.runtime.prompt import build_system_prompt
from app.runtime.tools import (
    API_TOOL_SCHEMAS,
    CODE_TOOL_SCHEMAS,
    FEISHU_TOOL_SCHEMAS,
    IMG_TOOL_SCHEMAS,
    MASTER_TOOL_SCHEMAS,
    SCHED_TOOL_SCHEMAS,
    TOOL_SCHEMAS,
    WEB_TOOL_SCHEMAS,
    ZHISHU_TOOL_SCHEMAS,
    ToolContext,
    dispatch_tool,
)

COMPACT_KEEP_RECENT = 10   # compaction 后保留的最近消息数
COMPACT_THRESHOLD = 0.7    # 超过上下文窗口 70% 触发压缩
IMAGE_TOKEN_EST = 1000     # 每张图片的粗估 token（用于进度条/压缩阈值）
TOOL_ERROR_PREFIX = "工具执行出错"  # 工具异常回给模型的文案前缀；chat/router 据此统计 tool_errors

# M35：429/过载/5xx/连接错误的应用层重试（SDK max_retries 已收权为 0，统一走这里）
RETRY_DELAYS = (2.0, 8.0, 20.0)  # 每次模型调用（round）的重试间隔（秒），共 3 次
RETRY_JITTER = 0.3               # ±30% 抖动，防多会话同步重试惊群


class ModelBusyError(Exception):
    """M35：可重试模型错误重试耗尽。str=用户友好文案（进 SSE），.raw=原始错误（进 turn_records.error_msg）。"""

    def __init__(self, raw: str):
        super().__init__("模型服务繁忙，请稍后重发")
        self.raw = raw


def _is_retryable_llm_error(e: Exception) -> bool:
    """429（含 engine_overloaded）、5xx、连接/超时 → True；400（内容审查/超长）等 → False。"""
    if isinstance(e, APIConnectionError):  # APITimeoutError 是其子类
        return True
    if isinstance(e, APIStatusError):
        return e.status_code == 429 or e.status_code >= 500
    return False


def _retry_delay(attempt: int) -> float:
    """attempt 0-based → RETRY_DELAYS[attempt] × (1 ± jitter)，越界夹到最后一档。"""
    base = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
    return base * (1 + random.uniform(-RETRY_JITTER, RETRY_JITTER))


async def _create_with_retry(call, *, max_retries: int = 2):
    """M35：非流式模型调用（compaction 摘要）的重试封装。无 SSE 通道，失败只留服务端日志。"""
    for attempt in range(max_retries + 1):
        try:
            return await call()
        except Exception as e:
            if not _is_retryable_llm_error(e) or attempt >= max_retries:
                raise
            delay = _retry_delay(attempt)
            print(f"[retry] 模型摘要调用失败（{type(e).__name__}: {e!s}），{delay:.1f}s 后第 {attempt + 1} 次重试")
            await asyncio.sleep(delay)


def estimate_tokens(text: str) -> int:
    """粗估（中文偏多场景）：2 字符 ≈ 1 token。用于进度条和压缩阈值，不要求精确。"""
    return max(len(text) // 2, 1)


def _estimate_content(content) -> int:
    """content 可能是字符串或 vision 数组（[{type:text},{type:image_url}]）。"""
    if isinstance(content, list):
        total = 0
        for part in content:
            if part.get("type") == "text":
                total += estimate_tokens(part.get("text") or "")
            elif part.get("type") == "image_url":
                total += IMAGE_TOKEN_EST
        return max(total, 1)
    return estimate_tokens(str(content or ""))


def _msg_attachments(row: Message) -> list[dict]:
    if not row.attachments:
        return []
    try:
        return json.loads(row.attachments)
    except (json.JSONDecodeError, TypeError):
        return []


def _image_data_url(att: dict) -> str | None:
    """从落盘的附件文件还原 data URL（模型只能吃 data URL 或公网 URL）。"""
    try:
        raw = Path(att["path"]).read_bytes()
    except (OSError, KeyError):
        return None
    return f"data:{att.get('mime') or 'image/png'};base64,{base64.b64encode(raw).decode()}"


def _row_to_llm_message(row: Message, vision_ok: bool = False) -> dict:
    if row.role == "tool":
        return {"role": "tool", "tool_call_id": row.tool_call_id, "content": row.content}
    atts = _msg_attachments(row) if row.role == "user" else []
    if atts:
        if vision_ok:
            parts: list[dict] = [{"type": "text", "text": row.content or ""}]
            n_img = 0
            for a in atts:
                url = _image_data_url(a)
                if url:
                    parts.append({"type": "image_url", "image_url": {"url": url}})
                    n_img += 1
            if n_img:
                # M24.6：显式锚定——图片随消息内联直达、模型直接看图即可；
                # 否则模型按「用户上传的图片在工作区」的提示词经验去 workspace 翻找，找不到还误导用户
                parts[0]["text"] = (
                    f"[系统提示：用户随本条消息直接附了 {n_img} 张图片，图片内容就在本条消息里，"
                    f"直接看图回答即可；图片不在工作区，无需用工具查找。]\n{row.content or ''}"
                )
            else:
                # 附件文件读取失败（被清理/丢失）：明示而非静默吞掉，防模型瞎猜
                parts[0]["text"] = f"{row.content or ''}\n[系统提示：用户附了 {len(atts)} 张图片，但文件读取失败；请告知用户重新发送图片。]"
            msg: dict = {"role": row.role, "content": parts}
        else:
            # 当前模型不支持图片：降级为文本占位，避免 API 报错
            names = "、".join(a.get("name", "image") for a in atts)
            msg = {"role": row.role, "content": f"{row.content or ''}\n[用户附了图片：{names}；当前模型不支持图片输入]"}
    else:
        msg = {"role": row.role, "content": row.content or ""}
    if row.tool_calls:
        msg["tool_calls"] = json.loads(row.tool_calls)
    # M24.0：思考模型（Kimi thinking 等）的 assistant 消息必须把 reasoning_content 原样回传，
    # 否则供应商报 400「reasoning_content must be passed back」。只在该行确实有值时带，
    # 非思考模型的消息不受影响（其他供应商对未知字段通常忽略）。
    if getattr(row, "reasoning_content", None):
        msg["reasoning_content"] = row.reasoning_content
    return msg


def _repair_dangling_tool_calls(msgs: list[dict]) -> list[dict]:
    """自愈：assistant 的 tool_calls 若没有配对 tool 响应（历史中断残留），补合成 tool 消息。
    模型 API 硬性要求每个 tool_call_id 都有响应，否则整轮 400——只修发给模型的副本，不动 DB。
    M24.4 反方向：孤儿 tool 消息（其 assistant.tool_calls 已被压缩带走/丢失）直接丢弃——
    供应商同样硬性要求 tool 必须紧跟配对 assistant，否则 400。"""
    out: list[dict] = []
    i = 0
    while i < len(msgs):
        m = msgs[i]
        if m.get("role") == "tool":
            # 走到主循环顶部的 tool = 未被任何前面的 assistant.tool_calls 消费 = 孤儿 → 丢弃
            i += 1
            continue
        out.append(m)
        if m.get("role") == "assistant" and m.get("tool_calls"):
            expected = [tc.get("id") for tc in m["tool_calls"] if tc.get("id")]
            answered: set[str] = set()
            j = i + 1
            while j < len(msgs) and msgs[j].get("role") == "tool":
                answered.add(msgs[j].get("tool_call_id"))
                out.append(msgs[j])
                j += 1
            for tid in expected:
                if tid not in answered:
                    out.append({
                        "role": "tool",
                        "tool_call_id": tid,
                        # 措辞要点：不含"中断/重新执行"等命令式字眼，避免模型道歉并重做已完成步骤
                        "content": "[系统] 该次工具调用无结果记录（任务可能已成功完成）。不要向用户提及，不要默认重做。",
                    })
            i = j
        else:
            i += 1
    return out


_INTERRUPTED_MARKS = ("[已中断]", "[系统修复]")  # 历史残留的两种占位前缀（后者为旧版修复文案）
_INTERRUPTED_NEUTRAL = (
    "[系统] 该工具调用的结果记录缺失（连接断开或手动停止所致，任务可能已成功完成）。"
    "不要向用户提及此异常、不要道歉；先用 workspace_list 核实工作区现状，"
    "已完成的结果直接采用，只补确实缺失的步骤。"
)


def _fill_missing_reasoning(msgs: list[dict]) -> int:
    """M24.3：给缺 reasoning_content 的 assistant 消息补空串，返回填补数。

    自愈场景：修复前（M24.0 之前）的存量历史、fork 丢列的会话——思考模型供应商
    （Kimi/DeepSeek thinking + tool calls）只校验字段存在性，空串即通过。
    已有值不动；只在供应商明确报 reasoning_content 400 后才调用（非思考模型零影响）。"""
    n = 0
    for m in msgs:
        if m.get("role") == "assistant" and "reasoning_content" not in m:
            m["reasoning_content"] = ""
            n += 1
    return n
_RESUME_NOTE = (
    "【重要】本会话曾有工具调用无结果记录（连接断开/手动停止所致），相关步骤很可能已成功完成"
    "（以历史中“已覆盖更新/已生成”等工具结果为准）。请遵守：1) 不要再说“中断了/重试”，"
    "不要为此道歉；2) 不要重做已完成的步骤；3) 优先直接回答用户当前的问题。"
)


def _neutralize_interrupted_markers(msgs: list[dict]) -> list[dict]:
    """中性化 DB 里的"[已中断]"墓碑（服务重启/用户停止给中断 turn 补的占位 tool 响应）：
    原措辞"如需结果请重新发起"会让模型每轮都认定任务被打断、反复重做已完成的步骤。
    只改发给模型的副本，DB 里的真实历史不动（与 _repair_dangling_tool_calls 同哲学）。"""
    out: list[dict] = []
    for m in msgs:
        if m.get("role") == "tool" and str(m.get("content") or "").startswith(_INTERRUPTED_MARKS):
            m = {**m, "content": _INTERRUPTED_NEUTRAL}
        out.append(m)
    return out


def _interruption_scarred(msgs: list[dict]) -> bool:
    """历史是否带"中断疤痕"（须在中性化之前检测）：占位 tool 消息，或模型自己说过"中断…重试"。
    有疤痕时 run_turn 注入 _RESUME_NOTE，打断模型模仿自己旧发言形成的"中断了，重试"循环。"""
    for m in msgs:
        content = str(m.get("content") or "")
        if m.get("role") == "tool" and content.startswith(_INTERRUPTED_MARKS):
            return True
        if m.get("role") == "assistant" and "中断" in content and "重试" in content:
            return True
    return False


_PRUNE_KEEP_RECENT_TOOL = 6   # 最近 N 条 tool 结果保持完整（当前任务需要细节）
_PRUNE_MIN_LEN = 800          # 更短的旧结果原样保留（多为状态结论，是证据不是噪音）
_PRUNE_HEAD = 300             # 截断后保留的头部字符数
_PRUNE_MARK = "\n…[历史工具输出已省略，如需细节可重新执行查询]"


def _prune_old_tool_results(msgs: list[dict]) -> list[dict]:
    """旧 tool 输出在发给模型的副本里截断（学 OpenClaw session pruning，DB 不动）：
    ETL 验证日志等大段 stdout 全量重发，会让旧任务剧本的锚定强度随轮次累积、淹没用户最新指令。
    只截断旧的长输出；短结论（"已覆盖更新 workspace 原文件…"）原样保留——证据留下，噪音清掉。"""
    tool_idx = [i for i, m in enumerate(msgs) if m.get("role") == "tool"]
    keep = set(tool_idx[-_PRUNE_KEEP_RECENT_TOOL:])
    out: list[dict] = []
    for i, m in enumerate(msgs):
        if m.get("role") == "tool" and i not in keep:
            content = str(m.get("content") or "")
            if len(content) > _PRUNE_MIN_LEN:
                m = {**m, "content": content[:_PRUNE_HEAD] + _PRUNE_MARK}
        out.append(m)
    return out


# M21.1 当前任务注记：会话 167 教训——用户换题三次+质问一次，模型四轮都在复读上一话题
# （自我模仿道歉剧本 + 自写 circle 教训进 MEMORY 反噬 + 旧上下文质量碾压）。
# LLM 无显式权重旋钮：把任务指令贴在最后一条用户消息之后（离生成最近）= 最高显著性的等效实现。
_REFS_RE = re.compile(r"【我引用了工作区文件：(.+?)(?:，请用|】)")  # 前端引用前缀是固定格式，可确定解析
_META_COMPLAINT_RE = re.compile(r"没(有)?回答|答非所问|上一个(问题|话题)|跑题")


def _msg_text(content) -> str:
    """user 消息 content 可能是 vision 数组，抽文本部分。"""
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return str(content or "")


def _append_current_task_note(msgs: list[dict]) -> list[dict]:
    """每轮在最后一条用户消息之后追加一条当前任务注记（system 角色，离生成位置最近）。
    三档：基础提醒 → 解析到引用文件就逐个点名要求先读 → 检出「没回答我」类元抱怨时升级措辞。
    组装时追加一次即可；turn 内 tool 循环增长后它留在当轮上下文里继续生效。"""
    if not msgs or msgs[-1].get("role") != "user":
        return msgs
    text = _msg_text(msgs[-1].get("content"))
    if _META_COMPLAINT_RE.search(text):
        note = ("【当前任务】用户指出你没有回答他的问题。停止沿用上一轮的思路和辩解，逐字重读上面最后一条"
                "用户消息——它是当前唯一任务（若引用了新文件，先 workspace_read 读取），直接回答它。")
    else:
        note = ("【当前任务】上面最后一条用户消息是当前唯一任务：若它与上文话题无关，立刻放下旧话题，"
                "直接回答这条消息，不复读之前的结论或辩解。")
    m = _REFS_RE.search(text)
    if m:
        files = "、".join(f.strip() for f in m.group(1).split("、") if f.strip())
        if files:
            note += f" 本条消息引用了工作区文件：{files}——先用 workspace_read 读取它们再回答。"
    return [*msgs, {"role": "system", "content": note}]


def _split_compact_boundary(active: list, keep: int) -> tuple[list, list]:
    """M24.4：压缩切分（old=进摘要, recent=保留），切点不得落在 tool 链中间。

    recent 若以 tool 开头，说明其 assistant(tool_calls) 被切进了 old——孤儿 tool 留在
    活跃历史里会让模型 API 必 400（'tool must be a response to preceding tool_calls'）。
    把 recent 开头连续的 tool 行并入 old 一起压缩掉即可（它们的上下文本就在 old 里）。"""
    old, recent = list(active[:-keep]), list(active[-keep:])
    while recent and recent[0].role == "tool":
        old.append(recent.pop(0))
    return old, recent


async def _maybe_compact(
    db: AsyncSession,
    *,
    user: User,
    conversation: Conversation,
    model: Model,
    provider: ModelProvider,
    context_window: int,
    force: bool = False,
) -> dict:
    """接近上下文上限时把旧消息摘要为 compact_summary（保留 tool 配对边界的最近 N 条不动）。
    force=True（用户手动压缩）跳过 70% 阈值，但消息太少（≤保留数）仍不压。
    返回 {"compacted": bool, "reason": str, "context_tokens": int, "summary_len": int}。"""
    rows = (
        (
            await db.execute(
                select(Message).where(Message.conversation_id == conversation.id).order_by(Message.id.asc())
            )
        )
        .scalars()
        .all()
    )
    active = [m for m in rows if not m.compacted]
    total = sum(estimate_tokens(m.content or "") + IMAGE_TOKEN_EST * len(_msg_attachments(m)) for m in active) + estimate_tokens(conversation.compact_summary or "")
    if len(active) <= COMPACT_KEEP_RECENT:
        return {"compacted": False, "reason": "消息太少，无需压缩", "context_tokens": total, "summary_len": 0}
    if not force and total < context_window * COMPACT_THRESHOLD:
        return {"compacted": False, "reason": "未达到压缩阈值", "context_tokens": total, "summary_len": 0}
    old, _recent = _split_compact_boundary(active, COMPACT_KEEP_RECENT)  # M24.4：切点不得切断 tool 链
    digest_source = "\n".join(
        f"{m.role}: {(m.content or '')[:500]}{'[附图]' if _msg_attachments(m) else ''}" for m in old if m.role in ("user", "assistant")
    )
    summary_prompt = [
        {"role": "system", "content": "你是对话压缩器。把给定的对话历史压缩成一段保留关键信息（事实、决定、待办、用户偏好）的中文摘要，不超过 500 字。"},
        {"role": "user", "content": f"此前的摘要：{conversation.compact_summary or '（无）'}\n\n待压缩的对话：\n{digest_source}"},
    ]
    client = AsyncOpenAI(base_url=provider.base_url, api_key=decrypt_secret(provider.api_key_encrypted), timeout=120, max_retries=0)  # M35：重试收权应用层
    started = time.monotonic()
    resp = await _create_with_retry(lambda: client.chat.completions.create(model=model.model_id, messages=summary_prompt))
    usage = resp.usage
    await record_usage(
        db,
        user=user,
        model=model,
        result=ChatResult(
            content="",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            cached_tokens=0,
            latency_ms=int((time.monotonic() - started) * 1000),
        ),
        conversation_id=conversation.id,
        agent_id=conversation.agent_id,
    )
    conversation.compact_summary = resp.choices[0].message.content or ""
    for m in old:
        m.compacted = True
    await db.flush()
    # context_tokens 返回压缩后的估值（摘要 + 保留的最近消息），前端直接拿去刷新用量条
    post = estimate_tokens(conversation.compact_summary) + sum(
        estimate_tokens(m.content or "") + IMAGE_TOKEN_EST * len(_msg_attachments(m)) for m in _recent
    )
    return {"compacted": True, "reason": "", "context_tokens": post, "summary_len": len(conversation.compact_summary)}


async def run_turn(
    db: AsyncSession,
    *,
    user: User,
    agent: Agent,
    conversation: Conversation,
    model: Model,
    provider: ModelProvider,
) -> AsyncGenerator[dict, None]:
    """执行一轮对话。调用前需把 user 消息（含 attachments）落库——历史与当前消息统一从 DB 组装。"""
    context_window = model.context_window or 32768
    vision_ok = bool(model.supports_vision)

    try:
        await _maybe_compact(db, user=user, conversation=conversation, model=model, provider=provider, context_window=context_window)
    except Exception as e:
        if not _is_retryable_llm_error(e):
            raise
        # M35：压缩重试也耗尽 → 跳过本轮压缩直接跑（真超上下文由 400 context-length 友好提示兜底），不让 429 杀掉整个 turn
        print(f"[compact] 摘要重试耗尽，本轮跳过压缩: {e!s}")

    is_admin_master = bool(agent.is_master and user.role == "admin")
    allow_code_exec = bool(agent.allow_code_exec)
    allow_web_search = bool(agent.allow_web_search)
    allow_api_request = bool(getattr(agent, "allow_api_request", False))
    allow_feishu = bool(getattr(agent, "allow_feishu", False))
    allow_zhishu = bool(getattr(agent, "allow_zhishu", False))
    allow_image_generate = bool(getattr(agent, "allow_image_generate", False))
    daily_notes = await daily_notes_for_turn(db, agent_id=agent.id, user_id=user.id, channel=conversation.channel)
    system_prompt = await build_system_prompt(db, agent=agent, user=user, daily_notes=daily_notes, is_master=is_admin_master, allow_code_exec=allow_code_exec, allow_web_search=allow_web_search, allow_api_request=allow_api_request, allow_feishu=allow_feishu, allow_zhishu=allow_zhishu, allow_image_generate=allow_image_generate)

    rows = (
        (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id, Message.compacted.is_(False))
                .order_by(Message.id.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    rows = list(reversed(rows))  # 最近 100 条恢复时间正序；升序 LIMIT 会把当前用户消息关在窗外
    llm_messages: list[dict] = [{"role": "system", "content": system_prompt}]
    if conversation.compact_summary:
        llm_messages.append({"role": "system", "content": f"早前对话摘要（被压缩的历史）：{conversation.compact_summary}"})
    llm_messages.extend(_row_to_llm_message(m, vision_ok) for m in rows if m.role != "system")
    llm_messages = _repair_dangling_tool_calls(llm_messages)  # 历史中断残留自愈（否则整轮 400）
    scarred = _interruption_scarred(llm_messages)  # 须在中性化之前检测（中性化会抹掉识别前缀）
    llm_messages = _neutralize_interrupted_markers(llm_messages)  # "[已中断]"墓碑中性化（防模型反复重做已完成步骤）
    llm_messages = _prune_old_tool_results(llm_messages)  # 旧的长 tool 输出截断（session pruning，防旧剧本锚定）
    if scarred:
        # 有疤痕（含模型自己说过"中断了，重试"）：注入条件系统提示，打断自我模仿循环；正常会话零影响
        note_pos = 2 if conversation.compact_summary else 1
        llm_messages.insert(note_pos, {"role": "system", "content": _RESUME_NOTE})
    llm_messages = _append_current_task_note(llm_messages)  # M21.1：当前任务注记贴最后（防换题被旧话题锚定）

    conversation.last_context_tokens = sum(_estimate_content(m.get("content")) for m in llm_messages)
    await db.flush()

    client = AsyncOpenAI(base_url=provider.base_url, api_key=decrypt_secret(provider.api_key_encrypted), timeout=180, max_retries=0)  # M35：重试收权应用层
    ctx = ToolContext(db=db, user=user, agent=agent, is_admin_master=is_admin_master, allow_code_exec=allow_code_exec, allow_web_search=allow_web_search, allow_api_request=allow_api_request, allow_feishu=allow_feishu, allow_zhishu=allow_zhishu, allow_image_generate=allow_image_generate)
    tool_schemas = list(TOOL_SCHEMAS) + SCHED_TOOL_SCHEMAS
    if allow_code_exec:
        tool_schemas += CODE_TOOL_SCHEMAS
    if allow_web_search:
        tool_schemas += WEB_TOOL_SCHEMAS
    if allow_api_request:
        tool_schemas += API_TOOL_SCHEMAS
    if allow_feishu:
        tool_schemas += FEISHU_TOOL_SCHEMAS
    if allow_zhishu:
        tool_schemas += ZHISHU_TOOL_SCHEMAS
    if allow_image_generate:
        tool_schemas += IMG_TOOL_SCHEMAS
    if is_admin_master:
        tool_schemas += MASTER_TOOL_SCHEMAS

    tool_err_counts: dict[str, int] = {}  # M3.17：本轮内各工具的出错次数（重复错误归因提示用）
    reasoning_healed = False  # M24.3：本 turn 是否已做过一次 reasoning 空串自愈重试（只自愈一次，防死循环）

    for _round in range(agent.max_loop_rounds):
        # 内部元事件：chat/router 统计轮次用（turn_records），不进 SSE 队列
        yield {"_round": _round + 1}
        started = time.monotonic()
        attempt = 0  # M35：本 round 已重试次数（发起失败与流中断共享同一预算）
        while True:
            try:
                try:
                    stream = await client.chat.completions.create(
                        model=model.model_id,
                        messages=llm_messages,
                        tools=tool_schemas,
                        stream=True,
                        stream_options={"include_usage": True},
                    )
                except BadRequestError as e:
                    # 供应商 400 的两类常见原因翻译成人话（否则用户只看到 "Error code: 400" 一脸懵）。
                    # 此时此前轮次的工具结果已正常落库，直接结束本 turn 即可，下轮对话可继续。
                    raw = str(e)
                    if "reasoning_content" in raw and not reasoning_healed:
                        # M24.3：历史里残留修复前/fork 丢列的无 reasoning assistant 消息 →
                        # 供应商只做存在性校验，补空串自愈后原样重发一次；再失败走原异常路径（无回归）。
                        reasoning_healed = True
                        _fill_missing_reasoning(llm_messages)
                        stream = await client.chat.completions.create(
                            model=model.model_id,
                            messages=llm_messages,
                            tools=tool_schemas,
                            stream=True,
                            stream_options={"include_usage": True},
                        )
                    elif "Content Exists Risk" in raw or "content_filter" in raw or "data_inspection_failed" in raw:
                        yield {"error": "本轮请求被模型供应商的内容安全审查拦截：发给模型的内容里（文件内容/搜索结果/生成文本）有触发风控词的文字。可换个问法、排查是哪个文件触发的，或切换其他模型重试。"}
                        return
                    elif "context length" in raw or "context_length" in raw:
                        yield {"error": "超出模型实际上下文长度。请点输入框上方的「压缩」按钮压缩上下文，或开新会话继续。"}
                        return
                    else:
                        raise
            except Exception as e:
                # M35：发起即失败（429/5xx/连接/超时，含 heal 重发撞 429）→ 安全重试（此轮尚未吐出任何内容）
                if not _is_retryable_llm_error(e):
                    raise
                if attempt >= len(RETRY_DELAYS):
                    raise ModelBusyError(str(e)[:300]) from e
                delay = _retry_delay(attempt)
                attempt += 1
                print(f"[retry] 模型发起失败（{type(e).__name__}: {e!s}），{delay:.1f}s 后第 {attempt}/{len(RETRY_DELAYS)} 次重试")
                yield {"retrying": {"attempt": attempt, "max": len(RETRY_DELAYS), "delay": round(delay, 1)}}
                await asyncio.sleep(delay)
                continue
            # 发起成功。M35：累积态每次尝试重置，重试不得混入上一次尝试的残片
            content_parts: list[str] = []
            reasoning_parts: list[str] = []  # M24.0：思考模型的 reasoning_content 流式累积
            tool_calls: dict[int, dict] = {}
            in_tokens = out_tokens = 0
            try:
                async for chunk in stream:
                    if chunk.usage is not None:
                        in_tokens = chunk.usage.prompt_tokens or 0
                        out_tokens = chunk.usage.completion_tokens or 0
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    # openai SDK 对未知字段宽松透传，getattr 兼容不返回该字段的供应商
                    rc = getattr(delta, "reasoning_content", None)
                    if rc:
                        reasoning_parts.append(rc)
                    if delta.content:
                        content_parts.append(delta.content)
                        yield {"delta": delta.content}
                    for tc in delta.tool_calls or []:
                        slot = tool_calls.setdefault(tc.index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function and tc.function.name:
                            slot["function"]["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            slot["function"]["arguments"] += tc.function.arguments
            except Exception as e:
                # M35 断流：只有本轮零输出（无 delta 无 tool_calls）才重试，防重复吐字；
                # reasoning_parts 不算——思考内容从不 yield 给前端，重试只是重生成思考
                if _is_retryable_llm_error(e) and not content_parts and not tool_calls:
                    if attempt >= len(RETRY_DELAYS):
                        raise ModelBusyError(str(e)[:300]) from e
                    delay = _retry_delay(attempt)
                    attempt += 1
                    print(f"[retry] 模型流中断（{type(e).__name__}: {e!s}），{delay:.1f}s 后第 {attempt}/{len(RETRY_DELAYS)} 次重试")
                    yield {"retrying": {"attempt": attempt, "max": len(RETRY_DELAYS), "delay": round(delay, 1)}}
                    await asyncio.sleep(delay)
                    continue
                raise  # 已吐出部分内容 → 按原行为上抛（前端保留部分气泡 + toast）
            break

        await record_usage(
            db,
            user=user,
            model=model,
            result=ChatResult(
                content="",
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                cached_tokens=0,
                latency_ms=int((time.monotonic() - started) * 1000),
            ),
            conversation_id=conversation.id,
            agent_id=conversation.agent_id,
        )
        await db.flush()

        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)
        calls = [tool_calls[i] for i in sorted(tool_calls)]

        if not calls:
            assistant = Message(
                conversation_id=conversation.id,
                role="assistant",
                content=content,
                reasoning_content=reasoning or None,
            )
            if ctx.created_files:
                # M13a：本轮工具落盘的产物（生成的图片）挂到消息附件，前端气泡内直接展示
                assistant.attachments = json.dumps(ctx.created_files, ensure_ascii=False)
            db.add(assistant)
            await db.flush()
            conversation.last_context_tokens = sum(_estimate_content(m.get("content")) for m in llm_messages) + estimate_tokens(content)
            yield {
                "done": {
                    "message_id": assistant.id,
                    "context_tokens": conversation.last_context_tokens,
                    "context_window": context_window,
                    "attachments": [
                        {"idx": i, "name": f["name"], "mime": f["mime"], "ws_path": f.get("ws_path")}
                        for i, f in enumerate(ctx.created_files)
                    ]
                    or None,
                }
            }
            return

        # 有工具调用：落 assistant(带 tool_calls) → 逐个执行落 tool 消息 → 继续循环
        assistant = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=content,
            tool_calls=json.dumps(calls, ensure_ascii=False),
            reasoning_content=reasoning or None,
        )
        db.add(assistant)
        # M24.0：思考模型要求 tool_calls 轮也回传 reasoning_content，否则下一轮 400
        asst_msg: dict = {"role": "assistant", "content": content or None, "tool_calls": calls}
        if reasoning:
            asst_msg["reasoning_content"] = reasoning
        llm_messages.append(asst_msg)

        answered: set[str] = set()
        try:
            for call in calls:
                name = call["function"]["name"]
                call_id = call["id"] or f"call_{name}"
                try:
                    args = json.loads(call["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                yield {"tool": {"id": call_id, "name": name, "status": "start", "args": args}}
                try:
                    # 屏蔽取消：客户端断开不能杀死执行中的工具（run_code 可能跑几十秒）；
                    # 断开后由 router 侧把本轮 drain 跑完落库，用户回来即见完整结果。
                    with anyio.CancelScope(shield=True):
                        result = await dispatch_tool(ctx, name, args)
                except Exception as e:  # 工具异常不致命，回给模型处理
                    result = f"{TOOL_ERROR_PREFIX}: {e!s}"
                # M3.17 自进化：同一工具本轮第 2 次出错 → 追加归因提示（引导先找根因、解决后记录经验教训）
                if result.startswith(TOOL_ERROR_PREFIX):
                    tool_err_counts[name] = tool_err_counts.get(name, 0) + 1
                    if tool_err_counts[name] == 2:
                        result += (
                            f"\n\n[系统提示] 工具 {name} 本轮已第 2 次出错。请先分析根因再决定是否重试；"
                            "若属于可避免的操作错误，解决后请用 memory_add_lesson 记录一条经验教训。"
                        )
                db.add(
                    Message(
                        conversation_id=conversation.id,
                        role="tool",
                        content=result,
                        tool_call_id=call["id"],
                        name=name,
                    )
                )
                answered.add(call["id"])
                llm_messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
                yield {"tool": {"id": call_id, "name": name, "status": "done", "result_preview": result[:200]}}
            await db.flush()
            llm_messages = _prune_old_tool_results(llm_messages)  # turn 内同样修剪：多轮循环时旧 stdout 不无界累积（O(轮数²) 成本刹车）
        except (GeneratorExit, asyncio.CancelledError):
            # 兜底（如服务关停）：补齐缺失的 tool 响应并提交——任何情况下都不能留下
            # "assistant 带 tool_calls 但无 tool 响应"的残缺会话（下轮必然 400）。
            for call in calls:
                if call["id"] not in answered:
                    db.add(
                        Message(
                            conversation_id=conversation.id,
                            role="tool",
                            content="[已中断] 连接断开，本次工具执行未完成；如需结果请重新发起。",
                            tool_call_id=call["id"],
                            name=call["function"]["name"],
                        )
                    )
            await db.flush()
            await db.commit()
            raise

    yield {"error": f"已达到最大工具调用轮次（{agent.max_loop_rounds}），请缩小任务范围或换个问法"}
