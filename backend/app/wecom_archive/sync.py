"""M31：企微会话存档同步服务——seq 增量拉取 → RSA 解随机密钥 → SDK 解密 → 明文落库 + 敏感词匹配。

挂法：scheduler.engine 每 30s tick 调一次（内部 1 分钟节流），lifespan 启动即跑一次。
阻塞的 SDK 调用全部走 _sdk_run 固定到单线程执行器（线程亲和，见下方事故教训）；
异常上抛给调用方（engine 兜底记日志，绝不弄挂主循环）。
未配置（本地开发/测试）时 no-op 返回 None。
"""
import asyncio
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.system import SystemSetting
from app.models.wecom import WecomMessage
from app.wecom_archive.alerts import check_keywords, load_keywords
from app.wecom_archive.maps import display_names, refresh_contacts, resolve_room_name
from app.wecom_archive.sdk import WecomSdk, decrypt_random_key

logger = logging.getLogger(__name__)

_SEQ_KEY = "wecom_archive_seq"
_PAGE_LIMIT = 100   # GetChatData 单页上限
_ROUND_MAX = 1000   # 单轮最多落库条数（防一轮跑太久；积压由后续轮次追平）
_CONTACT_REFRESH_INTERVAL = timedelta(hours=6)  # 通讯录映射刷新节流

# 2026-09-23 云上事故教训：企微 SDK 线程亲和——Init/GetChatData/DecryptData 必须落在同一条线程。
# asyncio.to_thread 走默认池、每次可能换线程，跨线程触碰 SDK 内部状态直接堆损坏
# （free(): invalid pointer，SIGABRT 进程级崩溃，Python 捕不到）。故所有 SDK 调用固定走这个单线程执行器，
# 且 SDK 实例常驻复用（不每轮 Init/Destroy，减少原生层折腾）。
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wecom-archive")
_sdk_instance: "WecomSdk | None" = None

_last_contact_refresh: datetime | None = None
# M31.2：通讯录刷新结果内存态（/status 暴露，48009 类失败不再只能翻日志；不含 secret）
_contact_status: dict = {"at": None, "ok": None, "error": ""}
_CONTACT_FAIL_RETRY = timedelta(minutes=10)  # 失败后 10 分钟重试（成功才走 6 小时节流）
_priv_key_cache: dict[str, bytes] = {}  # path -> pem bytes（私钥只从服务器文件读，永不进日志）

# 附件类消息本期只存占位（sdkfileid 拉附件二期）；撤回/action=recall 单独标注
_PLACEHOLDER = {
    "image": "[图片]",
    "emotion": "[表情]",
    "voice": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "mixed": "[图文混合消息]",
    "weapp": "[小程序]",
    "card": "[名片]",
    "location": "[位置]",
    "meeting": "[会议]",
    "docmsg": "[文档消息]",
    "markdown": "[markdown 消息]",
    "news": "[图文消息]",
}


def msg_to_row(msg: dict, seq: int) -> WecomMessage:
    """解密后的明文 JSON → ORM 行（纯函数，可单测）。"""
    msgtype = msg.get("msgtype") or ""
    action = msg.get("action") or "send"
    if action == "recall" or msgtype == "revoke":
        msgtype = msgtype or "revoke"
        text = "[撤回了一条消息]"
    elif msgtype == "text":
        text = (msg.get("text") or {}).get("content") or ""
    elif msgtype == "link":
        text = "[链接] " + ((msg.get("link") or {}).get("title") or "")
    else:
        text = _PLACEHOLDER.get(msgtype, f"[{msgtype or '未知消息类型'}]")
    return WecomMessage(
        seq=seq,
        msgid=msg.get("msgid") or "",
        action=action,
        sender_userid=msg.get("from") or "",
        receivers=json.dumps(msg.get("tolist") or [], ensure_ascii=False),
        roomid=msg.get("roomid") or "",
        msgtype=msgtype,
        text=text,
        msgtime=int(msg.get("msgtime") or 0),
    )


def _decrypt_page(sdk: WecomSdk, priv_pem: bytes, items: list[dict], pubkey_ver: int) -> tuple[list[tuple[int, dict]], int]:
    """同步函数（_EXECUTOR 线程里跑）：一页密文 → [(seq, 明文 dict)] + 版本不符跳过数。

    单条解密失败只跳过该条并记日志（毒消息不能卡死游标），正文绝不进日志。
    """
    out: list[tuple[int, dict]] = []
    skipped_ver = 0
    for it in items:
        seq = int(it.get("seq") or 0)
        if int(it.get("publickey_ver") or 0) != pubkey_ver:
            skipped_ver += 1  # 换公钥前的历史消息永不可解，只前进游标
            continue
        try:
            rand_key = decrypt_random_key(priv_pem, it["encrypt_random_key"])
            out.append((seq, sdk.decrypt_data(rand_key, it["encrypt_chat_msg"])))
        except Exception as e:  # noqa: BLE001 - 单条失败不阻断整页
            logger.warning("wecom archive decrypt failed seq=%s: %s", seq, e)
    return out, skipped_ver


async def _get_seq(db: AsyncSession) -> int:
    row = (
        await db.execute(select(SystemSetting).where(SystemSetting.key == _SEQ_KEY, SystemSetting.deleted_at.is_(None)))
    ).scalar_one_or_none()
    try:
        return int(row.value) if row and row.value else 0
    except ValueError:
        return 0


async def _set_seq(db: AsyncSession, seq: int) -> None:
    row = (
        await db.execute(select(SystemSetting).where(SystemSetting.key == _SEQ_KEY, SystemSetting.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if row is None:
        db.add(SystemSetting(key=_SEQ_KEY, value=str(seq)))
    else:
        row.value = str(seq)


def _private_key_pem(path: str) -> bytes:
    pem = _priv_key_cache.get(path)
    if pem is None:
        with open(path, "rb") as f:
            pem = f.read()
        _priv_key_cache[path] = pem
    return pem


def _ensure_sdk(so_path: str, corpid: str, secret: str) -> WecomSdk:
    """在 _EXECUTOR 线程里懒建并常驻 SDK 实例（线程亲和纪律：只能在这里碰它）。"""
    global _sdk_instance
    if _sdk_instance is None:
        _sdk_instance = WecomSdk(so_path, corpid, secret)
    return _sdk_instance


async def _sdk_run(fn, *args):
    """一切 SDK 触碰的唯一入口：固定在 _EXECUTOR 那条线程上执行。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXECUTOR, lambda: fn(*args))


async def sync_wecom_archive(db: AsyncSession, sdk=None) -> dict | None:
    """跑一轮增量同步。返回统计 dict；未配置/无 SDK 文件时返回 None（no-op）。

    sdk 参数供测试注入假 SDK；生产默认 None=真实 WecomSdk。
    """
    s = get_settings()
    if not getattr(s, "WECOM_ARCHIVE_SYNC_ENABLED", True):
        return None
    if not (s.WECOM_CORPID and s.WECOM_ARCHIVE_SECRET):
        return None
    if sdk is None and not os.path.exists(s.WECOM_ARCHIVE_SO_PATH):
        logger.debug("wecom archive sdk not found at %s, sync skipped", s.WECOM_ARCHIVE_SO_PATH)
        return None

    # SDK 实例常驻复用（线程亲和，见文件头事故教训）；进程退出时随进程回收，不逐轮 DestroySdk
    if sdk is None:
        sdk = await _sdk_run(_ensure_sdk, s.WECOM_ARCHIVE_SO_PATH, s.WECOM_CORPID, s.WECOM_ARCHIVE_SECRET)
    pubkey_ver = int(s.WECOM_ARCHIVE_PUBKEY_VER or "2")
    priv_pem = _private_key_pem(s.WECOM_ARCHIVE_KEY_PATH)
    seq = await _get_seq(db)
    keywords = await load_keywords(db)
    stats = {"pulled": 0, "inserted": 0, "skipped_ver": 0, "hits": 0}

    while stats["pulled"] < _ROUND_MAX:
        items = await _sdk_run(sdk.get_chat_data, seq, _PAGE_LIMIT)
        if not items:
            break
        stats["pulled"] += len(items)
        decrypted, skipped = await _sdk_run(_decrypt_page, sdk, priv_pem, items, pubkey_ver)
        stats["skipped_ver"] += skipped
        seq = max(int(it.get("seq") or 0) for it in items)  # 含跳过/失败条，游标照前进

        if decrypted:
            existing = set(
                (
                    await db.execute(
                        select(WecomMessage.msgid).where(WecomMessage.msgid.in_([m.get("msgid") or "" for _, m in decrypted]))
                    )
                )
                .scalars()
                .all()
            )
            names = await display_names(db, list({(m.get("from") or "") for _, m in decrypted}))
            for msg_seq, msg in decrypted:
                row = msg_to_row(msg, msg_seq)
                if not row.msgid or row.msgid in existing:
                    continue
                db.add(row)
                stats["inserted"] += 1
                room_name = ""
                if row.roomid:
                    room_name = await resolve_room_name(db, s.WECOM_CORPID, s.WECOM_ARCHIVE_SECRET, row.roomid)
                hits = await check_keywords(
                    db,
                    keywords,
                    msgid=row.msgid,
                    sender_userid=row.sender_userid,
                    sender_name=names.get(row.sender_userid, row.sender_userid),
                    room_name=room_name,
                    msgtime_ms=row.msgtime,
                    text=row.text,
                    roomid=row.roomid,  # M31.3：群名解析不出≠私聊，告警标签要用 roomid 判群
                )
                stats["hits"] += 1 if hits else 0

        await _set_seq(db, seq)
        await db.commit()
        if len(items) < _PAGE_LIMIT:
            break  # 最后一页

    # 通讯录映射 6 小时节流刷新（随同步顺带做，姓名供告警/Phase 2 查询显示）
    global _last_contact_refresh
    now = datetime.now(UTC)
    if s.WECOM_CONTACT_SECRET and (_last_contact_refresh is None or now - _last_contact_refresh >= _CONTACT_REFRESH_INTERVAL):
        try:
            n = await refresh_contacts(db, s.WECOM_CORPID, s.WECOM_CONTACT_SECRET)
            await db.commit()
            _last_contact_refresh = now  # M31.2：成功才走 6 小时节流（先置位会让 48009 类失败干等 6 小时）
            _contact_status.update({"at": now.isoformat(), "ok": True, "error": ""})
            logger.info("wecom contacts refreshed: %s 人", n)
        except Exception as e:  # noqa: BLE001 - 通讯录失败不阻断消息同步
            _last_contact_refresh = now - _CONTACT_REFRESH_INTERVAL + _CONTACT_FAIL_RETRY  # 失败 10 分钟后重试
            _contact_status.update({"at": now.isoformat(), "ok": False, "error": str(e)[:200]})
            logger.exception("wecom contacts refresh failed")

    stats["last_seq"] = seq
    return stats


# ---------- M31.2 保留策略（默认永久；管理员界面可配天数，清扫挂 scheduler/engine.py） ----------

_RETENTION_KEY = "wecom_retention_days"


async def get_retention_days(db: AsyncSession) -> int:
    """保留天数；0/未配置/非法值=永久不清理（router 端点与清扫任务共用唯一实现）。"""
    row = (
        await db.execute(select(SystemSetting.value).where(SystemSetting.key == _RETENTION_KEY, SystemSetting.deleted_at.is_(None)))
    ).scalar_one_or_none()
    try:
        return max(0, int(row)) if row else 0
    except (TypeError, ValueError):
        return 0


async def sweep_expired_messages(db: AsyncSession) -> int:
    """按保留策略物理删除过期消息（过期数据不留软删尸）。0 天=永久，直接 no-op。返回删除条数。"""
    days = await get_retention_days(db)
    if days <= 0:
        return 0
    cutoff_ms = int((datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000)
    res = await db.execute(delete(WecomMessage).where(WecomMessage.msgtime < cutoff_ms))
    await db.commit()
    return res.rowcount or 0
