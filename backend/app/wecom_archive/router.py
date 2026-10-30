"""M31：企微会话存档管理端——同步状态查看 + 敏感词维护（仅 admin；范式照抄 announcements/router.py）。

M31.1：消息查看界面 + 授权查看人 + 可配置告警接收人。
- require_wecom_viewer：admin 直通，否则查 wecom_viewers；非授权用户 403；
- GET /messages 是仓库首个分页端点；审计只在 page==1 留一条痕（筛选条件+条数，不落正文）；
- viewers/alert-receivers 的增删走「复活或新建」（user_id 是 int，不用关键词的软删改名套路）。

M31.2：消息导出 xlsx（/messages/export，跟随筛选、超 5 万条 400）+ 可配置保留策略
（system_settings 键 wecom_retention_days，0=永久；清扫在 scheduler/engine.py）+
/status 暴露通讯录刷新结果（48009 类失败不再只能翻日志）。

M31.4：外部人/外部群手工别名（PUT /rooms/{roomid}、PUT /contacts/{userid}、
GET /contacts/unknown 待认领清单）——wm/wr 开头的外部对象企微不开放接口，
管理员认一次名，显示/筛选/导出链路零改动自动生效。

M31.5：授权查看人开放面扩大——状态/关键词（可读写）/映射（可读写）/保留天数（只读）
均改 require_wecom_viewer；仍 admin 专属：PUT /retention、/viewers、/alert-receivers。
"""
import json
import os
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.deps import get_current_user, require_admin
from app.models.base import utcnow
from app.models.system import SystemSetting
from app.models.user import AuditLog, User
from app.models.wecom import WecomAlertReceiver, WecomContact, WecomKeyword, WecomMessage, WecomRoom, WecomViewer
from app.wecom_archive import maps
from app.wecom_archive.sync import _RETENTION_KEY, _SEQ_KEY, get_retention_days

router = APIRouter(prefix="/api/admin/wecom", tags=["wecom-archive"])


async def _audit(db: AsyncSession, actor: User, action: str, target_id: object, detail: dict, target_type: str = "wecom_keyword") -> None:
    db.add(AuditLog(actor_user_id=actor.id, action=action, target_type=target_type, target_id=str(target_id), detail=json.dumps(detail, ensure_ascii=False)))


async def _count(db: AsyncSession, model) -> int:
    return (await db.execute(select(func.count(model.id)).where(model.deleted_at.is_(None)))).scalar_one()


# ---------- M31.1 门禁：授权查看人（admin 直通，否则查 wecom_viewers） ----------
# 定义必须先于一切使用它的端点（Depends 在装饰期求值）。
# M31.5 开放面：viewer 可读状态/保留天数、可读写关键词与别名映射；
# 仍 admin 专属：PUT /retention、/viewers、/alert-receivers。


async def _is_viewer(db: AsyncSession, user: User) -> bool:
    row = (
        await db.execute(select(WecomViewer).where(WecomViewer.user_id == user.id, WecomViewer.deleted_at.is_(None)))
    ).scalar_one_or_none()
    return row is not None


async def require_wecom_viewer(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> User:
    """admin 或授权查看人；授权管理/保留策略写端点仍 require_admin。"""
    if user.role == "admin":
        return user
    if not await _is_viewer(db, user):
        raise HTTPException(403, "你未被授权查看企微存档消息，请联系管理员开通")
    return user


@router.get("/access")
async def wecom_access(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    """前端导航可见性用：admin=是否管理员，viewer=是否可看消息（admin 恒真）。"""
    return {"admin": user.role == "admin", "viewer": user.role == "admin" or await _is_viewer(db, user)}


@router.get("/status")
async def wecom_status(db: AsyncSession = Depends(get_db), _: User = Depends(require_wecom_viewer)) -> dict:
    """同步状态总览（管理端页面顶部卡片行）。"""
    from app.wecom_archive import sync as _sync  # 延迟 import 读 _contact_status 内存态

    s = get_settings()
    seq_row = (
        await db.execute(select(SystemSetting).where(SystemSetting.key == _SEQ_KEY, SystemSetting.deleted_at.is_(None)))
    ).scalar_one_or_none()
    last_msg = (
        await db.execute(select(func.max(WecomMessage.created_at)).where(WecomMessage.deleted_at.is_(None)))
    ).scalar_one()
    return {
        "configured": bool(s.WECOM_CORPID and s.WECOM_ARCHIVE_SECRET),
        "sdk_present": os.path.exists(s.WECOM_ARCHIVE_SO_PATH),
        "contact_secret": bool(s.WECOM_CONTACT_SECRET),
        "last_seq": int(seq_row.value) if seq_row and seq_row.value.isdigit() else 0,
        "last_sync_at": last_msg.isoformat() if last_msg else None,
        "message_count": await _count(db, WecomMessage),
        "keyword_count": await _count(db, WecomKeyword),
        "contact_count": await _count(db, WecomContact),
        "room_count": await _count(db, WecomRoom),
        # M31.2：上次通讯录刷新结果（48009 类权限失败直接可见，不用翻日志）
        "contact_sync": dict(_sync._contact_status),
    }


def _kw_out(k: WecomKeyword) -> dict:
    return {"id": k.id, "word": k.word, "created_at": k.created_at.isoformat() if k.created_at else None}


@router.get("/keywords")
async def list_keywords(db: AsyncSession = Depends(get_db), _: User = Depends(require_wecom_viewer)) -> list[dict]:
    rows = (
        (await db.execute(select(WecomKeyword).where(WecomKeyword.deleted_at.is_(None)).order_by(WecomKeyword.id.desc())))
        .scalars()
        .all()
    )
    return [_kw_out(k) for k in rows]


class KeywordIn(BaseModel):
    word: str = Field(min_length=1, max_length=128)


@router.post("/keywords", status_code=201)
async def create_keyword(body: KeywordIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_wecom_viewer)) -> dict:
    word = body.word.strip()
    if not word:
        raise HTTPException(400, "敏感词不能为空")
    dup = (
        await db.execute(select(WecomKeyword).where(WecomKeyword.word == word, WecomKeyword.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(409, f"敏感词「{word}」已存在")
    k = WecomKeyword(word=word)
    db.add(k)
    await db.flush()
    await _audit(db, admin, "wecom.keyword_create", k.id, {"word": word})
    await db.commit()
    return _kw_out(k)


@router.delete("/keywords/{kid}", status_code=204)
async def delete_keyword(kid: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_wecom_viewer)) -> None:
    k = (
        await db.execute(select(WecomKeyword).where(WecomKeyword.id == kid, WecomKeyword.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if k is None:
        raise HTTPException(404, "敏感词不存在")
    old = k.word
    k.deleted_at = utcnow()
    k.word = f"{old}#deleted-{kid}"[:128]  # 软删占唯一约束：改名释放（M9 技能导入同款教训）
    await _audit(db, admin, "wecom.keyword_delete", kid, {"word": old})
    await db.commit()


# ---------- M31.1 消息列表（仓库首个分页端点） ----------


def _like(s: str) -> str:
    r"""LIKE 模糊匹配转义（\ % _），两边加 %。"""
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def _message_conds(db: AsyncSession, sender, room, keyword, start, end) -> list:
    """M31.2 抽出共用：消息筛选条件（列表与导出同源，所见即所得）。
    sender 框同时认姓名模糊（通讯录映射）和 userid 精确；
    room=dm 私聊 / group 任意群 / 其余按群名模糊+roomid 精确；时间窗按毫秒闭区间。"""
    conds = [WecomMessage.deleted_at.is_(None)]
    if sender and sender.strip():
        s = sender.strip()
        ids = {
            r.userid
            for r in (
                await db.execute(
                    select(WecomContact).where(WecomContact.name.like(_like(s), escape="\\"), WecomContact.deleted_at.is_(None))
                )
            ).scalars().all()
        }
        ids.add(s)  # 原值并入：直接按 userid 精确匹配
        conds.append(WecomMessage.sender_userid.in_(ids))
    if room and room.strip():
        r = room.strip()
        if r == "dm":
            conds.append(WecomMessage.roomid == "")
        elif r == "group":
            conds.append(WecomMessage.roomid != "")
        else:
            ids = {
                x.roomid
                for x in (
                    await db.execute(
                        select(WecomRoom).where(WecomRoom.roomname.like(_like(r), escape="\\"), WecomRoom.deleted_at.is_(None))
                    )
                ).scalars().all()
            }
            ids.add(r)
            conds.append(WecomMessage.roomid.in_(ids))
    if keyword and keyword.strip():
        conds.append(WecomMessage.text.like(_like(keyword.strip()), escape="\\"))
    if start is not None:
        conds.append(WecomMessage.msgtime >= int(start.timestamp() * 1000))
    if end is not None:
        conds.append(WecomMessage.msgtime <= int(end.timestamp() * 1000))
    return conds


async def _name_maps(db: AsyncSession, rows) -> tuple[dict, dict, dict]:
    """姓名/群名内存批量映射（receivers 是 JSON，只能内存解）。返回 (人名映射, 群名映射, {消息id: 接收人userid列表})。"""
    userids = {m.sender_userid for m in rows}
    receiver_lists = {}
    for m in rows:
        try:
            receiver_lists[m.id] = json.loads(m.receivers) if m.receivers else []
        except (TypeError, ValueError):
            receiver_lists[m.id] = []
        userids.update(receiver_lists[m.id])
    names = await maps.display_names(db, sorted(userids))
    rnames = await maps.room_names(db, sorted({m.roomid for m in rows if m.roomid}))
    return names, rnames, receiver_lists


@router.get("/messages")
async def list_messages(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_wecom_viewer),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    sender: Annotated[str | None, Query(max_length=64)] = None,
    room: Annotated[str | None, Query(max_length=64)] = None,
    keyword: Annotated[str | None, Query(max_length=128)] = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict:
    """存档消息分页查看（筛选语义见 _message_conds）。"""
    conds = await _message_conds(db, sender, room, keyword, start, end)

    total = (await db.execute(select(func.count()).select_from(WecomMessage).where(*conds))).scalar_one()
    rows = (
        (
            await db.execute(
                select(WecomMessage)
                .where(*conds)
                .order_by(WecomMessage.msgtime.desc(), WecomMessage.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    names, rnames, receiver_lists = await _name_maps(db, rows)

    items = [
        {
            "id": m.id,
            "msgid": m.msgid,
            "action": m.action,
            "msgtype": m.msgtype,
            "text": m.text,
            "msgtime": m.msgtime,
            "sender_userid": m.sender_userid,
            "sender_name": names.get(m.sender_userid, ""),
            "roomid": m.roomid,
            "room_name": rnames.get(m.roomid, "") if m.roomid else "",
            "receivers": [{"userid": u, "name": names.get(u, "")} for u in receiver_lists[m.id]],
        }
        for m in rows
    ]

    # 审计只在第一页留一条痕（翻页是同次查看的延续）；detail 只有筛选条件+条数，不落正文
    if page == 1:
        db.add(
            AuditLog(
                actor_user_id=user.id,
                action="wecom.messages_view",
                target_type="wecom_message",
                target_id="",
                detail=json.dumps(
                    {
                        "filters": {
                            "sender": sender or "",
                            "room": room or "",
                            "keyword": keyword or "",
                            "start": start.isoformat() if start else "",
                            "end": end.isoformat() if end else "",
                        },
                        "total": total,
                    },
                    ensure_ascii=False,
                ),
            )
        )
        await db.commit()  # GET 里显式 commit（ops admin.read_conversation 范式）
    return {"total": total, "page": page, "page_size": page_size, "items": items}


# M31.2 导出上限：超出直接 400 引导缩小范围，不悄悄截断（存档完整性比省事重要）
_EXPORT_MAX = 50000
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/messages/export")
async def export_messages(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_wecom_viewer),
    sender: Annotated[str | None, Query(max_length=64)] = None,
    room: Annotated[str | None, Query(max_length=64)] = None,
    keyword: Annotated[str | None, Query(max_length=128)] = None,
    start: datetime | None = None,
    end: datetime | None = None,
):
    """导出存档消息 xlsx：跟随列表同款筛选（所见即所得），同事定期下载存档用。"""
    import io
    from urllib.parse import quote

    from fastapi.responses import StreamingResponse
    from openpyxl import Workbook

    conds = await _message_conds(db, sender, room, keyword, start, end)
    total = (await db.execute(select(func.count()).select_from(WecomMessage).where(*conds))).scalar_one()
    if total > _EXPORT_MAX:
        raise HTTPException(400, f"命中 {total} 条，超过单次导出上限 {_EXPORT_MAX}——请缩小日期范围或加筛选条件分批导出")
    rows = (
        (
            await db.execute(
                select(WecomMessage).where(*conds).order_by(WecomMessage.msgtime.asc(), WecomMessage.id.asc())
            )
        )
        .scalars()
        .all()
    )
    names, rnames, receiver_lists = await _name_maps(db, rows)

    wb = Workbook()
    ws = wb.active
    ws.title = "企微存档消息"
    ws.append(["时间", "发送人", "会话", "类型", "内容", "接收人"])
    for m in rows:
        conv = (rnames.get(m.roomid, "") or m.roomid) if m.roomid else "与 " + "、".join(
            names.get(u, "") or u for u in receiver_lists[m.id]
        )
        ws.append([
            datetime.fromtimestamp(m.msgtime / 1000).strftime("%Y-%m-%d %H:%M:%S"),
            names.get(m.sender_userid, "") or m.sender_userid,
            conv,
            m.msgtype or "",
            m.text or "",
            "、".join(names.get(u, "") or u for u in receiver_lists[m.id]),
        ])
    for col, w in zip("ABCDEF", (20, 14, 24, 10, 60, 24)):
        ws.column_dimensions[col].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    d = lambda dt, fallback: dt.astimezone().strftime("%Y-%m-%d") if dt else fallback  # noqa: E731
    filename = f"企微存档_{d(start, '起')}_{d(end, '至今')}.xlsx"

    # 审计一次（filters+total 不落正文），与列表查看同款纪律
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="wecom.messages_export",
            target_type="wecom_message",
            target_id="",
            detail=json.dumps(
                {
                    "filters": {
                        "sender": sender or "",
                        "room": room or "",
                        "keyword": keyword or "",
                        "start": start.isoformat() if start else "",
                        "end": end.isoformat() if end else "",
                    },
                    "total": total,
                },
                ensure_ascii=False,
            ),
        )
    )
    await db.commit()
    return StreamingResponse(buf, media_type=_XLSX_MIME, headers={"Content-Disposition": f"attachment; filename*=utf-8''{quote(filename)}"})


@router.get("/messages/meta")
async def messages_meta(db: AsyncSession = Depends(get_db), _: User = Depends(require_wecom_viewer)) -> dict:
    """筛选下拉数据源（群清单）；纯元数据不审计。"""
    rooms = (
        (
            await db.execute(
                select(WecomRoom).where(WecomRoom.deleted_at.is_(None)).order_by(WecomRoom.roomname.asc(), WecomRoom.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return {"rooms": [{"roomid": r.roomid, "roomname": r.roomname} for r in rooms]}


# ---------- M31.4 手工名称映射（别名） ----------
# wm/wr 开头的外部人/外部群企微不开放接口（外部群 msgaudit/groupchat/get 无群名、
# 外部联系人无姓名 API），唯一确定解=管理员认出后命名一次。天然兼容零迁移：
# maps.resolve_room_name 只在 roomname 为空时重试（手工群名永不被覆盖）；
# maps.refresh_contacts 只更新 user/list 出现的 userid（wm 别名永不被覆盖）。


class RoomNameIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)


@router.put("/rooms/{roomid}")
async def rename_room(roomid: str, body: RoomNameIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_wecom_viewer)) -> dict:
    """手工命名群（upsert；软删行复活，照 _grant 范式）。群名非会话正文，可落审计。"""
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "群名不能为空")
    row = (await db.execute(select(WecomRoom).where(WecomRoom.roomid == roomid))).scalar_one_or_none()
    if row is None:
        row = WecomRoom(roomid=roomid, roomname=name)
        db.add(row)
        await db.flush()
    else:
        row.deleted_at = None  # 复活软删行
        row.roomname = name
    await _audit(db, admin, "wecom.room_rename", roomid, {"roomid": roomid, "name": name}, target_type="wecom_room")
    await db.commit()
    return {"roomid": roomid, "roomname": name}


@router.get("/contacts/unknown")
async def unknown_senders(db: AsyncSession = Depends(get_db), _: User = Depends(require_wecom_viewer)) -> dict:
    """还没名字映射的发送人清单（按消息数降序，前 50）——管理员照单认人。"""
    rows = (
        await db.execute(
            select(WecomMessage.sender_userid, func.count().label("n"))
            .where(WecomMessage.deleted_at.is_(None), WecomMessage.sender_userid != "")
            .group_by(WecomMessage.sender_userid)
        )
    ).all()
    known = {
        c.userid
        for c in (await db.execute(select(WecomContact).where(WecomContact.deleted_at.is_(None)))).scalars().all()
    }
    items = sorted(({"userid": uid, "messages": n} for uid, n in rows if uid not in known), key=lambda x: -x["messages"])
    return {"items": items[:50]}


class ContactNameIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)


@router.put("/contacts/{userid}")
async def alias_contact(userid: str, body: ContactNameIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_wecom_viewer)) -> dict:
    """手工给 userid 起别名（upsert；软删行复活）。外部 wm 账号不在通讯录刷新范围，别名稳定。"""
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "名字不能为空")
    row = (await db.execute(select(WecomContact).where(WecomContact.userid == userid))).scalar_one_or_none()
    if row is None:
        row = WecomContact(userid=userid, name=name)
        db.add(row)
        await db.flush()
    else:
        row.deleted_at = None
        row.name = name
    await _audit(db, admin, "wecom.contact_alias", userid, {"userid": userid, "name": name}, target_type="wecom_contact")
    await db.commit()
    return {"userid": userid, "name": name}


# ---------- M31.1 授权管理（admin only；复活或新建） ----------


class GrantIn(BaseModel):
    user_id: int


async def _grant(db: AsyncSession, model, user_id: int, admin: User, action: str, err_dup: str, require_feishu: bool = False) -> dict:
    u = (
        await db.execute(select(User).where(User.id == user_id, User.deleted_at.is_(None), User.status == "active"))
    ).scalar_one_or_none()
    if u is None:
        raise HTTPException(404, "用户不存在或已停用")
    if require_feishu and not u.feishu_open_id:
        raise HTTPException(400, "该用户未绑定飞书，无法接收告警推送")
    row = (await db.execute(select(model).where(model.user_id == user_id))).scalar_one_or_none()
    if row is not None:
        if row.deleted_at is None:
            raise HTTPException(409, err_dup)
        row.deleted_at = None  # 复活软删行
    else:
        row = model(user_id=user_id)
        db.add(row)
        await db.flush()
    await _audit(db, admin, action, user_id, {"user_id": user_id, "name": u.name})
    await db.commit()
    return {"id": row.id, "user_id": user_id}


async def _revoke(db: AsyncSession, model, row_id: int, admin: User, action: str, err_404: str) -> None:
    row = (
        await db.execute(select(model).where(model.id == row_id, model.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, err_404)
    row.deleted_at = utcnow()
    await _audit(db, admin, action, row.user_id, {"user_id": row.user_id})
    await db.commit()


async def _list_grants(db: AsyncSession, model) -> list[dict]:
    rows = (
        await db.execute(
            select(model, User)
            .join(User, User.id == model.user_id)
            .where(model.deleted_at.is_(None), User.deleted_at.is_(None))
            .order_by(model.id.desc())
        )
    ).all()
    return [
        {
            "id": r.id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "user": {"id": u.id, "name": u.name, "username": u.username, "feishu_bound": bool(u.feishu_open_id)},
        }
        for r, u in rows
    ]


@router.get("/viewers")
async def list_viewers(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)) -> list[dict]:
    return await _list_grants(db, WecomViewer)


@router.post("/viewers", status_code=201)
async def grant_viewer(body: GrantIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> dict:
    return await _grant(db, WecomViewer, body.user_id, admin, "wecom.viewer_grant", "该用户已是授权查看人")


@router.delete("/viewers/{rid}", status_code=204)
async def revoke_viewer(rid: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> None:
    await _revoke(db, WecomViewer, rid, admin, "wecom.viewer_revoke", "授权记录不存在")


@router.get("/alert-receivers")
async def list_receivers(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)) -> list[dict]:
    return await _list_grants(db, WecomAlertReceiver)


@router.post("/alert-receivers", status_code=201)
async def add_receiver(body: GrantIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> dict:
    return await _grant(db, WecomAlertReceiver, body.user_id, admin, "wecom.receiver_add", "该用户已在告警接收人名单", require_feishu=True)


@router.delete("/alert-receivers/{rid}", status_code=204)
async def remove_receiver(rid: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> None:
    await _revoke(db, WecomAlertReceiver, rid, admin, "wecom.receiver_remove", "接收人记录不存在")


# ---------- M31.2 保留策略（默认永久；管理员界面可配天数，清扫在 scheduler/engine.py） ----------
# _RETENTION_KEY / get_retention_days 唯一实现在 sync.py（清扫任务也用它），此处只做 HTTP 壳

_RETENTION_MAX = 3650


@router.get("/retention")
async def get_retention(db: AsyncSession = Depends(get_db), _: User = Depends(require_wecom_viewer)) -> dict:
    return {"days": await get_retention_days(db)}


class RetentionIn(BaseModel):
    days: int = Field(ge=0, le=_RETENTION_MAX)


@router.put("/retention")
async def set_retention(body: RetentionIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> dict:
    row = (
        await db.execute(select(SystemSetting).where(SystemSetting.key == _RETENTION_KEY, SystemSetting.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if row is None:
        row = SystemSetting(key=_RETENTION_KEY, value=str(body.days))
        db.add(row)
    else:
        row.value = str(body.days)
    await _audit(db, admin, "wecom.retention_set", _RETENTION_KEY, {"days": body.days}, target_type="system")
    await db.commit()
    return {"days": body.days}
