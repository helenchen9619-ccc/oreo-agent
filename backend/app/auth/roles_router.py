"""角色-模块权限管理（仅 admin）。"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_admin
from app.models.agent import MODULES, MODULE_NAMES, RoleModule
from app.models.base import utcnow
from app.models.user import User

router = APIRouter(prefix="/api/admin/roles", tags=["admin-roles"])
ROLES = ("admin", "member")
ROLE_NAMES = {"admin": "管理员", "member": "员工"}


@router.get("/modules")
async def get_matrix(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)) -> dict:
    rows = ((await db.execute(select(RoleModule).where(RoleModule.deleted_at.is_(None)))).scalars().all())
    matrix: dict[str, list[str]] = {r: [] for r in ROLES}
    for row in rows:
        matrix.setdefault(row.role, []).append(row.module)
    return {
        "roles": [{"key": r, "name": ROLE_NAMES[r]} for r in ROLES],
        "modules": [{"key": m, "name": MODULE_NAMES[m]} for m in MODULES],
        "matrix": matrix,
    }


class MatrixIn(BaseModel):
    role: str
    modules: list[str]


@router.put("/modules")
async def set_role_modules(body: MatrixIn, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)) -> dict:
    if body.role not in ROLES:
        return {"ok": False, "detail": "未知角色"}
    if body.role == "admin" and "gateway_admin" not in body.modules:
        return {"ok": False, "detail": "admin 必须保留模型网关权限，防止把自己锁死"}
    invalid = set(body.modules) - set(MODULES)
    if invalid:
        return {"ok": False, "detail": f"未知模块: {invalid}"}
    existing = (
        (await db.execute(select(RoleModule).where(RoleModule.role == body.role, RoleModule.deleted_at.is_(None)))).scalars().all()
    )
    want = set(body.modules)
    for row in existing:
        if row.module not in want:
            row.deleted_at = utcnow()
    have = {r.module for r in existing}
    for m in want - have:
        db.add(RoleModule(role=body.role, module=m))
    await db.commit()
    return {"ok": True}
