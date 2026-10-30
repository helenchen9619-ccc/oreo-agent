"""M31：企微存档的映射表——userid→姓名（通讯录 secret）+ roomid→群名（存档 secret）。

两个 access_token 各管各的：通讯录 API 用 WECOM_CONTACT_SECRET，群信息接口 msgaudit/groupchat/get
用 WECOM_ARCHIVE_SECRET（会话存档应用的 secret 才能调）。token 内存缓存 7000s。
"""
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.wecom import WecomContact, WecomRoom

logger = logging.getLogger(__name__)

_BASE = "https://qyapi.weixin.qq.com"
_TOKEN_TTL_S = 7000  # 官方 token 有效期 7200s，提前 200s 续
_tokens: dict[str, tuple[str, float]] = {}  # secret -> (token, 过期 monotonic 时间)
_ROOM_RETRY_INTERVAL = timedelta(hours=24)  # M31.3：群名解析失败的负缓存 24h 后重试（原永不重试）


async def _access_token(corpid: str, secret: str) -> str:
    """按 secret 缓存的 access_token（通讯录和存档是两个 secret）。"""
    cached = _tokens.get(secret)
    if cached and time.monotonic() < cached[1]:
        return cached[0]
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{_BASE}/cgi-bin/gettoken", params={"corpid": corpid, "corpsecret": secret})
        data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"企微 gettoken 失败 errcode={data.get('errcode')} errmsg={data.get('errmsg')}")
    _tokens[secret] = (data["access_token"], time.monotonic() + _TOKEN_TTL_S)
    return data["access_token"]


async def refresh_contacts(db: AsyncSession, corpid: str, contact_secret: str) -> int:
    """通讯录全量刷新（department_id=1 + fetch_child=1 递归拉全员），upsert wecom_contacts。返回映射总数。"""
    token = await _access_token(corpid, contact_secret)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{_BASE}/cgi-bin/user/list",
            params={"access_token": token, "department_id": 1, "fetch_child": 1},
        )
        data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"通讯录 user/list 失败 errcode={data.get('errcode')} errmsg={data.get('errmsg')}")
    existing = {
        c.userid: c
        for c in (await db.execute(select(WecomContact).where(WecomContact.deleted_at.is_(None)))).scalars().all()
    }
    n = 0
    for u in data.get("userlist") or []:
        uid, name = u.get("userid") or "", u.get("name") or ""
        if not uid:
            continue
        n += 1
        row = existing.get(uid)
        if row is None:
            db.add(WecomContact(userid=uid, name=name))
        elif row.name != name:
            row.name = name  # 改名跟随
    return n


async def resolve_room_name(db: AsyncSession, corpid: str, archive_secret: str, roomid: str) -> str:
    """roomid→群名：先查映射表，未收录则调 msgaudit/groupchat/get（仅内部群有群名；
    外部非客户群无 API，errcode!=0 时落空串占位——显示侧回落 roomid）。返回群名或 ""。

    M31.3：空串是负缓存不是终态——缓存行 roomname 为空且距今 >24h 会重试一次
    （群后来可解析/群名后补都能自愈），重试无论成败都刷新 updated_at 重新武装 TTL。
    """
    row = (
        await db.execute(select(WecomRoom).where(WecomRoom.roomid == roomid, WecomRoom.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if row is not None:
        ua = row.updated_at
        if ua is not None and ua.tzinfo is None:
            ua = ua.replace(tzinfo=UTC)  # naive 时间戳按 UTC 解释（orm 落库惯例）
        stale = not row.roomname and (ua is None or datetime.now(UTC) - ua > _ROOM_RETRY_INTERVAL)
        if not stale:
            return row.roomname
    roomname = ""
    try:
        token = await _access_token(corpid, archive_secret)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{_BASE}/cgi-bin/msgaudit/groupchat/get", params={"access_token": token}, json={"roomid": roomid})
            data = resp.json()
        if data.get("errcode") == 0:
            roomname = data.get("roomname") or ""
        else:
            logger.info("wecom roomname lookup errcode=%s（外部群无接口属预期） roomid=%s", data.get("errcode"), roomid[:8])
    except Exception:  # noqa: BLE001 - 群名是锦上添花，失败不阻断落库
        logger.exception("wecom roomname lookup failed")
    if row is None:
        db.add(WecomRoom(roomid=roomid, roomname=roomname))
    else:
        row.roomname = roomname
        row.updated_at = datetime.now(UTC)  # 显式置位：roomname 空→空不算 dirty，onupdate 不会自动刷
    return roomname


async def display_names(db: AsyncSession, userids: list[str]) -> dict[str, str]:
    """批量 userid→姓名（查不到的不进结果，调用方回落 userid 原样）。"""
    if not userids:
        return {}
    rows = (
        await db.execute(select(WecomContact).where(WecomContact.userid.in_(userids), WecomContact.deleted_at.is_(None)))
    ).scalars().all()
    return {r.userid: r.name for r in rows}


async def room_names(db: AsyncSession, roomids: list[str]) -> dict[str, str]:
    """M31.1：批量 roomid→群名（消息列表显示用；查不到的不进结果，调用方回落 roomid 原样）。"""
    if not roomids:
        return {}
    rows = (
        await db.execute(select(WecomRoom).where(WecomRoom.roomid.in_(roomids), WecomRoom.deleted_at.is_(None)))
    ).scalars().all()
    return {r.roomid: r.roomname for r in rows}
