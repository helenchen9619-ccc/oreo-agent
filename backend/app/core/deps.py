from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.user import User

bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    cred: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    """每个请求必须带用户身份——这是所有隔离与授权的根基（修正 OpenClaw"认证即完全信任"）。"""
    if cred is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    user_id = decode_access_token(cred.credentials)
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已失效")
    user = (await db.execute(select(User).where(User.id == user_id, User.deleted_at.is_(None)))).scalar_one_or_none()
    if user is None or user.status != "active":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号不可用")
    # M15 维护模式：非 admin 一律 503（detail 带 maintenance 标记，前端据此整屏显示升级页）；
    # 管理员放行以便部署后测试。导入放函数内避免循环依赖（system.router 依赖本模块的 require_admin）。
    if user.role != "admin":
        from app.system.router import is_maintenance_on
        if await is_maintenance_on(db):
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"maintenance": True, "msg": "系统升级维护中，请稍后再试"},
            )
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    return user
