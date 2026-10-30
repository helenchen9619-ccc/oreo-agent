"""员工端对话 API（M2 版）：会话挂数字人，消息走运行时引擎（工具调用 + compaction + SSE 事件）。"""
import asyncio
import base64
import binascii
import json
import time
from pathlib import Path

import anyio
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.fork import FORK_TOOL_PLACEHOLDER, plan_fork
from app.core.config import get_settings
from app.core.database import SessionLocal, get_db
from app.core.deps import get_current_user
from app.gateway.service import GatewayError, check_model_access, check_quota, evaluate_grants
from app.models.agent import Agent
from app.models.base import utcnow
from app.models.chat import Conversation, Message, TurnRecord
from app.models.gateway import Model, ModelGrant, ModelProvider
from app.models.user import AuditLog, User
from app.runtime.engine import TOOL_ERROR_PREFIX, ModelBusyError, _is_retryable_llm_error, _maybe_compact, run_turn

router = APIRouter(prefix="/api/chat", tags=["chat"])

DATA_DIR = Path(getattr(get_settings(), "DATA_DIR", "data"))
MAX_IMAGES = 4                 # 每条消息最多附图数
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 单张 5MB（二进制）

# 正在执行的会话（单 worker 内存态）：断开后后台任务仍继续跑完，期间拒绝同会话再发消息
_ACTIVE_TURNS: set[int] = set()
# 会话 id → {task, started_at, agent_id, user_id, model_id}（供取消端点 task.cancel() 与运行中心实时态）
_ACTIVE_TASKS: dict[int, dict] = {}


class TurnCounters:
    """run_turn 事件流 → turn_records 指标（纯逻辑，便于单测）。"""

    def __init__(self) -> None:
        self.rounds = 0
        self.tool_calls = 0
        self.tool_errors = 0
        self.error_msg: str | None = None

    def observe(self, event: dict) -> bool:
        """统计一个事件；返回 True = 应转发给客户端，False = 内部元事件（如 _round）。"""
        if "_round" in event:
            self.rounds = max(self.rounds, int(event["_round"]))
            return False
        tool = event.get("tool")
        if tool:
            if tool.get("status") == "start":
                self.tool_calls += 1
            elif tool.get("status") == "done" and str(tool.get("result_preview") or "").startswith(TOOL_ERROR_PREFIX):
                self.tool_errors += 1
        if "error" in event:
            self.error_msg = str(event["error"])[:500]
        return True


async def _write_turn_record(*, conversation_id: int, agent_id: int, user_id: int, model_id: int,
                             started_at, duration_ms: int, counters: TurnCounters, cancelled: bool) -> None:
    """落一条轮次记录。独立会话 + 取消屏蔽：主会话可能已坏、任务可能处于取消态，都不能影响记录。

    记录写失败只告警不抛出——可观测性绝不能反过来弄挂对话本体。"""
    status = "cancelled" if cancelled else ("error" if counters.error_msg else "ok")
    try:
        with anyio.CancelScope(shield=True):
            async with SessionLocal() as rs:
                rs.add(TurnRecord(
                    conversation_id=conversation_id, agent_id=agent_id, user_id=user_id, model_id=model_id,
                    started_at=started_at, duration_ms=duration_ms,
                    rounds=counters.rounds, tool_calls=counters.tool_calls, tool_errors=counters.tool_errors,
                    status=status, error_msg=counters.error_msg,
                ))
                await rs.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[turn_records] 写入失败 conv={conversation_id}: {e!s}")


async def _run_turn_background(*, user_id: int, agent_id: int, conversation_id: int, model_id: int, queue: asyncio.Queue) -> None:
    """在独立任务 + 独立 DB 会话里执行一轮对话，事件进队列。

    关键：不能用请求作用域的 session——客户端断开后依赖清理会关掉它（连接池污染教训），
    所以这里用自己的 SessionLocal 重新按 id 取实体。断连只是没人再消费队列，任务照跑。
    每轮结束（含异常/取消）在 finally 落一条 turn_records（AGENT-OPS）。
    """
    started_at = utcnow()
    monotonic = time.monotonic()
    counters = TurnCounters()
    cancelled = False
    try:
        async with SessionLocal() as s:
            user = await s.get(User, user_id)
            agent = await s.get(Agent, agent_id)
            conv = await s.get(Conversation, conversation_id)
            model = await s.get(Model, model_id)
            provider = await s.get(ModelProvider, model.provider_id) if model else None
            if not all((user, agent, conv, model, provider)):
                counters.error_msg = "会话或模型状态已变化，请刷新后重试"
                queue.put_nowait({"error": counters.error_msg})
                return
            async for event in run_turn(s, user=user, agent=agent, conversation=conv, model=model, provider=provider):
                if counters.observe(event):
                    queue.put_nowait(event)
            await s.commit()
    except asyncio.CancelledError:
        cancelled = True
        raise
    except ModelBusyError as e:
        # M35：重试耗尽——前端看友好文案，turn_records 留原始错误（429/overloaded 现场可排查）
        counters.error_msg = f"模型调用失败: {e.raw}"[:500]
        queue.put_nowait({"error": str(e)})
    except Exception as e:
        counters.error_msg = f"模型调用失败: {e!s}"[:500]
        queue.put_nowait({"error": f"模型调用失败: {e!s}"})
    finally:
        queue.put_nowait(None)  # 结束哨兵
        _ACTIVE_TURNS.discard(conversation_id)
        _ACTIVE_TASKS.pop(conversation_id, None)
        await _write_turn_record(
            conversation_id=conversation_id, agent_id=agent_id, user_id=user_id, model_id=model_id,
            started_at=started_at, duration_ms=int((time.monotonic() - monotonic) * 1000),
            counters=counters, cancelled=cancelled,
        )


def _check_chat_access(agent: Agent, user: User) -> None:
    """对话权限：实例仅属主（admin 例外）；模板不可直接对话（需先领用）；主 agent 仅 admin。"""
    if agent.is_template:
        raise HTTPException(400, "模板不能直接对话，请先在首页领用")
    if agent.is_master and user.role != "admin":
        raise HTTPException(404, "数字人不存在")
    if agent.owner_user_id is not None and agent.owner_user_id != user.id and user.role != "admin":
        raise HTTPException(404, "数字人不存在")


# ---------- 数字人可用模型（授权 ∩ allowlist） ----------

@router.get("/agents/{agent_id}/models")
async def agent_models(agent_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    agent = (await db.execute(select(Agent).where(Agent.id == agent_id, Agent.deleted_at.is_(None), Agent.status == "active"))).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "数字人不存在")
    _check_chat_access(agent, user)
    allowlist: list[int] | None = json.loads(agent.model_allowlist) if agent.model_allowlist else None
    models = (
        (await db.execute(select(Model).where(Model.deleted_at.is_(None), Model.enabled.is_(True), Model.model_type == "chat")))
        .scalars()
        .all()
    )
    out = []
    for m in models:
        if allowlist is not None and m.id not in allowlist:
            continue
        provider = (await db.execute(select(ModelProvider).where(ModelProvider.id == m.provider_id, ModelProvider.deleted_at.is_(None), ModelProvider.enabled.is_(True)))).scalar_one_or_none()
        if provider is None:
            continue
        grants = ((await db.execute(select(ModelGrant).where(ModelGrant.model_id == m.id, ModelGrant.deleted_at.is_(None)))).scalars().all())
        if evaluate_grants(list(grants), user):
            out.append({"id": m.id, "display_name": m.display_name, "provider": provider.name, "is_default": m.id == agent.default_model_id, "context_window": m.context_window, "supports_vision": m.supports_vision})
    return out


# ---------- 会话 ----------

class ConversationIn(BaseModel):
    agent_id: int
    title: str = "新对话"


@router.get("/conversations")
async def list_conversations(agent_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (
        (
            await db.execute(
                select(Conversation)
                .where(Conversation.user_id == user.id, Conversation.agent_id == agent_id, Conversation.deleted_at.is_(None))
                .order_by(Conversation.pinned.desc(), Conversation.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )
    # M6a：分叉来源标注（一次 IN 查询拿源会话标题；源已软删 → 标题为 None，前端显示"已删除的会话"）
    src_ids = {c.forked_from_id for c in rows if c.forked_from_id}
    title_map: dict[int, str] = {}
    if src_ids:
        title_map = dict(
            (await db.execute(select(Conversation.id, Conversation.title).where(Conversation.id.in_(src_ids)))).all()
        )
    return [
        {
            "id": c.id, "title": c.title, "pinned": c.pinned, "channel": c.channel, "updated_at": c.updated_at.isoformat(),
            "forked_from_id": c.forked_from_id,
            "forked_from_title": title_map.get(c.forked_from_id) if c.forked_from_id else None,
        }
        for c in rows
    ]


@router.post("/conversations", status_code=201)
async def create_conversation(body: ConversationIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    agent = (await db.execute(select(Agent).where(Agent.id == body.agent_id, Agent.deleted_at.is_(None), Agent.status == "active"))).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "数字人不存在")
    _check_chat_access(agent, user)
    c = Conversation(user_id=user.id, agent_id=body.agent_id, title=body.title, channel="web")
    db.add(c)
    await db.commit()
    return {"id": c.id, "title": c.title}


async def _get_own_conversation(db: AsyncSession, user: User, cid: int) -> Conversation:
    c = (await db.execute(select(Conversation).where(Conversation.id == cid, Conversation.deleted_at.is_(None)))).scalar_one_or_none()
    if c is None or c.user_id != user.id:
        raise HTTPException(404, "会话不存在")
    return c


class ConversationPatch(BaseModel):
    title: str | None = None
    pinned: bool | None = None


@router.patch("/conversations/{cid}")
async def update_conversation(cid: int, body: ConversationPatch, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """重命名 / 置顶切换（⋯ 菜单）。"""
    conv = await _get_own_conversation(db, user, cid)
    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(400, "标题不能为空")
        conv.title = title[:256]
    if body.pinned is not None:
        conv.pinned = body.pinned
    await db.commit()
    return {"id": conv.id, "title": conv.title, "pinned": conv.pinned}


@router.delete("/conversations/{cid}", status_code=204)
async def delete_conversation(cid: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    """软删会话（消息保留在库里，不再展示）。"""
    conv = await _get_own_conversation(db, user, cid)
    conv.deleted_at = utcnow()
    await db.commit()


# ---------- 会话分叉（M6a） ----------

class ForkIn(BaseModel):
    message_id: int
    title: str | None = None


@router.post("/conversations/{cid}/fork", status_code=201)
async def fork_conversation(cid: int, body: ForkIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """从某条用户消息分叉为新会话：复制该消息（含）之前的前缀，原会话不动。

    边界安全：fork 点限定 user 消息 ⇒ 前缀以 user 结尾，天然不落在 tool 配对中间
    （决策见 chat/fork.py plan_fork 的 docstring）；脏数据（未配对的 assistant.tool_calls）
    由端点在新会话里补占位 tool 行。"""
    conv = await _get_own_conversation(db, user, cid)
    if cid in _ACTIVE_TURNS:
        raise HTTPException(409, "当前会话正在生成中，请稍后再分叉")
    rows = (
        (await db.execute(select(Message).where(Message.conversation_id == cid).order_by(Message.id.asc()))).scalars().all()
    )
    try:
        plan = plan_fork(rows, body.message_id, has_summary=bool(conv.compact_summary))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    new_conv = Conversation(
        user_id=user.id,
        agent_id=conv.agent_id,
        channel="web",  # fork 是 web UI 动作；源可能是 feishu/cron 会话
        title=(body.title or f"{conv.title}（分叉）")[:256],
        forked_from_id=cid,
        compact_summary=conv.compact_summary if plan.copy_summary else None,
    )
    db.add(new_conv)
    await db.flush()

    # 脏数据兜底：源前缀里未配对的 assistant.tool_calls → 紧随其副本插入占位 tool 行
    # （tool 行必须紧跟对应 assistant，追加到末尾会变成孤儿消息触发模型 API 400）
    dangling_map = {id(src): missing for src, missing in plan.dangling}
    for m in plan.prefix:
        nm = Message(
            conversation_id=new_conv.id,
            role=m.role,
            content=m.content,
            tool_calls=m.tool_calls,
            reasoning_content=m.reasoning_content,  # M24.3：思考模型推理过程必须随分叉复制，否则分叉会话下轮必 400
            tool_call_id=m.tool_call_id,
            name=m.name,
            compacted=False if plan.uncompact_prefix else m.compacted,
            input_tokens=m.input_tokens,
            output_tokens=m.output_tokens,
            trace_id=m.trace_id,
            attachments=m.attachments,  # 附件文件按引用共享（tmp_uploads 按 message_id 寻址，不复制文件）
        )
        nm.created_at = m.created_at  # 保留原始时间戳（排序靠自增 id，时间仅用于展示）
        nm.updated_at = m.updated_at
        db.add(nm)
        for tid in dangling_map.get(id(m), []):
            db.add(Message(conversation_id=new_conv.id, role="tool", tool_call_id=tid, content=FORK_TOOL_PLACEHOLDER))
    db.add(AuditLog(
        actor_user_id=user.id, action="conversation.fork", target_type="conversation", target_id=str(new_conv.id),
        detail=json.dumps(
            {"from_conversation_id": cid, "fork_message_id": body.message_id, "copied": len(plan.prefix), "dangling_repaired": len(plan.dangling)},
            ensure_ascii=False,
        ),
    ))
    await db.commit()
    return {"id": new_conv.id, "title": new_conv.title, "forked_from_id": cid}


@router.get("/conversations/{cid}/messages")
async def list_messages(cid: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    conv = await _get_own_conversation(db, user, cid)
    rows = (
        (await db.execute(select(Message).where(Message.conversation_id == cid).order_by(Message.id.asc()))).scalars().all()
    )
    # 工具过程消息也回传，前端可折叠展示
    # active：本会话有正在服务端执行的轮次（断连后仍在跑）——前端据此显示"处理中"并轮询等结果
    return {
        "context_tokens": conv.last_context_tokens,
        "active": cid in _ACTIVE_TURNS,
        "messages": [
            {
                "id": m.id, "role": m.role, "content": m.content, "compacted": m.compacted,
                "created_at": m.created_at.isoformat() if m.created_at else None,
                "tool_calls": json.loads(m.tool_calls) if m.tool_calls else None,
                "tool_call_id": m.tool_call_id,
                "name": m.name,
                "attachments": [
                    {"idx": i, "name": a.get("name", "image"), "mime": a.get("mime", "image/png"), "ws_path": a.get("ws_path")}
                    for i, a in enumerate(json.loads(m.attachments))
                ] if m.attachments else None,
            }
            for m in rows if m.role in ("user", "assistant", "tool")
        ],
    }


@router.get("/messages/{mid}/attachments/{idx}")
async def get_attachment(mid: int, idx: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """下载消息附件（图片）。仅会话属主可访问。"""
    m = (await db.execute(select(Message).where(Message.id == mid))).scalar_one_or_none()
    if m is None or not m.attachments:
        raise HTTPException(404, "附件不存在")
    conv = (await db.execute(select(Conversation).where(Conversation.id == m.conversation_id))).scalar_one_or_none()
    if conv is None or conv.user_id != user.id:
        raise HTTPException(404, "附件不存在")
    atts = json.loads(m.attachments)
    if idx < 0 or idx >= len(atts):
        raise HTTPException(404, "附件不存在")
    path = Path(atts[idx]["path"])
    if not path.exists():
        raise HTTPException(404, "附件文件已清理")
    return FileResponse(path, media_type=atts[idx].get("mime") or "application/octet-stream")


# ---------- 发消息（SSE：delta / tool / done / error） ----------

class ImageIn(BaseModel):
    name: str = Field(default="image", max_length=128)
    data_url: str = Field(max_length=8 * 1024 * 1024)  # base64 后 ~6.7MB


class SendIn(BaseModel):
    model_id: int | None = None   # 不传用数字人默认模型
    content: str = Field(min_length=1, max_length=20000)
    images: list[ImageIn] = []    # 临时图片附件（不进工作区）


def _decode_images(images: list[ImageIn]) -> list[tuple[str, str, bytes]]:
    """解析 data URL → (name, mime, raw)。非法即 400。"""
    if len(images) > MAX_IMAGES:
        raise HTTPException(400, f"每条消息最多附 {MAX_IMAGES} 张图片")
    out = []
    for img in images:
        header, _, b64 = img.data_url.partition(",")
        if not header.startswith("data:image/") or ";base64" not in header or not b64:
            raise HTTPException(400, "图片格式不正确（需要 image/* 的 data URL）")
        mime = header[5:].split(";")[0]
        try:
            raw = base64.b64decode(b64)
        except binascii.Error:
            raise HTTPException(400, "图片 base64 解码失败") from None
        if len(raw) > MAX_IMAGE_BYTES:
            raise HTTPException(400, f"图片「{img.name}」超过 5MB 限制")
        out.append((img.name, mime, raw))
    return out


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/conversations/{cid}/messages")
async def send_message(cid: int, body: SendIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> StreamingResponse:
    conv = await _get_own_conversation(db, user, cid)
    if cid in _ACTIVE_TURNS:
        raise HTTPException(409, "上一条消息还在处理中（即使你离开页面也会在服务端跑完），请稍候再发")
    _ACTIVE_TURNS.add(cid)  # 检查+占位连在一起，防同会话并发双轮；建任务失败时下面 except 会释放
    try:
        agent = (await db.execute(select(Agent).where(Agent.id == conv.agent_id, Agent.deleted_at.is_(None)))).scalar_one_or_none()
        if agent is None:
            raise HTTPException(400, "数字人已被删除")
        _check_chat_access(agent, user)

        model_id = body.model_id or agent.default_model_id
        if model_id is None:
            raise HTTPException(400, "该数字人未配置默认模型，请手动选择模型")
        allowlist: list[int] | None = json.loads(agent.model_allowlist) if agent.model_allowlist else None
        if allowlist is not None and model_id not in allowlist:
            raise HTTPException(403, "该模型不在此数字人的允许列表中")
        model = (await db.execute(select(Model).where(Model.id == model_id, Model.deleted_at.is_(None), Model.enabled.is_(True)))).scalar_one_or_none()
        if model is None:
            raise HTTPException(404, "模型不存在或已下线")
        provider = (await db.execute(select(ModelProvider).where(ModelProvider.id == model.provider_id, ModelProvider.deleted_at.is_(None), ModelProvider.enabled.is_(True)))).scalar_one_or_none()
        if provider is None:
            raise HTTPException(400, "模型供应商不可用")

        try:
            await check_model_access(db, user, model)
            await check_quota(db, user)
        except GatewayError as e:
            raise HTTPException(e.status_code, str(e)) from e

        images = _decode_images(body.images)
        if images and not model.supports_vision:
            raise HTTPException(400, f"当前模型「{model.display_name}」不支持图片输入，请切换到支持视觉的模型")

        msg = Message(conversation_id=cid, role="user", content=body.content)
        db.add(msg)
        if conv.title == "新对话":
            conv.title = body.content[:30]
        await db.flush()

        if images:
            # 落盘 data/tmp_uploads/{user}/{message}/，attachments 只存元数据
            folder = DATA_DIR / "tmp_uploads" / str(user.id) / str(msg.id)
            folder.mkdir(parents=True, exist_ok=True)
            atts = []
            ext_map = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
            for i, (name, mime, raw) in enumerate(images):
                fp = folder / f"{i}{ext_map.get(mime, '.png')}"
                fp.write_bytes(raw)
                atts.append({"name": name, "path": str(fp), "mime": mime})
            msg.attachments = json.dumps(atts, ensure_ascii=False)
        await db.commit()
    except Exception:
        _ACTIVE_TURNS.discard(cid)
        raise

    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(
        _run_turn_background(user_id=user.id, agent_id=agent.id, conversation_id=cid, model_id=model.id, queue=queue)
    )
    _ACTIVE_TASKS[cid] = {
        "task": task,
        "started_at": utcnow(),
        "agent_id": agent.id,
        "user_id": user.id,
        "model_id": model.id,
    }

    async def event_stream():
        # 只消费队列：客户端断开时本生成器被取消，但后台任务独立继续跑完落库，
        # 用户回到会话重新加载即可看到完整结果。
        while True:
            event = await queue.get()
            if event is None:
                break
            yield _sse(event)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},  # 禁中间层缓冲，SSE 即时到达
    )


@router.post("/conversations/{cid}/cancel")
async def cancel_turn(cid: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """用户主动停止当前轮次（前端"停止"按钮）。

    与断连不同：断连是"没人看了也要跑完"，取消是"用户明确不要了"。
    engine 的 CancelledError 兜底会补齐缺失的 tool 响应并提交，不留残缺会话。
    取消是异步生效的：执行中的工具（shield 保护）会先跑完当前这个再停。"""
    await _get_own_conversation(db, user, cid)
    entry = _ACTIVE_TASKS.get(cid)
    task = entry["task"] if entry else None
    if task is not None and not task.done():
        task.cancel()
        return {"cancelled": True}
    return {"cancelled": False}


@router.post("/conversations/{cid}/compact")
async def compact_conversation(cid: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """用户手动压缩上下文（openclaw /compact 同款）：把较早消息摘要成 compact_summary。
    进行中的轮次会边跑边读消息，此时压缩会破坏它的上下文 → 409。"""
    conv = await _get_own_conversation(db, user, cid)
    if cid in _ACTIVE_TURNS:
        raise HTTPException(409, "对话正在处理中，等本轮结束后再压缩")
    agent = (await db.execute(select(Agent).where(Agent.id == conv.agent_id, Agent.deleted_at.is_(None)))).scalar_one_or_none()
    if agent is None or agent.default_model_id is None:
        raise HTTPException(400, "该数字人未配置默认模型，无法压缩")
    model = (await db.execute(select(Model).where(Model.id == agent.default_model_id, Model.deleted_at.is_(None), Model.enabled.is_(True)))).scalar_one_or_none()
    if model is None:
        raise HTTPException(400, "默认模型不可用，无法压缩")
    provider = (await db.execute(select(ModelProvider).where(ModelProvider.id == model.provider_id, ModelProvider.deleted_at.is_(None), ModelProvider.enabled.is_(True)))).scalar_one_or_none()
    if provider is None:
        raise HTTPException(400, "模型供应商不可用，无法压缩")
    try:
        result = await _maybe_compact(
            db, user=user, conversation=conv, model=model, provider=provider,
            context_window=model.context_window or 32768, force=True,
        )
    except Exception as e:
        # M35：手动压缩撞 429/过载（重试也耗尽）→ 友好 503，不裸抛 500
        if _is_retryable_llm_error(e):
            raise HTTPException(503, "模型服务繁忙，请稍后重试") from e
        raise
    if result["compacted"]:
        conv.last_context_tokens = result["context_tokens"]  # 用量条立即反映压缩后的水位
    await db.commit()
    return result
