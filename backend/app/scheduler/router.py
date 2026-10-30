"""定时任务 API（M3.8）：属主自管自己的任务；调度语义（cron 解析、下次触发）全在 scheduler.engine。"""
import asyncio
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.router import _check_chat_access
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.agent import Agent
from app.models.base import utcnow
from app.models.gateway import Model
from app.models.scheduler import ScheduledTask
from app.models.user import User
from app.scheduler.engine import _INFLIGHT, _execute_task, compute_next, is_valid_cron, is_valid_tz
from app.scheduler.feishu_push import push_feishu_card, push_feishu_card_to_me, validate_webhook

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


class TaskIn(BaseModel):
    agent_id: int
    name: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1)
    cron_expr: str
    timezone: str = "Asia/Shanghai"
    model_id: int | None = None
    feishu_webhook: str | None = None  # M4b：跑完推飞书群机器人
    feishu_secret: str | None = None   # 机器人"签名校验"的密钥（选了关键词模式则不用填）
    push_to_me: bool = False           # M8：跑完由应用机器人私聊推给本人（与 webhook 二选一）


class TaskPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    prompt: str | None = Field(default=None, min_length=1)
    cron_expr: str | None = None
    timezone: str | None = None
    model_id: int | None = None
    enabled: bool | None = None
    feishu_webhook: str | None = None  # 传 "" = 清除（区分"不传"与"清空"）
    feishu_secret: str | None = None
    push_to_me: bool | None = None


def _out(t: ScheduledTask) -> dict:
    return {
        "id": t.id,
        "agent_id": t.agent_id,
        "name": t.name,
        "prompt": t.prompt,
        "cron_expr": t.cron_expr,
        "timezone": t.timezone,
        "model_id": t.model_id,
        "enabled": t.enabled,
        "next_run_at": t.next_run_at.isoformat() if t.next_run_at else None,
        "last_run_at": t.last_run_at.isoformat() if t.last_run_at else None,
        "last_status": t.last_status,
        "last_error": t.last_error,
        "last_conversation_id": t.last_conversation_id,
        "run_count": t.run_count,
        "feishu_webhook": t.feishu_webhook,
        "feishu_secret": t.feishu_secret,
        "push_to_me": t.push_to_me,
        "last_push_error": t.last_push_error,
        "created_at": t.created_at.isoformat(),
    }


async def _get_own_task(db: AsyncSession, user: User, task_id: int) -> ScheduledTask:
    """归属校验：查不到即 404，不暴露存在性。"""
    t = (
        await db.execute(
            select(ScheduledTask).where(
                ScheduledTask.id == task_id,
                ScheduledTask.user_id == user.id,
                ScheduledTask.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(404, "定时任务不存在")
    return t


async def _check_agent(db: AsyncSession, user: User, agent_id: int) -> Agent:
    agent = (
        await db.execute(select(Agent).where(Agent.id == agent_id, Agent.deleted_at.is_(None), Agent.status == "active"))
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "数字人不存在")
    _check_chat_access(agent, user)
    return agent


def _validate_schedule(cron_expr: str, timezone: str) -> None:
    if not is_valid_cron(cron_expr):
        raise HTTPException(400, "cron 表达式无效（需要 5 段：分 时 日 月 周，如 30 9 * * 1）")
    if not is_valid_tz(timezone):
        raise HTTPException(400, f"时区无效：{timezone}")


async def _validate_model(db: AsyncSession, agent: Agent, model_id: int | None) -> None:
    """model_id 给了就必须存在、启用、且在 agent allowlist 内。"""
    if model_id is None:
        return
    allowlist: list[int] | None = json.loads(agent.model_allowlist) if agent.model_allowlist else None
    if allowlist is not None and model_id not in allowlist:
        raise HTTPException(400, "该模型不在此数字人的允许列表中")
    model = (
        await db.execute(select(Model).where(Model.id == model_id, Model.deleted_at.is_(None), Model.enabled.is_(True)))
    ).scalar_one_or_none()
    if model is None:
        raise HTTPException(400, "模型不存在或已下线")


def _validate_push(user: User, push_to_me: bool, webhook: str | None) -> None:
    """推送配置校验：私聊推本人 与 群 webhook 二选一；推本人要求用户已绑飞书。"""
    if push_to_me and webhook:
        raise HTTPException(400, "「私聊推给我」和「群 Webhook」只能二选一（个人摘要推群会泄露给群成员）")
    if push_to_me and not user.feishu_open_id:
        raise HTTPException(400, "你的账号未绑定飞书（先用飞书登录一次平台），无法私聊推送")


@router.get("/tasks")
async def list_tasks(agent_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (
        (
            await db.execute(
                select(ScheduledTask)
                .where(
                    ScheduledTask.user_id == user.id,
                    ScheduledTask.agent_id == agent_id,
                    ScheduledTask.deleted_at.is_(None),
                )
                .order_by(ScheduledTask.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_out(t) for t in rows]


@router.post("/tasks", status_code=201)
async def create_task(body: TaskIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    agent = await _check_agent(db, user, body.agent_id)
    _validate_schedule(body.cron_expr, body.timezone)
    await _validate_model(db, agent, body.model_id)
    webhook = (body.feishu_webhook or "").strip() or None
    if webhook is not None and (err := validate_webhook(webhook)) is not None:
        raise HTTPException(400, err)
    _validate_push(user, body.push_to_me, webhook)
    t = ScheduledTask(
        user_id=user.id,
        agent_id=body.agent_id,
        name=body.name.strip(),
        prompt=body.prompt,
        cron_expr=body.cron_expr.strip(),
        timezone=body.timezone,
        model_id=body.model_id,
        enabled=True,
        next_run_at=compute_next(body.cron_expr.strip(), body.timezone, datetime.now(UTC)),
        feishu_webhook=webhook,
        feishu_secret=(body.feishu_secret or "").strip() or None,
        push_to_me=body.push_to_me,
    )
    db.add(t)
    await db.commit()
    return _out(t)


@router.patch("/tasks/{task_id}")
async def update_task(task_id: int, body: TaskPatch, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    t = await _get_own_task(db, user, task_id)
    agent = await _check_agent(db, user, t.agent_id)

    # validate-then-swap：全部校验通过前不动 ORM 对象，失败编辑不影响在线调度
    new_cron = body.cron_expr.strip() if body.cron_expr is not None else t.cron_expr
    new_tz = body.timezone if body.timezone is not None else t.timezone
    _validate_schedule(new_cron, new_tz)
    if body.model_id is not None:
        await _validate_model(db, agent, body.model_id)

    schedule_changed = new_cron != t.cron_expr or new_tz != t.timezone
    if body.name is not None:
        t.name = body.name.strip()
    if body.prompt is not None:
        t.prompt = body.prompt
    if body.model_id is not None:
        t.model_id = body.model_id
    # 飞书推送：传 "" 清除、None 不动；改了 webhook 就必须合法
    if body.feishu_webhook is not None:
        webhook = body.feishu_webhook.strip() or None
        if webhook is not None and (err := validate_webhook(webhook)) is not None:
            raise HTTPException(400, err)
        t.feishu_webhook = webhook
    if body.feishu_secret is not None:
        t.feishu_secret = body.feishu_secret.strip() or None
    if body.push_to_me is not None:
        t.push_to_me = body.push_to_me
    # 推送配置整体复核（patch 可能只改一边，组合后才看得出冲突）
    _validate_push(user, t.push_to_me, t.feishu_webhook)
    t.cron_expr = new_cron
    t.timezone = new_tz
    if body.enabled is not None:
        t.enabled = body.enabled
    # 调度变了，或从停用翻启用：从现在重算下次触发（锚定 now，不追补停机期间的次数）
    if schedule_changed or (body.enabled is True):
        t.next_run_at = compute_next(t.cron_expr, t.timezone, datetime.now(UTC)) if t.enabled else None
    elif body.enabled is False:
        t.next_run_at = None
    await db.commit()
    return _out(t)


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    t = await _get_own_task(db, user, task_id)
    t.deleted_at = utcnow()
    await db.commit()


@router.post("/tasks/{task_id}/run")
async def run_task_now(task_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """立即运行一次：不动 next_run_at（手动触发不扰乱正常调度）。"""
    t = await _get_own_task(db, user, task_id)
    if t.id in _INFLIGHT:
        raise HTTPException(409, "该任务正在运行中")
    t.last_run_at = utcnow()
    t.last_status = "running"
    t.last_error = None
    t.run_count += 1
    await db.commit()
    _INFLIGHT.add(t.id)
    asyncio.create_task(_execute_task(t.id))
    return {"started": True}


@router.post("/tasks/{task_id}/test-push")
async def test_push(task_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """立即推一条测试消息——私聊推本人或群 webhook 按任务配置走，即时验证不用等调度点。"""
    t = await _get_own_task(db, user, task_id)
    body_text = f"定时任务飞书推送已配通（{datetime.now(UTC):%Y-%m-%d %H:%M} UTC）。\n之后本任务每次跑完，结果会以这种卡片形式推送，长内容自动拆多条。"
    if t.push_to_me:
        if not user.feishu_open_id:
            raise HTTPException(400, "你的账号未绑定飞书（先用飞书登录一次平台），无法私聊推送")
        err = await push_feishu_card_to_me(user.feishu_open_id, f"【测试】{t.name}", body_text)
    else:
        if not t.feishu_webhook:
            raise HTTPException(400, "该任务未配置飞书推送（选「私聊推给我」或填群 Webhook）")
        err = await push_feishu_card(t.feishu_webhook, f"【测试】{t.name}", body_text, t.feishu_secret)
    if err:
        raise HTTPException(400, err)
    t.last_push_error = None
    await db.commit()
    return {"ok": True}


@router.get("/cron-preview")
async def cron_preview(expr: str, tz: str = "Asia/Shanghai", user: User = Depends(get_current_user)) -> dict:
    """未来 3 次触发时刻预览（自定义 cron 输入框的即时反馈）。"""
    _validate_schedule(expr, tz)
    from croniter import croniter
    from zoneinfo import ZoneInfo

    it = croniter(expr, datetime.now(UTC).astimezone(ZoneInfo(tz)))
    return {"next": [it.get_next(datetime).isoformat() for _ in range(3)]}
