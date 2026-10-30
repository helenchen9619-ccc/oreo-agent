"""模块权限：按角色勾选开放模块（用户的 1.1 想法）。

require_module("agents_admin") 作为 FastAPI 依赖使用。
"""
from collections.abc import Callable

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.agent import RoleModule
from app.models.user import User


async def get_user_modules(db: AsyncSession, user: User) -> list[str]:
    rows = (
        (await db.execute(select(RoleModule.module).where(RoleModule.role == user.role, RoleModule.deleted_at.is_(None))))
        .scalars()
        .all()
    )
    return list(rows)


def require_module(module: str) -> Callable:
    async def checker(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> User:
        modules = await get_user_modules(db, user)
        if module not in modules:
            raise HTTPException(403, f"你的角色未开通「{module}」模块，请联系 IT 信息部")
        return user

    return checker
