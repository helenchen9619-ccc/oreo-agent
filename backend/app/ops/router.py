"""AGENT-OPS 运行中心 API（一期：只读观察 + 审计留痕）。

总览/钻取全部只读；查看对话内容、预览/下载他人工作区文件会写 audit_logs
（admin.read_conversation / admin.read_workspace）。权限走模块门禁 require_module("ops")。
"""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import require_module
from app.chat.router import _ACTIVE_TASKS, _ACTIVE_TURNS
from app.core.database import get_db
from app.models.agent import Agent, AgentMemoryNote, WorkspaceFile
from app.models.chat import Conversation, Message, TurnRecord
from app.models.gateway import Model, ModelProvider, UsageRecord
from app.models.user import AuditLog, User
from app.workspace.router import _extract_text

router = APIRouter(prefix="/api/admin/ops", tags=["ops"])

# 健康度阈值（见 AGENT-OPS-PROPOSAL.md §2）
HEALTH_ERR_COUNT = 3          # 24h 错误轮次 ≥ 此值 → 🔴
HEALTH_ERR_RATE_RED = 0.2     # 24h 错误率 > 此值 → 🔴
HEALTH_ERR_RATE_WARN = 0.05   # 24h 错误率 ≥ 此值 → 🟡
HEALTH_SLOW_MS = 60_000       # 24h 平均轮次耗时 > 此值 → 🟡
IDLE_DAYS = 7                 # 超过此天数无活动 → ⚪闲置

OpsUser = Depends(require_module("ops"))


def health_of(*, turns: int, errors: int, avg_ms: float | None, last_active: datetime | None, now: datetime) -> str:
    """健康灯（纯函数，恒按 24h 窗口的数据调用）：error/warn/ok/idle。"""
    if last_active is None or last_active < now - timedelta(days=IDLE_DAYS):
        return "idle"
    if turns == 0:
        return "ok"  # 24h 无轮次但 7 天内有活动：没有错误可报
    rate = errors / turns
    if errors >= HEALTH_ERR_COUNT or rate > HEALTH_ERR_RATE_RED:
        return "error"
    if rate >= HEALTH_ERR_RATE_WARN or (avg_ms is not None and avg_ms > HEALTH_SLOW_MS):
        return "warn"
    return "ok"


async def _turn_stats(db: AsyncSession, since: datetime, agent_id: int | None = None) -> dict[int, dict]:
    """按 agent 聚合 turn_records：{agent_id: {turns, errors, avg_ms}}。"""
    q = (
        select(
            TurnRecord.agent_id,
            func.count().label("turns"),
            func.sum(case((TurnRecord.status == "error", 1), else_=0)).label("errors"),
            func.avg(TurnRecord.duration_ms).label("avg_ms"),
        )
        .where(TurnRecord.started_at >= since)
        .group_by(TurnRecord.agent_id)
    )
    if agent_id is not None:
        q = q.where(TurnRecord.agent_id == agent_id)
    rows = (await db.execute(q)).all()
    return {r.agent_id: {"turns": r.turns, "errors": int(r.errors or 0), "avg_ms": float(r.avg_ms) if r.avg_ms is not None else None} for r in rows}


async def _usage_stats(db: AsyncSession, since: datetime) -> dict[int, dict]:
    """按 agent 聚合 usage_records：{agent_id: {tokens, cost}}。"""
    rows = (
        await db.execute(
            select(
                UsageRecord.agent_id,
                func.sum(UsageRecord.input_tokens + UsageRecord.output_tokens).label("tokens"),
                func.sum(UsageRecord.cost).label("cost"),
            )
            .where(UsageRecord.created_at >= since)
            .group_by(UsageRecord.agent_id)
        )
    ).all()
    return {r.agent_id: {"tokens": int(r.tokens or 0), "cost": float(r.cost or 0)} for r in rows if r.agent_id is not None}


async def _last_active_map(db: AsyncSession) -> dict[int, datetime]:
    rows = (await db.execute(select(TurnRecord.agent_id, func.max(TurnRecord.started_at)).group_by(TurnRecord.agent_id))).all()
    return {r[0]: r[1] for r in rows}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _turn_dict(t: TurnRecord) -> dict:
    return {
        "id": t.id,
        "conversation_id": t.conversation_id,
        "user_id": t.user_id,
        "model_id": t.model_id,
        "started_at": t.started_at.isoformat(),
        "duration_ms": t.duration_ms,
        "rounds": t.rounds,
        "tool_calls": t.tool_calls,
        "tool_errors": t.tool_errors,
        "status": t.status,
        "error_msg": t.error_msg,
    }


@router.get("/agents")
async def ops_agents(
    window: str = Query("24h", pattern="^(24h|7d)$"),
    db: AsyncSession = Depends(get_db),
    _: User = OpsUser,
) -> list[dict]:
    """L1 总览：每实例一行。健康灯恒按 24h 窗口；指标列随 window 切换。"""
    now = datetime.now(UTC)
    since = now - (timedelta(hours=24) if window == "24h" else timedelta(days=7))
    agents = (
        (await db.execute(select(Agent).where(Agent.is_template.is_(False), Agent.deleted_at.is_(None)).order_by(Agent.id.asc())))
        .scalars()
        .all()
    )
    owners = {u.id: u for u in ((await db.execute(select(User).where(User.id.in_({a.owner_user_id for a in agents})))).scalars().all())} if agents else {}
    stats = await _turn_stats(db, since)
    stats_24h = await _turn_stats(db, now - timedelta(hours=24))
    usage = await _usage_stats(db, since)
    last_active = await _last_active_map(db)
    active_now: dict[int, int] = {}
    for entry in _ACTIVE_TASKS.values():
        active_now[entry["agent_id"]] = active_now.get(entry["agent_id"], 0) + 1

    out = []
    for a in agents:
        s = stats.get(a.id, {"turns": 0, "errors": 0, "avg_ms": None})
        h = stats_24h.get(a.id, {"turns": 0, "errors": 0, "avg_ms": None})
        u = usage.get(a.id, {"tokens": 0, "cost": 0.0})
        la = last_active.get(a.id)
        owner = owners.get(a.owner_user_id)
        out.append({
            "id": a.id,
            "name": a.name,
            "owner_id": a.owner_user_id,
            "owner_name": (owner.name or owner.username) if owner else None,
            "status": a.status,
            "health": health_of(turns=h["turns"], errors=h["errors"], avg_ms=h["avg_ms"], last_active=la, now=now),
            "turns": s["turns"],
            "error_rate": (s["errors"] / s["turns"]) if s["turns"] else 0.0,
            "avg_duration_ms": s["avg_ms"],
            "tokens": u["tokens"],
            "cost": u["cost"],
            "last_active": la.isoformat() if la else None,
            "active_now": active_now.get(a.id, 0),
        })
    return out


@router.get("/agents/{agent_id}")
async def ops_agent_detail(agent_id: int, db: AsyncSession = Depends(get_db), _: User = OpsUser) -> dict:
    """L2 详情：基础信息 + 24h/7d 指标 + 最近 20 条轮次记录。"""
    a = (await db.execute(select(Agent).where(Agent.id == agent_id, Agent.deleted_at.is_(None)))).scalar_one_or_none()
    if a is None:
        raise HTTPException(404, "数字人不存在")
    now = datetime.now(UTC)
    owner = await db.get(User, a.owner_user_id) if a.owner_user_id else None
    s24 = (await _turn_stats(db, now - timedelta(hours=24), agent_id)).get(agent_id, {"turns": 0, "errors": 0, "avg_ms": None})
    s7d = (await _turn_stats(db, now - timedelta(days=7), agent_id)).get(agent_id, {"turns": 0, "errors": 0, "avg_ms": None})
    usage7d = (await _usage_stats(db, now - timedelta(days=7))).get(agent_id, {"tokens": 0, "cost": 0.0})
    la = (await db.execute(select(func.max(TurnRecord.started_at)).where(TurnRecord.agent_id == agent_id))).scalar_one_or_none()
    turns = (
        (
            await db.execute(
                select(TurnRecord).where(TurnRecord.agent_id == agent_id).order_by(TurnRecord.id.desc()).limit(20)
            )
        )
        .scalars()
        .all()
    )
    users = {u.id: u for u in ((await db.execute(select(User).where(User.id.in_({t.user_id for t in turns})))).scalars().all())} if turns else {}
    # M24.1：轮次记录本就有 model_id，排障时要一眼看到「哪个模型出的错」→ 解析成 display_name 随 recent_turns 返回
    models = (
        {m.id: m for m in ((await db.execute(select(Model).where(Model.id.in_({t.model_id for t in turns if t.model_id})))).scalars().all())}
        if turns else {}
    )
    return {
        "agent": {
            "id": a.id,
            "name": a.name,
            "status": a.status,
            "owner_id": a.owner_user_id,
            "owner_name": (owner.name or owner.username) if owner else None,
            "default_model_id": a.default_model_id,
            "created_at": a.created_at.isoformat(),
        },
        "health": health_of(turns=s24["turns"], errors=s24["errors"], avg_ms=s24["avg_ms"], last_active=la, now=now),
        "metrics": {
            "24h": {**s24, "error_rate": (s24["errors"] / s24["turns"]) if s24["turns"] else 0.0},
            "7d": {**s7d, "error_rate": (s7d["errors"] / s7d["turns"]) if s7d["turns"] else 0.0, **usage7d},
        },
        "recent_turns": [
            {
                **_turn_dict(t),
                "user_name": (lambda u: (u.name or u.username) if u else None)(users.get(t.user_id)),
                "model_name": (lambda m: m.display_name if m else None)(models.get(t.model_id)),
            }
            for t in turns
        ],
    }


@router.get("/agents/{agent_id}/conversations")
async def ops_agent_conversations(agent_id: int, db: AsyncSession = Depends(get_db), _: User = OpsUser) -> list[dict]:
    """该 agent 全部会话（跨用户）。"""
    rows = (
        await db.execute(
            select(Conversation, User)
            .join(User, User.id == Conversation.user_id)
            .where(Conversation.agent_id == agent_id, Conversation.deleted_at.is_(None))
            .order_by(Conversation.updated_at.desc())
        )
    ).all()
    counts = dict(
        (await db.execute(select(Message.conversation_id, func.count()).group_by(Message.conversation_id))).all()
    )
    return [
        {
            "id": c.id,
            "title": c.title,
            "channel": c.channel,
            "user_id": u.id,
            "user_name": u.name or u.username,
            "message_count": int(counts.get(c.id, 0)),
            "updated_at": c.updated_at.isoformat(),
            "active": c.id in _ACTIVE_TURNS,
        }
        for c, u in rows
    ]


@router.get("/conversations/{cid}/messages")
async def ops_conversation_messages(cid: int, db: AsyncSession = Depends(get_db), admin: User = OpsUser) -> dict:
    """只读消息时间线（跨用户）。★查看行为写 audit_logs。"""
    conv = (await db.execute(select(Conversation).where(Conversation.id == cid))).scalar_one_or_none()
    if conv is None:
        raise HTTPException(404, "会话不存在")
    owner = await db.get(User, conv.user_id)
    rows = ((await db.execute(select(Message).where(Message.conversation_id == cid).order_by(Message.id.asc()))).scalars().all())
    messages = [
        {
            "id": m.id, "role": m.role, "content": m.content, "compacted": m.compacted,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "tool_calls": json.loads(m.tool_calls) if m.tool_calls else None,
            "tool_call_id": m.tool_call_id,
            "name": m.name,
            "attachments": [
                {"idx": i, "name": a.get("name", "image"), "mime": a.get("mime", "image/png")}
                for i, a in enumerate(json.loads(m.attachments))
            ] if m.attachments else None,
        }
        for m in rows if m.role in ("user", "assistant", "tool")
    ]
    db.add(AuditLog(
        actor_user_id=admin.id,
        action="admin.read_conversation",
        target_type="conversation",
        target_id=str(cid),
        detail=json.dumps({"agent_id": conv.agent_id, "owner_user_id": conv.user_id, "message_count": len(messages)}, ensure_ascii=False),
    ))
    await db.commit()
    return {
        "conversation": {
            "id": conv.id,
            "title": conv.title,
            "channel": conv.channel,
            "agent_id": conv.agent_id,
            "user_id": conv.user_id,
            "user_name": (owner.name or owner.username) if owner else None,
        },
        "active": cid in _ACTIVE_TURNS,
        "messages": messages,
    }


@router.get("/agents/{agent_id}/workspace")
async def ops_agent_workspace(
    agent_id: int,
    user_id: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _: User = OpsUser,
) -> list[dict]:
    """工作区文件列表，按用户分组（只读，不含内容——内容走 /workspace/{file_id} 并留痕）。"""
    q = (
        select(WorkspaceFile, User)
        .join(User, User.id == WorkspaceFile.user_id)
        .where(WorkspaceFile.agent_id == agent_id, WorkspaceFile.deleted_at.is_(None))
        .order_by(WorkspaceFile.user_id.asc(), WorkspaceFile.filename.asc())
    )
    if user_id is not None:
        q = q.where(WorkspaceFile.user_id == user_id)
    rows = (await db.execute(q)).all()
    groups: dict[int, dict] = {}
    for f, u in rows:
        g = groups.setdefault(u.id, {"user_id": u.id, "user_name": u.name or u.username, "files": []})
        g["files"].append({
            "id": f.id, "filename": f.filename, "size": f.size, "mime": f.mime, "pinned": f.pinned,
            "updated_at": f.updated_at.isoformat(),
        })
    return list(groups.values())


async def _get_any_file(db: AsyncSession, file_id: int) -> WorkspaceFile:
    f = (await db.execute(select(WorkspaceFile).where(WorkspaceFile.id == file_id, WorkspaceFile.deleted_at.is_(None)))).scalar_one_or_none()
    if f is None:
        raise HTTPException(404, "文件不存在")
    return f


def _audit_read_workspace(db: AsyncSession, admin: User, f: WorkspaceFile, mode: str) -> None:
    db.add(AuditLog(
        actor_user_id=admin.id,
        action="admin.read_workspace",
        target_type="workspace_file",
        target_id=str(f.id),
        detail=json.dumps({"agent_id": f.agent_id, "owner_user_id": f.user_id, "filename": f.filename, "mode": mode}, ensure_ascii=False),
    ))


@router.get("/workspace/{file_id}")
async def ops_workspace_file(file_id: int, db: AsyncSession = Depends(get_db), admin: User = OpsUser) -> dict:
    """只读预览（含文本内容，xlsx/docx/pptx 懒抽取）。★留痕。"""
    f = await _get_any_file(db, file_id)
    if not f.content_text and Path(f.filename).suffix.lower() in (".xlsx", ".docx", ".pptx"):
        try:
            raw = Path(f.storage_path).read_bytes()
        except OSError:
            raw = b""
        text = _extract_text(f.filename, raw) if raw else None
        if text:
            f.content_text = text
    _audit_read_workspace(db, admin, f, "preview")
    await db.commit()
    return {"id": f.id, "filename": f.filename, "size": f.size, "mime": f.mime, "content_text": f.content_text}


@router.get("/workspace/{file_id}/raw")
async def ops_workspace_file_raw(file_id: int, db: AsyncSession = Depends(get_db), admin: User = OpsUser) -> FileResponse:
    """只读下载原始字节。★留痕。"""
    f = await _get_any_file(db, file_id)
    p = Path(f.storage_path)
    if not p.is_file():
        raise HTTPException(404, "文件已从磁盘移除")
    _audit_read_workspace(db, admin, f, "raw")
    await db.commit()
    return FileResponse(p, filename=p.name, media_type=f.mime or "application/octet-stream")


@router.get("/workspace/{file_id}/export.docx")
async def ops_workspace_file_export_docx(file_id: int, db: AsyncSession = Depends(get_db), admin: User = OpsUser):
    """.md → .docx 导出（与员工侧「导出为 Word」同款）。★留痕。"""
    from app.workspace.router import _export_md_as_docx

    f = await _get_any_file(db, file_id)
    resp = _export_md_as_docx(f)
    _audit_read_workspace(db, admin, f, "export_docx")
    await db.commit()
    return resp


@router.get("/agents/{agent_id}/memory-notes")
async def ops_agent_memory_notes(agent_id: int, db: AsyncSession = Depends(get_db), _: User = OpsUser) -> list[dict]:
    """记忆笔记列表（agent_memory_notes），按用户分组（只读）。"""
    rows = (
        await db.execute(
            select(AgentMemoryNote, User)
            .join(User, User.id == AgentMemoryNote.user_id)
            .where(AgentMemoryNote.agent_id == agent_id)
            .order_by(AgentMemoryNote.user_id.asc(), AgentMemoryNote.id.desc())
        )
    ).all()
    groups: dict[int, dict] = {}
    for n, u in rows:
        g = groups.setdefault(u.id, {"user_id": u.id, "user_name": u.name or u.username, "notes": []})
        g["notes"].append({
            "id": n.id,
            "note_date": n.note_date.isoformat(),
            "content": n.content,
            "created_at": n.created_at.isoformat(),
        })
    return list(groups.values())


@router.post("/agents/{agent_id}/memory-consolidate")
async def ops_agent_memory_consolidate(agent_id: int, db: AsyncSession = Depends(get_db), admin: User = OpsUser) -> dict:
    """手动触发记忆提炼：属主笔记 → MEMORY.md「用户偏好与约定」区（默认模型）。★写操作，留痕（不落提炼正文）。"""
    from app.runtime.memory import consolidate_memory

    a = (await db.execute(select(Agent).where(Agent.id == agent_id, Agent.deleted_at.is_(None)))).scalar_one_or_none()
    if a is None:
        raise HTTPException(404, "数字人不存在")
    if a.owner_user_id is None:
        raise HTTPException(400, "模板数字人无属主，没有可提炼的笔记")
    if not a.default_model_id:
        raise HTTPException(400, "该数字人未配置默认模型，无法提炼")
    model = (
        await db.execute(select(Model).where(Model.id == a.default_model_id, Model.enabled.is_(True), Model.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if model is None:
        raise HTTPException(400, "默认模型不存在或已停用")
    provider = (
        await db.execute(
            select(ModelProvider).where(
                ModelProvider.id == model.provider_id, ModelProvider.enabled.is_(True), ModelProvider.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if provider is None:
        raise HTTPException(400, "默认模型的供应商不存在或已停用")

    result = await consolidate_memory(db, agent=a, actor=admin, model=model, provider=provider)
    db.add(AuditLog(
        actor_user_id=admin.id,
        action="admin.memory_consolidate",
        target_type="agent",
        target_id=str(a.id),
        detail=json.dumps(
            {"agent_name": a.name, "owner_user_id": a.owner_user_id, "ok": result["ok"],
             "notes_in": result.get("notes_in"), "items_out": result.get("items_out"), "reason": result.get("reason")},
            ensure_ascii=False,
        ),
    ))
    await db.commit()
    if not result["ok"]:
        raise HTTPException(400, result["reason"])
    return result


@router.get("/runtime/active")
async def ops_runtime_active(db: AsyncSession = Depends(get_db), _: User = OpsUser) -> list[dict]:
    """实时运行中的轮次（内存态）：会话/agent/用户/已跑时长。"""
    now = datetime.now(UTC)
    agent_ids = {e["agent_id"] for e in _ACTIVE_TASKS.values()}
    user_ids = {e["user_id"] for e in _ACTIVE_TASKS.values()}
    agents = {a.id: a for a in ((await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))).scalars().all())} if agent_ids else {}
    users = {u.id: u for u in ((await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all())} if user_ids else {}
    out = []
    for cid, e in _ACTIVE_TASKS.items():
        started = _aware(e["started_at"])
        a = agents.get(e["agent_id"])
        u = users.get(e["user_id"])
        out.append({
            "conversation_id": cid,
            "agent_id": e["agent_id"],
            "agent_name": a.name if a else None,
            "user_id": e["user_id"],
            "user_name": (u.name or u.username) if u else None,
            "model_id": e["model_id"],
            "started_at": started.isoformat(),
            "elapsed_s": int((now - started).total_seconds()),
        })
    out.sort(key=lambda x: -x["elapsed_s"])
    return out
