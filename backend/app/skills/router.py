"""技能库：管理端管平台技能，员工管个人技能（owner_user_id 区分）。

技能是文件夹形态：SKILL.md 为主文件 + scripts/x.py 等附件（skill_files 表）。
平台技能对员工只读；员工 fork 复制为个人技能后可自由改（自动改名防冲突）。
M3.3 流通体系：fork/收录记血缘（source_skill_id + source_updated_at）；
个人技能先「收录」复制为平台技能再派发，作者改原稿不影响使用者。
M33：领用副本可自助「升级到最新版」（pull-update 覆盖内容，覆盖前自动快照备份，
rollback-upgrade 回滚）；备份=软删态 Skill 行（name--bak-<副本id>），零迁移零侵入。
"""
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import require_module
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.agent import Agent, AgentSkill, Skill, SkillFile
from app.models.base import utcnow
from app.models.user import User

admin_router = APIRouter(prefix="/api/admin/skills", tags=["admin-skills"])
router = APIRouter(prefix="/api/skills", tags=["skills"])


class SkillIn(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str = ""
    category: str = ""
    instructions: str = ""  # = SKILL.md 内容
    enabled: bool = True


def _out(s: Skill) -> dict:
    return {
        "id": s.id, "name": s.name, "description": s.description, "category": s.category,
        "instructions": s.instructions, "enabled": s.enabled, "owner_user_id": s.owner_user_id,
        "created_by": s.created_by, "source_skill_id": s.source_skill_id,
        "source_updated_at": s.source_updated_at.isoformat() if s.source_updated_at else None,
    }


def _deleted_name(name: str, skill_id: int) -> str:
    """软删除时改名释放 name 唯一约束：name--del-<id>（id 唯一保证不撞名），截断到 64。"""
    suffix = f"--del-{skill_id}"
    return name[: 64 - len(suffix)] + suffix


def _check_name_free(name: str, dup: Skill | None) -> None:
    if dup is not None:
        if dup.deleted_at is None:
            raise HTTPException(400, "技能名已存在（含平台技能）")
        raise HTTPException(400, "技能名被已删除技能占用（历史遗留数据），请换个名称或联系管理员清理")


# ---------- 技能文件夹 ----------

class SkillFileIn(BaseModel):
    # \w 在 Python re 下含中文等 unicode 字符
    path: str = Field(min_length=1, max_length=256, pattern=r"^[\w.\-]+(/[\w.\-]+)*$")
    content: str = ""

    @field_validator("path")
    @classmethod
    def _no_dot_segments(cls, v: str) -> str:
        if any(set(seg) <= {"."} for seg in v.split("/")):  # 纯点段（"." / ".."）有穿越语义，拒绝
            raise ValueError("路径不允许 . 或 .. 段")
        return v


class SkillFilesIn(BaseModel):
    files: list[SkillFileIn]  # 全量替换语义：不在列表里的旧文件被删除


async def _get_files(db: AsyncSession, skill_id: int) -> list[dict]:
    rows = (
        (await db.execute(select(SkillFile).where(SkillFile.skill_id == skill_id, SkillFile.deleted_at.is_(None)).order_by(SkillFile.path)))
        .scalars()
        .all()
    )
    return [{"path": f.path, "content": f.content} for f in rows]


async def _put_files(db: AsyncSession, skill: Skill, files: list[SkillFileIn]) -> list[dict]:
    paths = [f.path for f in files]
    if len(paths) != len(set(paths)):
        raise HTTPException(400, "文件路径重复")
    if "SKILL.md" not in paths:
        raise HTTPException(400, "必须包含主文件 SKILL.md")
    existing = (
        (await db.execute(select(SkillFile).where(SkillFile.skill_id == skill.id, SkillFile.deleted_at.is_(None)))).scalars().all()
    )
    by_path = {f.path: f for f in existing}
    want = {f.path: f.content for f in files}
    for p, row in by_path.items():
        if p not in want:
            row.deleted_at = utcnow()
    for p, content in want.items():
        if p in by_path:
            by_path[p].content = content
            by_path[p].deleted_at = None
        else:
            db.add(SkillFile(skill_id=skill.id, path=p, content=content))
    skill.instructions = want["SKILL.md"]  # 冗余同步，兼容旧读法
    await db.commit()
    return await _get_files(db, skill.id)


# ---------- 管理端：技能总览（平台 + 个人） ----------

async def _user_names(db: AsyncSession, ids: set[int]) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {i: n for i, n in (await db.execute(select(User.id, User.name).where(User.id.in_(ids)))).all()}


@admin_router.get("")
async def list_platform_skills(db: AsyncSession = Depends(get_db), _: User = Depends(require_module("skills_admin"))) -> list[dict]:
    """技能总览：平台技能 + 全部员工的个人技能（带归属名/原作者署名）。"""
    rows = (
        (await db.execute(select(Skill).where(Skill.deleted_at.is_(None)).order_by(Skill.id.asc())))
        .scalars()
        .all()
    )
    names = await _user_names(db, {s.owner_user_id for s in rows} | {s.created_by for s in rows})
    return [
        {
            **_out(s),
            "owner_name": names.get(s.owner_user_id) if s.owner_user_id else None,
            "created_by_name": names.get(s.created_by) if s.created_by else None,
        }
        for s in rows
    ]


@admin_router.get("/overview")
async def skills_overview(db: AsyncSession = Depends(get_db), _: User = Depends(require_module("skills_admin"))) -> list[dict]:
    """派发总览：每个技能（含个人技能）被哪些 agent（含属主）绑定。"""
    skills = ((await db.execute(select(Skill).where(Skill.deleted_at.is_(None)).order_by(Skill.id.asc()))).scalars().all())
    binds = (
        (
            await db.execute(
                select(AgentSkill.skill_id, Agent.id, Agent.name, Agent.is_template, User.name)
                .join(Agent, Agent.id == AgentSkill.agent_id)
                .outerjoin(User, User.id == Agent.owner_user_id)
                .where(AgentSkill.deleted_at.is_(None), Agent.deleted_at.is_(None))
            )
        )
        .all()
    )
    by_skill: dict[int, list[dict]] = {}
    for skill_id, agent_id, agent_name, is_template, owner_name in binds:
        by_skill.setdefault(skill_id, []).append({
            "agent_id": agent_id,
            "agent_name": agent_name,
            "is_template": is_template,
            "owner_name": owner_name,
        })
    owners = {s.owner_user_id for s in skills if s.owner_user_id}
    owners |= {s.created_by for s in skills if s.created_by}
    owner_names = await _user_names(db, owners)
    return [
        {
            **_out(s),
            "owner_name": owner_names.get(s.owner_user_id) if s.owner_user_id else None,
            "created_by_name": owner_names.get(s.created_by) if s.created_by else None,
            "bindings": by_skill.get(s.id, []),
        }
        for s in skills
    ]


@admin_router.post("", status_code=201)
async def create_platform_skill(body: SkillIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_module("skills_admin"))) -> dict:
    dup = (await db.execute(select(Skill).where(Skill.name == body.name))).scalar_one_or_none()
    _check_name_free(body.name, dup)
    s = Skill(**body.model_dump(), owner_user_id=None, created_by=admin.id)
    db.add(s)
    try:
        await db.flush()
        db.add(SkillFile(skill_id=s.id, path="SKILL.md", content=body.instructions))
        await db.commit()
    except IntegrityError:  # 并发同名等极端情况兜底，不裸抛 500
        await db.rollback()
        raise HTTPException(400, "技能名已存在（含平台技能）")
    return _out(s)


@admin_router.patch("/{skill_id}")
async def update_platform_skill(skill_id: int, body: SkillIn, db: AsyncSession = Depends(get_db), _: User = Depends(require_module("skills_admin"))) -> dict:
    s = (await db.execute(select(Skill).where(Skill.id == skill_id, Skill.deleted_at.is_(None)))).scalar_one_or_none()
    if s is None:
        raise HTTPException(404, "技能不存在")
    for k, v in body.model_dump().items():
        setattr(s, k, v)
    # instructions 同步到 SKILL.md
    f = (
        await db.execute(
            select(SkillFile).where(SkillFile.skill_id == s.id, SkillFile.path == "SKILL.md", SkillFile.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if f is None:
        db.add(SkillFile(skill_id=s.id, path="SKILL.md", content=body.instructions))
    else:
        f.content = body.instructions
    await db.commit()
    return _out(s)


@admin_router.delete("/{skill_id}", status_code=204)
async def delete_platform_skill(skill_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(require_module("skills_admin"))) -> None:
    s = (await db.execute(select(Skill).where(Skill.id == skill_id, Skill.deleted_at.is_(None)))).scalar_one_or_none()
    if s is None:
        raise HTTPException(404, "技能不存在")
    s.deleted_at = utcnow()
    s.name = _deleted_name(s.name, s.id)  # 释放 name 唯一约束，否则同名重建会 500
    await db.commit()


@admin_router.get("/{skill_id}/files")
async def admin_get_files(skill_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(require_module("skills_admin"))) -> list[dict]:
    return await _get_files(db, skill_id)


class SkillDispatchIn(BaseModel):
    agent_ids: list[int] = Field(min_length=1)


class PromoteIn(BaseModel):
    name: str | None = None  # 平台版名称；默认自动从原名派生（原名被原稿占用）


@admin_router.post("/{skill_id}/promote", status_code=201)
async def promote_skill(skill_id: int, body: PromoteIn, db: AsyncSession = Depends(get_db), _: User = Depends(require_module("skills_admin"))) -> dict:
    """收录（M3.3 蒸馏）：把个人技能**复制**为平台技能（归组织，created_by 署名原作者）。
    原稿仍归作者、互不影响；收录后再派发平台版，使用者不受作者后续修改影响。"""
    src = (await db.execute(select(Skill).where(Skill.id == skill_id, Skill.deleted_at.is_(None)))).scalar_one_or_none()
    if src is None:
        raise HTTPException(404, "技能不存在")
    if src.owner_user_id is None:
        raise HTTPException(400, "平台技能无需收录")
    existing = set((await db.execute(select(Skill.name))).scalars().all())  # 含软删除行：历史遗留占名也要避开
    if body.name:
        name = body.name.strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            raise HTTPException(400, "名称格式不合法：小写字母/数字/连字符")
        if name in existing:
            raise HTTPException(400, "技能名已存在，请换一个")
    else:
        name = next((c for c in [f"{src.name}-lib"] + [f"{src.name}-lib-{i}" for i in range(2, 10)] if c not in existing), None)
        if name is None:
            raise HTTPException(400, "无法生成不冲突的技能名，请手工指定 name")
    s = Skill(
        name=name, description=src.description, category=src.category, instructions=src.instructions,
        owner_user_id=None, enabled=True, created_by=src.owner_user_id,
        source_skill_id=src.id, source_updated_at=src.updated_at,
    )
    db.add(s)
    await db.flush()
    files = (
        (await db.execute(select(SkillFile).where(SkillFile.skill_id == src.id, SkillFile.deleted_at.is_(None)))).scalars().all()
    )
    for f in files:
        db.add(SkillFile(skill_id=s.id, path=f.path, content=f.content))
    await db.commit()
    return _out(s)


@admin_router.post("/{skill_id}/dispatch")
async def dispatch_skill(skill_id: int, body: SkillDispatchIn, db: AsyncSession = Depends(get_db), _: User = Depends(require_module("skills_admin"))) -> dict:
    """把平台技能下发给一批数字人（模板或实例均可）。幂等：已绑定跳过，软删复活。
    M3.3 起个人技能不能直接派发——先「收录」为平台技能再派发，否则作者改原稿会实时影响使用者。"""
    s = (await db.execute(select(Skill).where(Skill.id == skill_id, Skill.deleted_at.is_(None)))).scalar_one_or_none()
    if s is None:
        raise HTTPException(404, "技能不存在")
    if s.owner_user_id is not None:
        raise HTTPException(400, "个人技能不能直接派发：请先用「收录」复制为平台技能再派发，确保使用者不受原作者修改影响")
    bound: list[int] = []
    for aid in set(body.agent_ids):
        a = (await db.execute(select(Agent).where(Agent.id == aid, Agent.deleted_at.is_(None)))).scalar_one_or_none()
        if a is None:
            raise HTTPException(404, f"数字人 #{aid} 不存在")
        row = (
            await db.execute(select(AgentSkill).where(AgentSkill.agent_id == aid, AgentSkill.skill_id == skill_id))
        ).scalar_one_or_none()
        if row is None:
            db.add(AgentSkill(agent_id=aid, skill_id=skill_id))
        elif row.deleted_at is not None:
            row.deleted_at = None
        bound.append(aid)
    await db.commit()
    return {"skill_id": skill_id, "bound_agent_ids": bound}


# ---------- 员工端：技能市场 ----------

@router.get("")
async def skill_market(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    """技能市场：全部平台技能（含停用，灰显），附我是否已 fork 过。"""
    platform = (
        (await db.execute(select(Skill).where(Skill.deleted_at.is_(None), Skill.owner_user_id.is_(None)).order_by(Skill.id.asc())))
        .scalars()
        .all()
    )
    my_names = set(
        (await db.execute(select(Skill.name).where(Skill.deleted_at.is_(None), Skill.owner_user_id == user.id))).scalars().all()
    )
    out = []
    for s in platform:
        forked = any(n == s.name or n.startswith(s.name + "-") for n in my_names)
        out.append({**_out(s), "forked": forked})
    return out


@admin_router.put("/{skill_id}/files")
async def admin_put_files(skill_id: int, body: SkillFilesIn, db: AsyncSession = Depends(get_db), _: User = Depends(require_module("skills_admin"))) -> list[dict]:
    s = (await db.execute(select(Skill).where(Skill.id == skill_id, Skill.deleted_at.is_(None)))).scalar_one_or_none()
    if s is None:
        raise HTTPException(404, "技能不存在")
    return await _put_files(db, s, body.files)


# ---------- 员工端：个人技能 ----------

@router.get("/mine")
async def list_my_skills(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    """我的个人技能；fork 副本附血缘信息（源自谁 / 源有更新 / 源已删除）。"""
    rows = (
        (await db.execute(select(Skill).where(Skill.deleted_at.is_(None), Skill.owner_user_id == user.id).order_by(Skill.id.asc())))
        .scalars()
        .all()
    )
    src_ids = {s.source_skill_id for s in rows if s.source_skill_id}
    srcs: dict[int, Skill] = {}
    if src_ids:
        # 含已软删的源（要区分"源已删除"与"源有更新"）
        srcs = {s.id: s for s in (await db.execute(select(Skill).where(Skill.id.in_(src_ids)))).scalars().all()}
    copy_ids = [s.id for s in rows if s.source_skill_id]
    bak_of: set[int] = set()
    if copy_ids:
        # M33：哪些副本有升级备份（软删态、name 带 --bak- 后缀，区别于被删的 fork）
        bak_of = set(
            (
                await db.execute(
                    select(Skill.source_skill_id).where(
                        Skill.owner_user_id == user.id,
                        Skill.deleted_at.is_not(None),
                        Skill.source_skill_id.in_(copy_ids),
                        Skill.name.like("%--bak-%"),
                    )
                )
            ).scalars().all()
        )
    out = []
    for s in rows:
        d = _out(s)
        if s.source_skill_id:
            src = srcs.get(s.source_skill_id)
            d["source_name"] = src.name if src else None
            d["source_deleted"] = src is None or src.deleted_at is not None
            # 源在 fork 之后被更新过 → 提醒（不强制）
            d["source_has_update"] = bool(
                src and src.deleted_at is None and s.source_updated_at and src.updated_at and src.updated_at > s.source_updated_at
            )
            d["has_backup"] = s.id in bak_of
        out.append(d)
    return out


@router.post("/mine", status_code=201)
async def create_my_skill(body: SkillIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    dup = (await db.execute(select(Skill).where(Skill.name == body.name))).scalar_one_or_none()
    _check_name_free(body.name, dup)
    s = Skill(**body.model_dump(), owner_user_id=user.id, created_by=user.id)
    db.add(s)
    try:
        await db.flush()
        db.add(SkillFile(skill_id=s.id, path="SKILL.md", content=body.instructions))
        await db.commit()
    except IntegrityError:  # 并发同名等极端情况兜底，不裸抛 500
        await db.rollback()
        raise HTTPException(400, "技能名已存在（含平台技能）")
    return _out(s)


@router.patch("/mine/{skill_id}")
async def update_my_skill(skill_id: int, body: SkillIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    s = (await db.execute(select(Skill).where(Skill.id == skill_id, Skill.deleted_at.is_(None), Skill.owner_user_id == user.id))).scalar_one_or_none()
    if s is None:
        raise HTTPException(404, "技能不存在或不属于你")
    for k, v in body.model_dump().items():
        setattr(s, k, v)
    f = (
        await db.execute(
            select(SkillFile).where(SkillFile.skill_id == s.id, SkillFile.path == "SKILL.md", SkillFile.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if f is None:
        db.add(SkillFile(skill_id=s.id, path="SKILL.md", content=body.instructions))
    else:
        f.content = body.instructions
    await db.commit()
    return _out(s)


@router.delete("/mine/{skill_id}", status_code=204)
async def delete_my_skill(skill_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    s = (await db.execute(select(Skill).where(Skill.id == skill_id, Skill.deleted_at.is_(None), Skill.owner_user_id == user.id))).scalar_one_or_none()
    if s is None:
        raise HTTPException(404, "技能不存在或不属于你")
    s.deleted_at = utcnow()
    s.name = _deleted_name(s.name, s.id)  # 释放 name 唯一约束，否则同名重建会 500
    await db.commit()


@router.get("/mine/{skill_id}/files")
async def my_get_files(skill_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    s = (await db.execute(select(Skill).where(Skill.id == skill_id, Skill.deleted_at.is_(None)))).scalar_one_or_none()
    if s is None:
        raise HTTPException(404, "技能不存在")
    if s.owner_user_id is not None and s.owner_user_id != user.id:
        # 他人的个人技能：仅当绑定在我名下的数字人上时可只读查看（派发/迁移场景，否则 404 不暴露存在性）
        bound = (
            await db.execute(
                select(AgentSkill)
                .join(Agent, Agent.id == AgentSkill.agent_id)
                .where(
                    AgentSkill.skill_id == skill_id,
                    AgentSkill.deleted_at.is_(None),
                    Agent.owner_user_id == user.id,
                    Agent.deleted_at.is_(None),
                )
            )
        ).scalars().first()
        if bound is None:
            raise HTTPException(404, "技能不存在")
    return await _get_files(db, skill_id)


@router.put("/mine/{skill_id}/files")
async def my_put_files(skill_id: int, body: SkillFilesIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    s = (await db.execute(select(Skill).where(Skill.id == skill_id, Skill.deleted_at.is_(None), Skill.owner_user_id == user.id))).scalar_one_or_none()
    if s is None:
        raise HTTPException(404, "技能不存在或不属于你")
    return await _put_files(db, s, body.files)


# ---------- fork：复制平台技能为个人技能 ----------

@router.post("/{skill_id}/fork", status_code=201)
async def fork_skill(skill_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    src = (await db.execute(select(Skill).where(Skill.id == skill_id, Skill.deleted_at.is_(None)))).scalar_one_or_none()
    if src is None:
        raise HTTPException(404, "技能不存在")
    if src.owner_user_id is not None and src.owner_user_id != user.id:
        raise HTTPException(403, "只能复制平台技能或自己的技能")
    base = src.name
    candidates = [base, f"{base}-{user.username}"] + [f"{base}-{user.username}-{i}" for i in range(2, 10)]
    existing = set((await db.execute(select(Skill.name))).scalars().all())  # 含软删除行：历史遗留占名也要避开
    name = next((c for c in candidates if c not in existing), None)
    if name is None:
        raise HTTPException(400, "无法生成不冲突的技能名，请手工新建")
    s = Skill(
        name=name, description=src.description, category=src.category, instructions=src.instructions,
        owner_user_id=user.id, enabled=True, created_by=user.id,
        source_skill_id=src.id, source_updated_at=src.updated_at,  # 血缘：fork 自谁 + 源快照时间
    )
    db.add(s)
    await db.flush()
    files = (
        (await db.execute(select(SkillFile).where(SkillFile.skill_id == src.id, SkillFile.deleted_at.is_(None)))).scalars().all()
    )
    if files:
        for f in files:
            db.add(SkillFile(skill_id=s.id, path=f.path, content=f.content))
    else:
        db.add(SkillFile(skill_id=s.id, path="SKILL.md", content=src.instructions))
    await db.commit()
    return _out(s)


# ---------- M33：领用副本自助升级（覆盖前自动备份，可回滚） ----------


def _backup_name(copy: Skill) -> str:
    """升级备份名：name--bak-<副本id>（id 唯一保证不撞名，仿 _deleted_name），截断到 64。"""
    suffix = f"--bak-{copy.id}"
    return copy.name[: 64 - len(suffix)] + suffix


async def _find_backups(db: AsyncSession, copy: Skill) -> list[Skill]:
    """查副本的全部升级备份：血缘指向副本 + 软删态 + --bak-<id> 后缀（区别于被删除的 fork，后者是 --del-）。
    按后缀而非全名匹配：副本升级后改过名也能找到旧备份（改名会产生多代备份行，新的 id 大排前）。"""
    return list(
        (
            await db.execute(
                select(Skill)
                .where(
                    Skill.source_skill_id == copy.id,
                    Skill.deleted_at.is_not(None),
                    Skill.name.like(f"%--bak-{copy.id}"),
                )
                .order_by(Skill.id.desc())
            )
        )
        .scalars()
        .all()
    )


async def _snapshot_copy(db: AsyncSession, copy: Skill) -> Skill:
    """把副本当前内容快照为软删态备份技能（单级备份：先硬删全部旧备份，重复升级不撞 name 唯一约束）。"""
    for old in await _find_backups(db, copy):
        await db.execute(delete(SkillFile).where(SkillFile.skill_id == old.id))
        await db.delete(old)
    await db.flush()
    bak = Skill(
        name=_backup_name(copy), description=copy.description, category=copy.category,
        instructions=copy.instructions, owner_user_id=copy.owner_user_id, enabled=copy.enabled,
        created_by=copy.created_by, source_skill_id=copy.id, source_updated_at=copy.source_updated_at,
        deleted_at=utcnow(),  # 软删态：市场/我的技能/prompt 技能名册/_read_skill 天然不可见
    )
    db.add(bak)
    await db.flush()
    files = (
        (await db.execute(select(SkillFile).where(SkillFile.skill_id == copy.id, SkillFile.deleted_at.is_(None)))).scalars().all()
    )
    for f in files:
        db.add(SkillFile(skill_id=bak.id, path=f.path, content=f.content))
    await db.flush()
    return bak


async def _overwrite_files(db: AsyncSession, dst: Skill, files: list[tuple[str, str]], instructions_fallback: str) -> None:
    """用 files 整批覆盖目标技能的文件夹，并同步冗余 instructions。

    必须硬删旧行（含软删行）：uq_skill_file 是纯唯一约束，软删行仍占 (skill_id, path)，
    软删后同名 INSERT 会撞 500（_put_files 的 revive 也救不了历史软删行）。
    """
    await db.execute(delete(SkillFile).where(SkillFile.skill_id == dst.id))
    if not files:
        files = [("SKILL.md", instructions_fallback)]
    main = None
    for path, content in files:
        db.add(SkillFile(skill_id=dst.id, path=path, content=content))
        if path == "SKILL.md":
            main = content
    dst.instructions = main if main is not None else instructions_fallback


@router.post("/{skill_id}/pull-update")
async def pull_update_skill(skill_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """领用副本一键升级到源技能最新版：整批覆盖内容（保留自己起的名/描述），覆盖前自动备份可回滚。

    绑定该副本的数字人无需处理：运行时实时读 skill_files，升级即生效。
    """
    copy = (
        await db.execute(select(Skill).where(Skill.id == skill_id, Skill.deleted_at.is_(None), Skill.owner_user_id == user.id))
    ).scalar_one_or_none()
    if copy is None:
        raise HTTPException(404, "技能不存在或不属于你")
    if copy.source_skill_id is None:
        raise HTTPException(400, "只有领用的副本才能升级")
    src = (await db.execute(select(Skill).where(Skill.id == copy.source_skill_id))).scalar_one_or_none()  # 含软删：要区分"源已删除"
    if src is None or src.deleted_at is not None:
        raise HTTPException(409, "源技能已被删除，无法升级")
    if not (copy.source_updated_at and src.updated_at and src.updated_at > copy.source_updated_at):
        raise HTTPException(400, "已是最新版本")
    await _snapshot_copy(db, copy)
    src_files = (
        (await db.execute(select(SkillFile).where(SkillFile.skill_id == src.id, SkillFile.deleted_at.is_(None)))).scalars().all()
    )
    await _overwrite_files(db, copy, [(f.path, f.content) for f in src_files], src.instructions)
    copy.source_updated_at = src.updated_at  # 清掉 source_has_update 标记
    await db.commit()
    return _out(copy)


@router.post("/{skill_id}/rollback-upgrade")
async def rollback_upgrade_skill(skill_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """回滚到升级前的备份；回滚前把当前状态再存成新备份 → 支持「回滚后再重做」。"""
    copy = (
        await db.execute(select(Skill).where(Skill.id == skill_id, Skill.deleted_at.is_(None), Skill.owner_user_id == user.id))
    ).scalar_one_or_none()
    if copy is None:
        raise HTTPException(404, "技能不存在或不属于你")
    bak = next(iter(await _find_backups(db, copy)), None)
    if bak is None:
        raise HTTPException(404, "没有可回滚的备份")
    # 先读进内存：_snapshot_copy 会硬删旧备份行
    bak_files = [
        (f.path, f.content)
        for f in (await db.execute(select(SkillFile).where(SkillFile.skill_id == bak.id, SkillFile.deleted_at.is_(None)))).scalars().all()
    ]
    bak_instructions, bak_source_updated_at = bak.instructions, bak.source_updated_at
    await _snapshot_copy(db, copy)  # 当前状态存成新备份（redo）
    await _overwrite_files(db, copy, bak_files, bak_instructions)
    copy.source_updated_at = bak_source_updated_at
    await db.commit()
    return _out(copy)
