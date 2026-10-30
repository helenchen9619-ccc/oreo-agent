"""定时任务调度引擎（M3.8）。

架构：DB 是调度唯一事实源 + 单条 asyncio tick 循环（main.py lifespan 启动，单 worker，
与 chat.router._ACTIVE_TURNS 同假设——多 worker 部署需重审）。

关键设计（吸收 OpenClaw cron 的教训）：
- 到期任务【先推进 next_run_at 再执行】：崩溃/重启不会重复触发副作用；停机 N 天
  重启后只补跑 1 次（compute_next 锚定 now 而非过期时刻，不追补 N 次）。
- 重叠策略"等不跳"：同一任务上一次还在跑（_INFLIGHT）时不推进 next_run_at，
  下个 tick 再捡起来，该次运行顺延而不是丢弃。
- 执行复用 chat 的 _run_turn_background（独立 SessionLocal + queue），注册进
  _ACTIVE_TURNS/_ACTIVE_TASKS——前端打开 cron 会话时现有恢复轮询直接生效，/cancel 也可用。
"""
import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.chat.router import _ACTIVE_TASKS, _ACTIVE_TURNS, _check_chat_access, _run_turn_background
from app.core.database import SessionLocal
from app.gateway.service import GatewayError, check_model_access, check_quota
from app.models.agent import Agent
from app.models.chat import Conversation, Message
from app.models.gateway import Model, ModelProvider
from app.models.scheduler import ScheduledTask
from app.models.user import User
from app.runtime.tools import sweep_feishu_images
# 纯函数实现已挪到 cronutil（M3.10 供 runtime.tools 复用防循环导入）；此处再导出保持既有 import 路径
from app.scheduler.cronutil import compute_next, is_valid_cron, is_valid_tz  # noqa: F401
from app.scheduler.feishu_push import push_feishu_card, push_feishu_card_to_me

logger = logging.getLogger(__name__)

TICK_SECONDS = 30
_INFLIGHT: set[int] = set()  # 正在执行的任务 id（单 worker 内存态，同 _ACTIVE_TURNS）

_IMG_SWEEP_INTERVAL = timedelta(hours=1)  # M8d：feishu_imgs/ 清扫间隔
_last_img_sweep: datetime | None = None

_WECOM_SYNC_INTERVAL = timedelta(minutes=1)  # M31：企微存档同步间隔（归档本身有分钟级延迟，再密无意义）
_last_wecom_sync: datetime | None = None

_WECOM_SWEEP_INTERVAL = timedelta(hours=1)  # M31.2：企微存档保留策略清扫间隔
_last_wecom_sweep: datetime | None = None


async def _maybe_sync_wecom_archive(now: datetime) -> None:
    """M31：距上次 ≥1 分钟拉一轮企微存档；同步失败只记日志，绝不影响调度主循环。"""
    global _last_wecom_sync
    if _last_wecom_sync is not None and now - _last_wecom_sync < _WECOM_SYNC_INTERVAL:
        return
    _last_wecom_sync = now
    try:
        from app.wecom_archive.sync import sync_wecom_archive  # 延迟 import：未配置环境零成本

        async with SessionLocal() as s:
            stats = await sync_wecom_archive(s)
            if stats and (stats.get("inserted") or stats.get("hits")):
                logger.info("wecom archive sync: %s", stats)
    except Exception:
        logger.exception("wecom archive sync failed")


async def wecom_archive_sync_on_startup() -> None:
    """M31：进程启动时立即同步一轮（部署/重启后尽快追平积压）。"""
    await _maybe_sync_wecom_archive(datetime.now(UTC))


async def _maybe_sweep_feishu_images(now: datetime) -> None:
    """M8d：距上次清扫 ≥1 小时才扫一次 feishu_imgs/ 过期图片；保洁失败只记日志，绝不影响调度主循环。"""
    global _last_img_sweep
    if _last_img_sweep is not None and now - _last_img_sweep < _IMG_SWEEP_INTERVAL:
        return
    _last_img_sweep = now
    try:
        async with SessionLocal() as s:
            n = await sweep_feishu_images(s)
            if n:
                logger.info("feishu_imgs sweep removed %s expired file(s)", n)
    except Exception:
        logger.exception("feishu_imgs sweep failed")


async def sweep_feishu_images_on_startup() -> None:
    """M8d：进程启动时立即清扫一次（覆盖刚部署/长期不重启后重启的场景）。"""
    await _maybe_sweep_feishu_images(datetime.now(UTC))


async def _maybe_sweep_wecom_messages(now: datetime) -> None:
    """M31.2：距上次 ≥1 小时按保留策略清扫过期企微消息（默认永久=0 天不清理）；保洁失败只记日志。"""
    global _last_wecom_sweep
    if _last_wecom_sweep is not None and now - _last_wecom_sweep < _WECOM_SWEEP_INTERVAL:
        return
    _last_wecom_sweep = now
    try:
        from app.wecom_archive.sync import sweep_expired_messages  # 延迟 import：未配置环境零成本

        async with SessionLocal() as s:
            n = await sweep_expired_messages(s)
            if n:
                logger.info("wecom archive retention sweep removed %s expired message(s)", n)
    except Exception:
        logger.exception("wecom archive retention sweep failed")


async def sweep_wecom_messages_on_startup() -> None:
    """M31.2：进程启动时立即清扫一次（管理员调小保留天数后重启即生效，不等下一小时）。"""
    await _maybe_sweep_wecom_messages(datetime.now(UTC))


async def scheduler_loop() -> None:
    """tick 循环：自身异常只记日志，绝不退出（否则调度静默死亡）。"""
    while True:
        try:
            await _tick()
        except Exception:
            logger.exception("scheduler tick failed")
        await _maybe_sweep_feishu_images(datetime.now(UTC))
        await _maybe_sync_wecom_archive(datetime.now(UTC))
        await _maybe_sweep_wecom_messages(datetime.now(UTC))
        await asyncio.sleep(TICK_SECONDS)


async def sweep_stale_running() -> None:
    """启动时自愈：上次进程硬杀留下的 last_status="running" 残留扫成 error。"""
    async with SessionLocal() as s:
        stale = (
            (await s.execute(select(ScheduledTask).where(ScheduledTask.last_status == "running")))
            .scalars()
            .all()
        )
        for t in stale:
            t.last_status = "error"
            t.last_error = "服务重启，上一次运行被打断"
        if stale:
            await s.commit()


async def _tick() -> None:
    now = datetime.now(UTC)
    async with SessionLocal() as s:
        due = (
            (
                await s.execute(
                    select(ScheduledTask).where(
                        ScheduledTask.enabled.is_(True),
                        ScheduledTask.deleted_at.is_(None),
                        ScheduledTask.next_run_at.is_not(None),
                        ScheduledTask.next_run_at <= now,
                    )
                )
            )
            .scalars()
            .all()
        )
        for t in due:
            if t.id in _INFLIGHT:
                continue  # 等不跳：不推进 next_run_at，下 tick 再捡起来
            # 关键顺序：先推进并提交，再执行——即使执行崩溃/进程重启也不会重复触发
            t.next_run_at = compute_next(t.cron_expr, t.timezone, now)
            t.last_run_at = now
            t.last_status = "running"
            t.last_error = None
            t.run_count += 1
            await s.commit()
            _INFLIGHT.add(t.id)
            asyncio.create_task(_execute_task(t.id))


async def _fail_early(s, task: ScheduledTask, error: str) -> None:
    """前置校验失败：记录 error，不建会话（任务保持启用，由用户处置）。"""
    task.last_status = "error"
    task.last_error = error[:500]
    await s.commit()


async def _execute_task(task_id: int) -> None:
    """执行一次定时任务：以属主身份建独立会话，跑完整 agent loop，结果回写任务行。"""
    try:
        async with SessionLocal() as s:
            task = await s.get(ScheduledTask, task_id)
            if task is None or task.deleted_at is not None:
                return
            user = await s.get(User, task.user_id)
            agent = await s.get(Agent, task.agent_id)
            if user is None or user.status != "active":
                await _fail_early(s, task, "属主账号不可用")
                return
            if agent is None or agent.deleted_at is not None or agent.status != "active":
                await _fail_early(s, task, "数字人已被删除或停用")
                return
            try:
                _check_chat_access(agent, user)
            except Exception as e:
                await _fail_early(s, task, f"无权限使用该数字人：{getattr(e, 'detail', e)}")
                return

            # 模型解析与配额检查：完全照抄 send_message 的规则
            model_id = task.model_id or agent.default_model_id
            if model_id is None:
                await _fail_early(s, task, "该数字人未配置默认模型")
                return
            allowlist: list[int] | None = json.loads(agent.model_allowlist) if agent.model_allowlist else None
            if allowlist is not None and model_id not in allowlist:
                await _fail_early(s, task, "配置的模型不在此数字人的允许列表中")
                return
            model = (
                await s.execute(select(Model).where(Model.id == model_id, Model.deleted_at.is_(None), Model.enabled.is_(True)))
            ).scalar_one_or_none()
            if model is None:
                await _fail_early(s, task, "模型不存在或已下线")
                return
            provider = (
                await s.execute(
                    select(ModelProvider).where(ModelProvider.id == model.provider_id, ModelProvider.deleted_at.is_(None), ModelProvider.enabled.is_(True))
                )
            ).scalar_one_or_none()
            if provider is None:
                await _fail_early(s, task, "模型供应商不可用")
                return
            try:
                await check_model_access(s, user, model)
                await check_quota(s, user)
            except GatewayError as e:
                await _fail_early(s, task, str(e))
                return

            now = datetime.now(UTC)
            conv = Conversation(
                user_id=user.id,
                agent_id=agent.id,
                title=f"定时任务：{task.name} {now.astimezone(ZoneInfo(task.timezone)):%m-%d %H:%M}",
                channel="cron",
            )
            s.add(conv)
            await s.flush()
            s.add(Message(conversation_id=conv.id, role="user", content=task.prompt))
            task.last_conversation_id = conv.id
            await s.commit()
            cid = conv.id

        # 注册进 _ACTIVE_TURNS/_ACTIVE_TASKS（send_message 同款）：前端打开该会话时
        # 恢复轮询显示"仍在处理中"并在跑完自动出现回复，/cancel 端点也能停它。
        queue: asyncio.Queue = asyncio.Queue()
        _ACTIVE_TURNS.add(cid)
        turn = asyncio.create_task(
            _run_turn_background(user_id=user.id, agent_id=agent.id, conversation_id=cid, model_id=model.id, queue=queue)
        )
        _ACTIVE_TASKS[cid] = {
            "task": turn,
            "started_at": now,
            "agent_id": agent.id,
            "user_id": user.id,
            "model_id": model.id,
        }

        errors: list[str] = []
        while True:
            event = await queue.get()
            if event is None:
                break
            if isinstance(event, dict) and event.get("error"):
                errors.append(str(event["error"]))

        async with SessionLocal() as s:
            task = await s.get(ScheduledTask, task_id)
            if task is not None:
                task.last_status = "error" if errors else "ok"
                task.last_error = errors[0][:500] if errors else None
                await s.commit()

                # M4b/M8：推送二选一——push_to_me=应用机器人私聊推本人（个人摘要私密）；
                # feishu_webhook=推群自定义机器人。推送失败只记 last_push_error，绝不影响运行状态
                if task.push_to_me or task.feishu_webhook:
                    if errors:
                        push_title, push_body, push_ok = f"【{task.name}】运行失败", task.last_error, False
                    else:
                        last_msg = (
                            await s.execute(
                                select(Message)
                                .where(Message.conversation_id == cid, Message.role == "assistant", Message.content.is_not(None))
                                .order_by(Message.id.desc())
                                .limit(1)
                            )
                        ).scalar_one_or_none()
                        push_title = f"【{task.name}】运行成功"
                        push_body = last_msg.content if last_msg else "(本轮无文本回复)"
                        push_ok = True
                    if task.push_to_me:
                        owner = await s.get(User, task.user_id)
                        open_id = owner.feishu_open_id if owner else None
                        if not open_id:
                            err = "任务属主未绑定飞书账号（先用飞书登录一次平台）"
                        else:
                            err = await push_feishu_card_to_me(open_id, push_title, push_body, ok=push_ok)
                    else:
                        err = await push_feishu_card(task.feishu_webhook, push_title, push_body, task.feishu_secret, ok=push_ok)
                    task.last_push_error = err
                    if task.last_push_error:
                        logger.warning("scheduled task %s feishu push failed: %s", task_id, task.last_push_error)
                    await s.commit()
    except Exception:
        logger.exception("scheduled task %s execution failed", task_id)
        try:
            async with SessionLocal() as s:
                task = await s.get(ScheduledTask, task_id)
                if task is not None and task.last_status == "running":
                    task.last_status = "error"
                    task.last_error = "执行异常（详见服务端日志）"
                    await s.commit()
        except Exception:
            logger.exception("scheduled task %s status writeback failed", task_id)
    finally:
        _INFLIGHT.discard(task_id)
