"""更新公告（M12）：全员可读；发布/编辑/删除仅管理员（页面上手动维护，不再发版改代码）。"""
import json
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user, require_admin
from app.models.announcement import Announcement
from app.models.base import utcnow
from app.models.user import AuditLog, User

router = APIRouter(prefix="/api/announcements", tags=["announcements"])

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _out(a: Announcement) -> dict:
    return {
        "id": a.id,
        "title": a.title,
        "pub_date": a.pub_date,
        "content": a.content,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


async def _audit(db: AsyncSession, actor: User, action: str, target_id: object, detail: dict) -> None:
    db.add(AuditLog(actor_user_id=actor.id, action=action, target_type="announcement", target_id=str(target_id), detail=json.dumps(detail, ensure_ascii=False)))


class AnnouncementIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    pub_date: str = Field(min_length=10, max_length=10)
    content: str = Field(min_length=1)


class AnnouncementUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    pub_date: str | None = Field(default=None, min_length=10, max_length=10)
    content: str | None = Field(default=None, min_length=1)


def _check_date(raw: str) -> str:
    d = raw.strip()
    if not _DATE_RE.match(d):
        raise HTTPException(400, "日期格式必须是 YYYY-MM-DD")
    return d


async def _latest_id(db: AsyncSession) -> int | None:
    """M27：当前最大公告 id（未读判定游标的比较基准；按 id 而非 pub_date，补发的历史公告也算未读）。"""
    row = (
        await db.execute(
            select(Announcement).where(Announcement.deleted_at.is_(None)).order_by(Announcement.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    return row.id if row else None


@router.get("/status")
async def announcement_status(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    """M27：侧栏 NEW 标签轮询用——最新公告 id + 当前用户是否未读。"""
    latest = await _latest_id(db)
    return {"latest_id": latest, "unread": latest is not None and latest > (user.last_read_announcement_id or 0)}


@router.put("/read")
async def mark_announcements_read(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    """M27：进入更新公告页即把已读游标推到最新（无公告时不动）。"""
    latest = await _latest_id(db)
    if latest is not None:
        user.last_read_announcement_id = latest
        await db.commit()
    return {"latest_id": latest}


@router.get("")
async def list_announcements(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_user)) -> list[dict]:
    rows = (
        (
            await db.execute(
                select(Announcement)
                .where(Announcement.deleted_at.is_(None))
                .order_by(Announcement.pub_date.desc(), Announcement.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_out(a) for a in rows]


@router.post("", status_code=201)
async def create_announcement(body: AnnouncementIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> dict:
    a = Announcement(title=body.title.strip(), pub_date=_check_date(body.pub_date), content=body.content, created_by=admin.id)
    db.add(a)
    await db.flush()
    admin.last_read_announcement_id = a.id  # M27：发布者自己不该亮 NEW
    await _audit(db, admin, "announcement.create", a.id, {"title": a.title, "pub_date": a.pub_date})
    await db.commit()
    return _out(a)


@router.patch("/{aid}")
async def update_announcement(aid: int, body: AnnouncementUpdate, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> dict:
    a = (await db.execute(select(Announcement).where(Announcement.id == aid, Announcement.deleted_at.is_(None)))).scalar_one_or_none()
    if a is None:
        raise HTTPException(404, "公告不存在")
    changed = body.model_dump(exclude_none=True)
    if "title" in changed:
        a.title = changed["title"].strip()
    if "pub_date" in changed:
        a.pub_date = _check_date(changed["pub_date"])
    if "content" in changed:
        a.content = changed["content"]
    admin.last_read_announcement_id = await _latest_id(db)  # M27：编辑者刚看过全部，不亮 NEW
    await _audit(db, admin, "announcement.update", a.id, {"fields": sorted(changed)})
    await db.commit()
    return _out(a)


@router.delete("/{aid}", status_code=204)
async def delete_announcement(aid: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> None:
    a = (await db.execute(select(Announcement).where(Announcement.id == aid, Announcement.deleted_at.is_(None)))).scalar_one_or_none()
    if a is None:
        raise HTTPException(404, "公告不存在")
    a.deleted_at = utcnow()
    await _audit(db, admin, "announcement.delete", aid, {"title": a.title})
    await db.commit()
