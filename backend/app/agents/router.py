"""数字人路由（模板/实例二层模型）。

- /api/agents        员工端：我的实例 + 可领用模板 + 领用
- /api/my-agents     实例属主管理：名称头像 / .md 文件（版本+回滚）/ 技能绑定 / 从模板同步
- /api/admin/agents  管理端：模板与实例全视图、派发、可领用范围、主 agent、全量编辑
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import require_module
from app.core.database import get_db
from app.core.deps import get_current_user
from app.gateway.service import evaluate_grants
from app.models.agent import (
    AGENT_FILE_KEYS,
    Agent,
    AgentClaimGrant,
    AgentFile,
    AgentFileVersion,
    AgentSkill,
    Skill,
)
from app.models.base import utcnow
from app.models.user import Department, User

admin_router = APIRouter(prefix="/api/admin/agents", tags=["admin-agents"])
router = APIRouter(prefix="/api/agents", tags=["agents"])
my_router = APIRouter(prefix="/api/my-agents", tags=["my-agents"])


def _agent_out(a: Agent, owner_name: str | None = None) -> dict:
    return {
        "id": a.id,
        "name": a.name,
        "avatar": a.avatar,
        "description": a.description,
        "status": a.status,
        "is_template": a.is_template,
        "template_id": a.template_id,
        "owner_user_id": a.owner_user_id,
        "owner_name": owner_name,
        "is_master": a.is_master,
        "default_model_id": a.default_model_id,
        "model_allowlist": json.loads(a.model_allowlist) if a.model_allowlist else None,
        "allow_code_exec": a.allow_code_exec,
        "allow_web_search": a.allow_web_search,
        "allow_api_request": getattr(a, "allow_api_request", False),
        "allow_feishu": getattr(a, "allow_feishu", False),
        "allow_zhishu": getattr(a, "allow_zhishu", False),
        "allow_image_generate": getattr(a, "allow_image_generate", False),
        "max_loop_rounds": a.max_loop_rounds,
    }


async def _owner_infos(db: AsyncSession, agents: list[Agent]) -> dict[int, User]:
    ids = {a.owner_user_id for a in agents if a.owner_user_id}
    if not ids:
        return {}
    rows = ((await db.execute(select(User).where(User.id.in_(ids)))).scalars().all())
    return {u.id: u for u in rows}


async def _dept_names(db: AsyncSession, dept_ids: set[int]) -> dict[int, str]:
    if not dept_ids:
        return {}
    rows = (await db.execute(select(Department.id, Department.name).where(Department.id.in_(dept_ids)))).all()
    return {i: n for i, n in rows}


# ---------- 领用/派发：实例创建（复制模板） ----------

async def create_instance(db: AsyncSession, *, template: Agent, owner: User) -> Agent:
    """从模板复制一个员工实例：.md 文件（版本从 1）+ 技能绑定 + 模型设置。"""
    # 幂等只认 active 实例：已归档的不算"已领用"（否则主人永远无法重新领用该模板，
    # 会拿到归档实例 → 建会话 404"数字人不存在"），归档后重新领用 = 从模板复制全新实例
    existing = (
        await db.execute(
            select(Agent).where(
                Agent.template_id == template.id,
                Agent.owner_user_id == owner.id,
                Agent.status == "active",
                Agent.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing  # 幂等：已领用过直接返回

    owner_name = owner.name or owner.username
    inst = Agent(
        name=f"{owner_name}的{template.name}"[:64],
        avatar=template.avatar,
        description=template.description,
        status="active",
        created_by=owner.id,
        is_template=False,
        template_id=template.id,
        owner_user_id=owner.id,
        default_model_id=template.default_model_id,
        model_allowlist=template.model_allowlist,
        allow_code_exec=template.allow_code_exec,
        allow_web_search=template.allow_web_search,
        allow_api_request=getattr(template, "allow_api_request", False),
        allow_feishu=getattr(template, "allow_feishu", False),
        allow_zhishu=getattr(template, "allow_zhishu", False),
        allow_image_generate=getattr(template, "allow_image_generate", False),
        max_loop_rounds=template.max_loop_rounds,
    )
    db.add(inst)
    await db.flush()
    files = (
        (await db.execute(select(AgentFile).where(AgentFile.agent_id == template.id, AgentFile.deleted_at.is_(None))))
        .scalars()
        .all()
    )
    have_keys = {f.file_key for f in files}
    for f in files:
        db.add(AgentFile(agent_id=inst.id, file_key=f.file_key, content=f.content, version=1, updated_by=owner.id))
    for key in AGENT_FILE_KEYS:
        if key not in have_keys:
            db.add(AgentFile(agent_id=inst.id, file_key=key, content="", version=1, updated_by=owner.id))
    binds = (
        (await db.execute(select(AgentSkill).where(AgentSkill.agent_id == template.id, AgentSkill.deleted_at.is_(None))))
        .scalars()
        .all()
    )
    for b in binds:
        db.add(AgentSkill(agent_id=inst.id, skill_id=b.skill_id))
    await db.commit()
    return inst


async def _get_template(db: AsyncSession, tid: int) -> Agent:
    t = (await db.execute(select(Agent).where(Agent.id == tid, Agent.deleted_at.is_(None), Agent.is_template.is_(True)))).scalar_one_or_none()
    if t is None:
        raise HTTPException(404, "模板不存在")
    return t


async def _check_claimable(db: AsyncSession, template: Agent, user: User) -> None:
    if user.role == "admin":
        return
    grants = (
        (await db.execute(select(AgentClaimGrant).where(AgentClaimGrant.agent_id == template.id, AgentClaimGrant.deleted_at.is_(None))))
        .scalars()
        .all()
    )
    if not evaluate_grants(list(grants), user):
        raise HTTPException(403, "该数字人不在你的可领用范围内")


# ---------- 员工端：卡片墙 + 领用 ----------

@router.get("")
async def wall(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """我的实例 + 可领用模板（admin 额外看到主 agent 实例）。"""
    mine = (
        (
            await db.execute(
                select(Agent)
                .where(Agent.deleted_at.is_(None), Agent.is_template.is_(False), Agent.owner_user_id == user.id, Agent.status == "active")
                .order_by(Agent.id.asc())
            )
        )
        .scalars()
        .all()
    )
    templates = (
        (
            await db.execute(
                select(Agent).where(Agent.deleted_at.is_(None), Agent.is_template.is_(True), Agent.status == "active").order_by(Agent.id.asc())
            )
        )
        .scalars()
        .all()
    )
    claimed_template_ids = {a.template_id for a in mine}
    claimable = []
    for t in templates:
        if t.id in claimed_template_ids:
            continue
        grants = (
            (await db.execute(select(AgentClaimGrant).where(AgentClaimGrant.agent_id == t.id, AgentClaimGrant.deleted_at.is_(None))))
            .scalars()
            .all()
        )
        if evaluate_grants(list(grants), user):
            claimable.append(t)
    return {
        "my_instances": [_agent_out(a) for a in mine],
        "claimable_templates": [_agent_out(t) for t in claimable],
    }


@router.post("/{template_id}/claim", status_code=201)
async def claim(template_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    t = await _get_template(db, template_id)
    if t.status != "active":
        raise HTTPException(400, "该模板已归档")
    await _check_claimable(db, t, user)
    inst = await create_instance(db, template=t, owner=user)
    return _agent_out(inst)


# ---------- 实例属主管理（owner 本人；admin 走管理端） ----------

async def _get_own_instance(agent_id: int, user: User, db: AsyncSession) -> Agent:
    a = (await db.execute(select(Agent).where(Agent.id == agent_id, Agent.deleted_at.is_(None)))).scalar_one_or_none()
    if a is None or a.is_template or (a.owner_user_id != user.id and user.role != "admin"):
        raise HTTPException(404, "数字人不存在")
    return a


@my_router.get("/{agent_id}")
async def my_agent_detail(agent_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    a = await _get_own_instance(agent_id, user, db)
    return _agent_out(a)


class OwnerPatchIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    avatar: str | None = None
    description: str = ""


@my_router.patch("/{agent_id}")
async def my_agent_patch(agent_id: int, body: OwnerPatchIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    a = await _get_own_instance(agent_id, user, db)
    a.name = body.name
    a.avatar = body.avatar
    a.description = body.description
    await db.commit()
    return _agent_out(a)


# ----- .md 文件（属主与管理端共用一套逻辑） -----

async def _list_files(db: AsyncSession, agent_id: int) -> list[dict]:
    rows = (
        (await db.execute(select(AgentFile).where(AgentFile.agent_id == agent_id, AgentFile.deleted_at.is_(None)))).scalars().all()
    )
    return [{"file_key": f.file_key, "content": f.content, "version": f.version, "updated_at": f.updated_at.isoformat()} for f in rows]


class FileIn(BaseModel):
    content: str


async def _save_file(db: AsyncSession, agent_id: int, file_key: str, content: str, saved_by: int | None) -> dict:
    if file_key not in AGENT_FILE_KEYS:
        raise HTTPException(400, f"file_key 必须是 {AGENT_FILE_KEYS} 之一")
    f = (
        await db.execute(
            select(AgentFile).where(AgentFile.agent_id == agent_id, AgentFile.file_key == file_key, AgentFile.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if f is None:
        f = AgentFile(agent_id=agent_id, file_key=file_key, content="", version=1, updated_by=saved_by)
        db.add(f)
        await db.flush()
    db.add(AgentFileVersion(agent_file_id=f.id, version=f.version, content=f.content, saved_by=f.updated_by, created_at=utcnow()))
    f.content = content
    f.version += 1
    f.updated_by = saved_by
    await db.commit()
    return {"file_key": f.file_key, "version": f.version}


async def _list_file_versions(db: AsyncSession, agent_id: int, file_key: str) -> list[dict]:
    f = (
        await db.execute(
            select(AgentFile).where(AgentFile.agent_id == agent_id, AgentFile.file_key == file_key, AgentFile.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if f is None:
        raise HTTPException(404, "文件不存在")
    versions = (
        (
            await db.execute(
                select(AgentFileVersion).where(AgentFileVersion.agent_file_id == f.id).order_by(AgentFileVersion.version.desc()).limit(50)
            )
        )
        .scalars()
        .all()
    )
    return [{"version": v.version, "created_at": v.created_at.isoformat()} for v in versions]


async def _rollback_file(db: AsyncSession, agent_id: int, file_key: str, version: int, saved_by: int) -> dict:
    f = (
        await db.execute(
            select(AgentFile).where(AgentFile.agent_id == agent_id, AgentFile.file_key == file_key, AgentFile.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if f is None:
        raise HTTPException(404, "文件不存在")
    v = (
        await db.execute(
            select(AgentFileVersion).where(AgentFileVersion.agent_file_id == f.id, AgentFileVersion.version == version)
        )
    ).scalar_one_or_none()
    if v is None:
        raise HTTPException(404, "目标版本不存在")
    db.add(AgentFileVersion(agent_file_id=f.id, version=f.version, content=f.content, saved_by=f.updated_by, created_at=utcnow()))
    f.content = v.content
    f.version += 1
    f.updated_by = saved_by
    await db.commit()
    return {"file_key": f.file_key, "version": f.version}


@my_router.get("/{agent_id}/files")
async def my_files(agent_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    await _get_own_instance(agent_id, user, db)
    return await _list_files(db, agent_id)


@my_router.put("/{agent_id}/files/{file_key}")
async def my_save_file(agent_id: int, file_key: str, body: FileIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _get_own_instance(agent_id, user, db)
    return await _save_file(db, agent_id, file_key, body.content, user.id)


@my_router.get("/{agent_id}/files/{file_key}/versions")
async def my_file_versions(agent_id: int, file_key: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    await _get_own_instance(agent_id, user, db)
    return await _list_file_versions(db, agent_id, file_key)


@my_router.post("/{agent_id}/files/{file_key}/rollback/{version}")
async def my_rollback(agent_id: int, file_key: str, version: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await _get_own_instance(agent_id, user, db)
    return await _rollback_file(db, agent_id, file_key, version, user.id)


# ----- 实例技能绑定 -----

def _skill_brief(s: Skill) -> dict:
    # instructions 与 SKILL.md 保持同步，员工只读查看平台技能内容用
    return {"id": s.id, "name": s.name, "description": s.description, "category": s.category, "owner_user_id": s.owner_user_id, "enabled": s.enabled, "instructions": s.instructions}


@my_router.get("/{agent_id}/skills")
async def my_agent_skills(agent_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """本实例的技能视图：已绑定（含完整内容，平台技能只读）+ 可绑定的平台技能 + 我的个人技能。"""
    a = await _get_own_instance(agent_id, user, db)
    bound_ids = set(
        (await db.execute(select(AgentSkill.skill_id).where(AgentSkill.agent_id == a.id, AgentSkill.deleted_at.is_(None)))).scalars().all()
    )
    all_skills = (
        (await db.execute(select(Skill).where(Skill.deleted_at.is_(None)).order_by(Skill.id.asc()))).scalars().all()
    )
    # 可见性：平台技能 + 我的个人技能 + **已绑定的技能**（派发/迁移来的他人个人技能也必须可见，否则显示"已绑定"却看不到内容）
    visible = [s for s in all_skills if s.owner_user_id is None or s.owner_user_id == user.id or s.id in bound_ids]
    return {
        "bound_ids": sorted(bound_ids),
        "skills": [_skill_brief(s) for s in visible],
    }


class SkillsBindIn(BaseModel):
    skill_ids: list[int]


async def _set_agent_skills(db: AsyncSession, agent_id: int, skill_ids: list[int], user: User) -> dict:
    existing = (
        (await db.execute(select(AgentSkill).where(AgentSkill.agent_id == agent_id))).scalars().all()
    )
    already_bound = {r.skill_id for r in existing if r.deleted_at is None}
    # 校验：新绑定的只能是平台技能或自己的个人技能（admin 不限）；已绑定的（如派发来的他人个人技能）保持不校验
    for sid in skill_ids:
        if sid in already_bound:
            continue
        s = (await db.execute(select(Skill).where(Skill.id == sid, Skill.deleted_at.is_(None)))).scalar_one_or_none()
        if s is None or (s.owner_user_id is not None and s.owner_user_id != user.id and user.role != "admin"):
            raise HTTPException(400, f"技能 #{sid} 不存在或不属于你")
    want = set(skill_ids)
    for row in existing:
        if row.skill_id in want:
            row.deleted_at = None  # 已绑定保持 / 软删的复活（重复插入会撞唯一约束）
        elif row.deleted_at is None:
            row.deleted_at = utcnow()
    have = {r.skill_id for r in existing}
    for sid in want - have:
        db.add(AgentSkill(agent_id=agent_id, skill_id=sid))
    await db.commit()
    return {"ok": True, "skill_ids": sorted(want)}


@my_router.put("/{agent_id}/skills")
async def my_set_skills(agent_id: int, body: SkillsBindIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    a = await _get_own_instance(agent_id, user, db)
    return await _set_agent_skills(db, a.id, body.skill_ids, user)


@my_router.post("/{agent_id}/sync-from-template")
async def sync_from_template(agent_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """用模板当前的 .md 与技能绑定覆盖实例（员工自己调教的内容会被覆盖，前端需确认）。"""
    a = await _get_own_instance(agent_id, user, db)
    if not a.template_id:
        raise HTTPException(400, "该数字人不是模板实例")
    t = await _get_template(db, a.template_id)
    t_files = (
        (await db.execute(select(AgentFile).where(AgentFile.agent_id == t.id, AgentFile.deleted_at.is_(None)))).scalars().all()
    )
    for tf in t_files:
        await _save_file(db, a.id, tf.file_key, tf.content, user.id)
    t_binds = set(
        (await db.execute(select(AgentSkill.skill_id).where(AgentSkill.agent_id == t.id, AgentSkill.deleted_at.is_(None)))).scalars().all()
    )
    await _set_agent_skills(db, a.id, sorted(t_binds), user)
    return {"ok": True}


# ---------- 管理端 ----------

class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    avatar: str | None = None
    description: str = ""
    default_model_id: int | None = None
    model_allowlist: list[int] | None = None
    allow_code_exec: bool = False
    allow_web_search: bool = False
    allow_api_request: bool = False
    allow_feishu: bool = False
    allow_zhishu: bool = False
    allow_image_generate: bool = False
    max_loop_rounds: int = Field(default=16, ge=1, le=100)


def _apply_agent_in(a: Agent, body: AgentIn) -> None:
    a.name = body.name
    a.avatar = body.avatar
    a.description = body.description
    a.default_model_id = body.default_model_id
    a.model_allowlist = json.dumps(body.model_allowlist) if body.model_allowlist else None
    a.allow_code_exec = body.allow_code_exec
    a.allow_web_search = body.allow_web_search
    a.allow_api_request = body.allow_api_request
    a.allow_feishu = body.allow_feishu
    a.allow_zhishu = body.allow_zhishu
    a.allow_image_generate = body.allow_image_generate
    a.max_loop_rounds = body.max_loop_rounds


def _admin_agent_item(a: Agent, owner: User | None, dept_names: dict[int, str], tpl_names: dict[int, str]) -> dict:
    """管理端列表行：基础字段 + 属主账号/部门 + 来源模板名 + 领用时间。"""
    item = _agent_out(a, owner.name if owner else None)
    item.update({
        "owner_username": owner.username if owner else None,
        "owner_department_id": owner.department_id if owner else None,
        "owner_department_name": dept_names.get(owner.department_id) if owner and owner.department_id else None,
        "template_name": tpl_names.get(a.template_id) if a.template_id else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    })
    return item


@admin_router.get("")
async def admin_list_agents(view: str = "all", db: AsyncSession = Depends(get_db), _: User = Depends(require_module("agents_admin"))) -> list[dict]:
    q = select(Agent).where(Agent.deleted_at.is_(None)).order_by(Agent.id.asc())
    if view == "templates":
        q = q.where(Agent.is_template.is_(True))
    elif view == "instances":
        q = q.where(Agent.is_template.is_(False))
    rows = ((await db.execute(q)).scalars().all())
    owners = await _owner_infos(db, rows)
    dept_names = await _dept_names(db, {u.department_id for u in owners.values() if u.department_id})
    # 实例 → 来源模板名（模板已删则为 None）
    tids = {a.template_id for a in rows if a.template_id}
    tpl_names: dict[int, str] = {}
    if tids:
        tpl_names = {
            i: n
            for i, n in (await db.execute(select(Agent.id, Agent.name).where(Agent.id.in_(tids), Agent.deleted_at.is_(None)))).all()
        }
    return [_admin_agent_item(a, owners.get(a.owner_user_id), dept_names, tpl_names) for a in rows]


@admin_router.post("", status_code=201)
async def create_template(body: AgentIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_module("agents_admin"))) -> dict:
    a = Agent(created_by=admin.id, is_template=True)
    _apply_agent_in(a, body)
    db.add(a)
    await db.flush()
    for key in AGENT_FILE_KEYS:
        db.add(AgentFile(agent_id=a.id, file_key=key, content="", version=1, updated_by=admin.id))
    await db.commit()
    return _agent_out(a)


@admin_router.patch("/{agent_id}")
async def update_agent(agent_id: int, body: AgentIn, db: AsyncSession = Depends(get_db), _: User = Depends(require_module("agents_admin"))) -> dict:
    a = (await db.execute(select(Agent).where(Agent.id == agent_id, Agent.deleted_at.is_(None)))).scalar_one_or_none()
    if a is None:
        raise HTTPException(404, "数字人不存在")
    _apply_agent_in(a, body)
    await db.commit()
    return _agent_out(a)


@admin_router.post("/{agent_id}/archive")
async def archive_agent(agent_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(require_module("agents_admin"))) -> dict:
    a = (await db.execute(select(Agent).where(Agent.id == agent_id, Agent.deleted_at.is_(None)))).scalar_one_or_none()
    if a is None:
        raise HTTPException(404, "数字人不存在")
    a.status = "archived" if a.status == "active" else "active"
    await db.commit()
    return {"status": a.status}


class DispatchIn(BaseModel):
    user_id: int


@admin_router.post("/{agent_id}/dispatch", status_code=201)
async def dispatch(agent_id: int, body: DispatchIn, db: AsyncSession = Depends(get_db), _: User = Depends(require_module("agents_admin"))) -> dict:
    """管理员把模板派发给指定员工（不受可领用范围限制）。"""
    t = await _get_template(db, agent_id)
    owner = (await db.execute(select(User).where(User.id == body.user_id, User.deleted_at.is_(None)))).scalar_one_or_none()
    if owner is None:
        raise HTTPException(404, "用户不存在")
    inst = await create_instance(db, template=t, owner=owner)
    return _agent_out(inst, owner.name)


@admin_router.post("/{agent_id}/set-master")
async def set_master(agent_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(require_module("agents_admin"))) -> dict:
    """设置/取消主 agent。平台同一时间只有一个主 agent。"""
    a = (await db.execute(select(Agent).where(Agent.id == agent_id, Agent.deleted_at.is_(None)))).scalar_one_or_none()
    if a is None:
        raise HTTPException(404, "数字人不存在")
    if a.is_master:
        a.is_master = False
    else:
        others = ((await db.execute(select(Agent).where(Agent.is_master.is_(True), Agent.deleted_at.is_(None)))).scalars().all())
        for o in others:
            o.is_master = False
        a.is_master = True
    await db.commit()
    return {"is_master": a.is_master}


# ----- 可领用范围（claim grants） -----

@admin_router.get("/{agent_id}/claim-grants")
async def list_claim_grants(agent_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(require_module("agents_admin"))) -> list[dict]:
    rows = (
        (await db.execute(select(AgentClaimGrant).where(AgentClaimGrant.agent_id == agent_id, AgentClaimGrant.deleted_at.is_(None))))
        .scalars()
        .all()
    )
    return [{"id": g.id, "grantee_type": g.grantee_type, "grantee_id": g.grantee_id} for g in rows]


class ClaimGrantIn(BaseModel):
    grantee_type: str  # user / department / all
    grantee_id: int | None = None


@admin_router.post("/{agent_id}/claim-grants", status_code=201)
async def create_claim_grant(agent_id: int, body: ClaimGrantIn, db: AsyncSession = Depends(get_db), _: User = Depends(require_module("agents_admin"))) -> dict:
    if body.grantee_type not in ("user", "department", "all"):
        raise HTTPException(400, "grantee_type 必须是 user/department/all")
    g = AgentClaimGrant(agent_id=agent_id, grantee_type=body.grantee_type, grantee_id=body.grantee_id)
    db.add(g)
    await db.commit()
    return {"id": g.id, "grantee_type": g.grantee_type, "grantee_id": g.grantee_id}


@admin_router.delete("/claim-grants/{gid}", status_code=204)
async def delete_claim_grant(gid: int, db: AsyncSession = Depends(get_db), _: User = Depends(require_module("agents_admin"))) -> None:
    g = (await db.execute(select(AgentClaimGrant).where(AgentClaimGrant.id == gid, AgentClaimGrant.deleted_at.is_(None)))).scalar_one_or_none()
    if g is None:
        raise HTTPException(404, "授权不存在")
    g.deleted_at = utcnow()
    await db.commit()


# ----- 管理端的文件/技能编辑（复用共享逻辑） -----

@admin_router.get("/{agent_id}/files")
async def admin_list_files(agent_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(require_module("agents_admin"))) -> list[dict]:
    return await _list_files(db, agent_id)


@admin_router.put("/{agent_id}/files/{file_key}")
async def admin_save_file(agent_id: int, file_key: str, body: FileIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_module("agents_admin"))) -> dict:
    return await _save_file(db, agent_id, file_key, body.content, admin.id)


@admin_router.get("/{agent_id}/files/{file_key}/versions")
async def admin_file_versions(agent_id: int, file_key: str, db: AsyncSession = Depends(get_db), _: User = Depends(require_module("agents_admin"))) -> list[dict]:
    return await _list_file_versions(db, agent_id, file_key)


@admin_router.post("/{agent_id}/files/{file_key}/rollback/{version}")
async def admin_rollback(agent_id: int, file_key: str, version: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_module("agents_admin"))) -> dict:
    return await _rollback_file(db, agent_id, file_key, version, admin.id)


@admin_router.get("/{agent_id}/skills")
async def admin_agent_skills(agent_id: int, db: AsyncSession = Depends(get_db), _: User = Depends(require_module("agents_admin"))) -> dict:
    """管理端技能视图：绑定 id + 全部技能清单（含个人技能及归属名）——管理员可见所有技能。"""
    bound_ids = set(
        (await db.execute(select(AgentSkill.skill_id).where(AgentSkill.agent_id == agent_id, AgentSkill.deleted_at.is_(None)))).scalars().all()
    )
    all_skills = (
        (await db.execute(select(Skill).where(Skill.deleted_at.is_(None)).order_by(Skill.id.asc()))).scalars().all()
    )
    owners = {s.owner_user_id for s in all_skills if s.owner_user_id}
    owner_names = {}
    if owners:
        owner_names = {i: n for i, n in (await db.execute(select(User.id, User.name).where(User.id.in_(owners)))).all()}
    return {
        "bound_ids": sorted(bound_ids),
        "skills": [
            {**_skill_brief(s), "owner_name": owner_names.get(s.owner_user_id) if s.owner_user_id else None}
            for s in all_skills
        ],
    }


@admin_router.put("/{agent_id}/skills")
async def admin_set_agent_skills(agent_id: int, body: SkillsBindIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_module("agents_admin"))) -> dict:
    # 管理端不限制技能归属；软删行复活而非重复插入（唯一约束）
    existing = (
        (await db.execute(select(AgentSkill).where(AgentSkill.agent_id == agent_id))).scalars().all()
    )
    want = set(body.skill_ids)
    for row in existing:
        if row.skill_id in want:
            row.deleted_at = None
        elif row.deleted_at is None:
            row.deleted_at = utcnow()
    have = {r.skill_id for r in existing}
    for sid in want - have:
        db.add(AgentSkill(agent_id=agent_id, skill_id=sid))
    await db.commit()
    return {"ok": True, "skill_ids": sorted(want)}
