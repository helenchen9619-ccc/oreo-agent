"""M31：敏感词告警——落库时匹配 wecom_keywords，命中即飞书卡片推给接收人。

M31.1：接收人可配置（wecom_alert_receivers 表，管理端维护）；表为空时回落全体绑定飞书的
admin（兼容 M31 行为）。表非空但名单里无人能推（停用/未绑飞书）时同样回落 admin——
宁可多推不可漏推。

安全纪律：告警卡片是监控目的的正当输出（含消息摘要）；审计只记命中词/会话/发送人，不落正文。
"""
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import AuditLog, User
from app.models.wecom import WecomAlertReceiver, WecomKeyword
from app.scheduler.feishu_push import push_feishu_card_to_me

logger = logging.getLogger(__name__)

_TZ = ZoneInfo("Asia/Shanghai")
_SUMMARY_MAX = 200  # 卡片里消息摘要截断长度


async def load_keywords(db: AsyncSession) -> list[str]:
    rows = (await db.execute(select(WecomKeyword).where(WecomKeyword.deleted_at.is_(None)))).scalars().all()
    return [r.word for r in rows if r.word]


async def _feishu_admins(db: AsyncSession) -> list[User]:
    """全体绑定飞书的 admin（回落路径）。"""
    return (
        (
            await db.execute(
                select(User).where(
                    User.role == "admin",
                    User.deleted_at.is_(None),
                    User.feishu_open_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )


async def _alert_recipients(db: AsyncSession) -> list[User]:
    """告警接收人解析：名单表非空 → 推名单内绑定飞书者；表空或无人能推 → 回落全体 admin。"""
    rows = (
        (await db.execute(select(WecomAlertReceiver).where(WecomAlertReceiver.deleted_at.is_(None))))
        .scalars()
        .all()
    )
    if not rows:
        return await _feishu_admins(db)
    users = (
        (
            await db.execute(
                select(User).where(
                    User.id.in_([r.user_id for r in rows]),
                    User.deleted_at.is_(None),
                    User.feishu_open_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if users:
        return users
    logger.warning("企微告警接收人名单非空但无人能推（停用/未绑飞书），回落全体 admin")
    return await _feishu_admins(db)


async def check_keywords(
    db: AsyncSession,
    keywords: list[str],
    *,
    msgid: str,
    sender_userid: str,
    sender_name: str,
    room_name: str,
    msgtime_ms: int,
    text: str,
    roomid: str = "",
) -> list[str]:
    """对一条新落库消息做敏感词匹配；命中则推告警 + 写审计。返回命中的词列表（测试断言用）。"""
    if not text or not keywords:
        return []
    hits = sorted({w for w in keywords if w in text})
    if not hits:
        return []
    admins = await _alert_recipients(db)
    if not admins:
        logger.warning("企微敏感词命中但没有可推送的 admin（未绑定飞书）：words=%s", hits)
        return hits
    when = datetime.fromtimestamp(msgtime_ms / 1000, tz=_TZ).strftime("%Y-%m-%d %H:%M:%S")
    # 会话标签三态（M31.3）：群名解析不出（外部群无接口）≠ 私聊——roomid 非空就是群聊
    if room_name:
        where = f"群「{room_name}」"
        room_tag = room_name
    elif roomid:
        where = "群聊（群名未解析）"
        room_tag = f"群聊:{roomid}"
    else:
        where = "私聊/单聊"
        room_tag = "私聊"
    summary = text[:_SUMMARY_MAX] + ("…" if len(text) > _SUMMARY_MAX else "")
    card = f"命中词：{'、'.join(hits)}\n发送人：{sender_name}（{sender_userid}）\n会话：{where}\n时间：{when}\n内容：{summary}"
    for admin in admins:
        err = await push_feishu_card_to_me(admin.feishu_open_id, "⚠️ 企微敏感词命中", card, ok=False)
        if err:
            logger.warning("企微敏感词告警推送失败 admin=%s: %s", admin.id, err)
    # 审计不落正文：只记命中词/会话/发送人
    db.add(
        AuditLog(
            actor_user_id=admins[0].id,
            action="wecom.keyword_hit",
            target_type="wecom_message",
            target_id=msgid,
            detail=json.dumps({"keywords": hits, "sender_userid": sender_userid, "room": room_tag}, ensure_ascii=False),
        )
    )
    return hits
