"""组织与账号管理（M3.12，仅 admin）：部门 CRUD + 人员账号管理。

地基（departments 表 / users.department_id / 按部门授权判定）M1 已就绪，本模块补写入路径。
飞书通讯录同步归 M4（feishu_dept_id / feishu_open_id 锚点字段已预留）。
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_admin
from app.core.security import hash_password
from app.models.agent import AgentClaimGrant
from app.models.base import utcnow
from app.models.gateway import ModelGrant, Quota
from app.models.user import AuditLog, Department, User

router = APIRouter(prefix="/api/admin", tags=["admin-org"])


def _audit(db: AsyncSession, admin: User, action: str, target_type: str, target_id, detail: dict) -> None:
    db.add(AuditLog(
        actor_user_id=admin.id,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        detail=json.dumps(detail, ensure_ascii=False),
    ))


# ---------- 部门 CRUD ----------


def _dept_out(d: Department, user_counts: dict[int, int]) -> dict:
    return {
        "id": d.id,
        "name": d.name,
        "parent_id": d.parent_id,
        "feishu_dept_id": d.feishu_dept_id,
        "user_count": user_counts.get(d.id, 0),
    }


async def _get_dept(db: AsyncSession, dept_id: int) -> Department:
    d = (await db.execute(select(Department).where(Department.id == dept_id, Department.deleted_at.is_(None)))).scalar_one_or_none()
    if d is None:
        raise HTTPException(404, "部门不存在")
    return d


async def _user_counts(db: AsyncSession) -> dict[int, int]:
    rows = (
        await db.execute(
            select(User.department_id, func.count())
            .where(User.deleted_at.is_(None), User.department_id.is_not(None))
            .group_by(User.department_id)
        )
    ).all()
    return {dept_id: n for dept_id, n in rows}


@router.get("/departments")
async def list_departments(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)) -> list[dict]:
    rows = ((await db.execute(select(Department).where(Department.deleted_at.is_(None)).order_by(Department.id.asc()))).scalars().all())
    counts = await _user_counts(db)
    return [_dept_out(d, counts) for d in rows]


class DeptIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    parent_id: int | None = None


async def _check_dept_in(db: AsyncSession, body: DeptIn, exclude_id: int | None = None) -> None:
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "部门名不能为空")
    q = select(Department).where(
        Department.deleted_at.is_(None),
        Department.name == name,
        Department.parent_id.is_(None) if body.parent_id is None else Department.parent_id == body.parent_id,
    )
    if exclude_id is not None:
        q = q.where(Department.id != exclude_id)
    if (await db.execute(q)).scalar_one_or_none() is not None:
        raise HTTPException(400, "同级下已存在同名部门")
    if body.parent_id is not None:
        await _get_dept(db, body.parent_id)


@router.post("/departments", status_code=201)
async def create_department(body: DeptIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> dict:
    await _check_dept_in(db, body)
    d = Department(name=body.name.strip(), parent_id=body.parent_id)
    db.add(d)
    await db.flush()
    _audit(db, admin, "admin.dept_create", "department", d.id, {"name": d.name, "parent_id": d.parent_id})
    await db.commit()
    return _dept_out(d, {})


async def _is_descendant(db: AsyncSession, dept_id: int, maybe_ancestor_id: int) -> bool:
    """dept_id 是否位于 maybe_ancestor_id 的子树里（含自身）。"""
    rows = (await db.execute(select(Department.id, Department.parent_id).where(Department.deleted_at.is_(None)))).all()
    return creates_cycle(maybe_ancestor_id, dept_id, dict(rows))


def creates_cycle(dept_id: int, new_parent_id: int | None, parent_map: dict[int, int | None]) -> bool:
    """把 dept_id 的父级改成 new_parent_id 是否会成环（纯函数，沿父链走到根）。"""
    cur = new_parent_id
    while cur is not None:
        if cur == dept_id:
            return True
        cur = parent_map.get(cur)
    return False


@router.patch("/departments/{dept_id}")
async def update_department(dept_id: int, body: DeptIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> dict:
    d = await _get_dept(db, dept_id)
    if body.parent_id is not None and await _is_descendant(db, body.parent_id, dept_id):
        raise HTTPException(400, "不能把部门移动到它自己的子部门下")
    await _check_dept_in(db, body, exclude_id=dept_id)
    old = {"name": d.name, "parent_id": d.parent_id}
    d.name = body.name.strip()
    d.parent_id = body.parent_id
    _audit(db, admin, "admin.dept_update", "department", d.id, {"from": old, "to": {"name": d.name, "parent_id": d.parent_id}})
    await db.commit()
    return _dept_out(d, {})


@router.delete("/departments/{dept_id}", status_code=204)
async def delete_department(dept_id: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> None:
    """删除前必须无引用（子部门/用户/授权/配额）——不做级联，防误删。"""
    d = await _get_dept(db, dept_id)
    if (await db.execute(select(Department.id).where(Department.parent_id == dept_id, Department.deleted_at.is_(None)).limit(1))).scalar_one_or_none() is not None:
        raise HTTPException(400, "请先删除或移动其子部门")
    if (await db.execute(select(User.id).where(User.department_id == dept_id, User.deleted_at.is_(None)).limit(1))).scalar_one_or_none() is not None:
        raise HTTPException(400, "该部门下还有用户，请先调整他们的部门")
    for model, label in ((AgentClaimGrant, "数字人领用范围"), (ModelGrant, "模型授权"), (Quota, "配额")):
        col = model.grantee_type if hasattr(model, "grantee_type") else model.scope_type
        col_id = model.grantee_id if hasattr(model, "grantee_type") else model.scope_id
        q = select(model.id).where(col == "department", col_id == dept_id, model.deleted_at.is_(None)).limit(1)
        if (await db.execute(q)).scalar_one_or_none() is not None:
            raise HTTPException(400, f"该部门仍被「{label}」引用，请先解除")
    d.deleted_at = utcnow()
    _audit(db, admin, "admin.dept_delete", "department", d.id, {"name": d.name})
    await db.commit()


# ---------- 人员账号管理 ----------


def _user_out(u: User, dept_names: dict[int, str]) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "name": u.name,
        "role": u.role,
        "status": u.status,
        "department_id": u.department_id,
        "department_name": dept_names.get(u.department_id) if u.department_id else None,
        "feishu_bound": bool(u.feishu_open_id),
        "feishu_open_id": u.feishu_open_id,  # 管理端排障/白名单抄录用（仅 admin 可见本接口）
        "zhishu_user_id": u.zhishu_user_id,  # M9c：飞书 user_id（用户ID）＝智书 user_id，登录自动抓取；非飞书用户可手动补
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


async def _dept_names(db: AsyncSession) -> dict[int, str]:
    rows = (await db.execute(select(Department.id, Department.name).where(Department.deleted_at.is_(None)))).all()
    return {i: n for i, n in rows}


@router.get("/users")
async def admin_list_users(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)) -> list[dict]:
    rows = ((await db.execute(select(User).where(User.deleted_at.is_(None)).order_by(User.id.asc()))).scalars().all())
    names = await _dept_names(db)
    return [_user_out(u, names) for u in rows]


class UserCreateIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    role: str = "member"
    department_id: int | None = None
    password: str | None = Field(default=None, max_length=128)  # M14b：选填，设置后可用账号密码登录


def validate_password(password: str | None) -> str | None:
    """密码校验（纯函数）：None/空白 → None（不设密码）；否则最少 6 位，返回原文（哈希在调用方做）。"""
    pwd = (password or "").strip()
    if not pwd:
        return None
    if len(pwd) < 6:
        raise HTTPException(400, "密码至少 6 位")
    return pwd


@router.post("/users", status_code=201)
async def admin_create_user(body: UserCreateIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> dict:
    if body.role not in ("admin", "member"):
        raise HTTPException(400, "role 必须是 admin/member")
    username = body.username.strip()
    if not username:
        raise HTTPException(400, "用户名不能为空")
    if (await db.execute(select(User).where(User.username == username, User.deleted_at.is_(None)))).scalar_one_or_none() is not None:
        raise HTTPException(400, "用户名已存在")
    if body.department_id is not None:
        await _get_dept(db, body.department_id)
    u = User(username=username, name=body.name.strip(), role=body.role, department_id=body.department_id)
    pwd = validate_password(body.password)
    if pwd is not None:
        u.password_hash = hash_password(pwd)
    db.add(u)
    await db.flush()
    # 审计不落密码本体，只记录是否设置
    _audit(db, admin, "admin.user_create", "user", u.id,
           {"username": u.username, "role": u.role, "department_id": u.department_id, "password_set": pwd is not None})
    await db.commit()
    return _user_out(u, await _dept_names(db))


class UserPatchIn(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    role: str | None = None
    department_id: int | None = None
    set_department: bool = False  # True 时才动 department_id（区分"不传"与"清空"）
    status: str | None = None
    unbind_feishu: bool = False  # True 时解除飞书绑定（用户可再扫码按姓名认领）
    zhishu_user_id: str | None = None  # M9c：智书 user_id；空串 = 清空
    set_zhishu_user_id: bool = False  # True 时才动 zhishu_user_id（区分"不传"与"清空"）
    password: str | None = Field(default=None, max_length=128)  # M14b：重置密码；空串 = 清除密码（回到仅飞书登录）
    set_password: bool = False  # True 时才动 password_hash


def validate_zhishu_user_id(zsid: str | None) -> str | None:
    """校验并归一化智书 user_id（纯函数）：空 → None（清空）；否则须为小写字母数字串。"""
    zsid = (zsid or "").strip()
    if not zsid:
        return None
    if not all(c in "abcdefghijklmnopqrstuvwxyz0123456789" for c in zsid):
        raise HTTPException(400, "智书 user_id 格式不对（应为小写字母数字串，如 944f4998）")
    return zsid


def check_admin_lockout(actor_id: int, target: User, new_role: str, new_status: str, other_active_admins: int) -> None:
    """防锁死守卫（纯函数）：不能禁用/降级自己；系统至少保留 1 个可用管理员。"""
    if actor_id == target.id and (new_status != "active" or new_role != "admin"):
        raise HTTPException(400, "不能禁用或降级自己的账号")
    if (
        target.role == "admin" and target.status == "active"
        and (new_role != "admin" or new_status != "active")
        and other_active_admins == 0
    ):
        raise HTTPException(400, "系统至少需要保留 1 个可用管理员")


@router.patch("/users/{user_id}")
async def admin_update_user(user_id: int, body: UserPatchIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> dict:
    u = ((await db.execute(select(User).where(User.id == user_id, User.deleted_at.is_(None)))).scalar_one_or_none())
    if u is None:
        raise HTTPException(404, "用户不存在")

    new_role = body.role if body.role is not None else u.role
    new_status = body.status if body.status is not None else u.status
    if body.role is not None and body.role not in ("admin", "member"):
        raise HTTPException(400, "role 必须是 admin/member")
    if body.status is not None and body.status not in ("active", "disabled"):
        raise HTTPException(400, "status 必须是 active/disabled")

    # 防锁死：不能禁用/降级自己；系统至少保留 1 个 active admin
    other_admins = 0
    if u.role == "admin" and u.status == "active" and (new_role != "admin" or new_status != "active"):
        other_admins = (
            await db.execute(
                select(func.count()).select_from(User).where(
                    User.deleted_at.is_(None), User.role == "admin", User.status == "active", User.id != u.id
                )
            )
        ).scalar_one()
    check_admin_lockout(admin.id, u, new_role, new_status, other_admins)

    if body.set_department and body.department_id is not None:
        await _get_dept(db, body.department_id)

    changes: dict = {}
    if body.name is not None and body.name.strip() and body.name.strip() != u.name:
        changes["name"] = {"from": u.name, "to": body.name.strip()}
        u.name = body.name.strip()
    if new_role != u.role:
        changes["role"] = {"from": u.role, "to": new_role}
        u.role = new_role
    if new_status != u.status:
        changes["status"] = {"from": u.status, "to": new_status}
        u.status = new_status
    if body.set_department and body.department_id != u.department_id:
        changes["department_id"] = {"from": u.department_id, "to": body.department_id}
        u.department_id = body.department_id
    if body.set_zhishu_user_id:
        new_zsid = validate_zhishu_user_id(body.zhishu_user_id)
        if new_zsid != u.zhishu_user_id:
            changes["zhishu_user_id"] = {"from": u.zhishu_user_id, "to": new_zsid}
            u.zhishu_user_id = new_zsid
    if body.set_password:
        new_pwd = validate_password(body.password)
        new_hash = hash_password(new_pwd) if new_pwd is not None else None
        if new_hash != u.password_hash:
            # 审计只记动作（reset/cleared），不落密码与哈希
            changes["password"] = "reset" if new_pwd is not None else "cleared"
            u.password_hash = new_hash
    unbound = False
    if body.unbind_feishu and u.feishu_open_id:
        unbound = True
        u.feishu_open_id = None

    if changes or unbound:
        if unbound:
            _audit(db, admin, "admin.user_unbind_feishu", "user", u.id, {"username": u.username})
        if changes:
            action = "admin.user_disable" if changes.get("status", {}).get("to") == "disabled" else "admin.user_update"
            _audit(db, admin, action, "user", u.id, changes)
        await db.commit()
    return _user_out(u, await _dept_names(db))
