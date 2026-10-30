"""维护模式（M15）：管理员一键开关，开启后非 admin 的全部已认证请求被 503 拦截，
员工前端整屏显示「系统更新升级中」；管理员不受影响可照常测试。

状态存 system_settings 键值表（key='maintenance'，value '1'/'0'），进程内 5s TTL 缓存
避免每请求一次 DB 往返（单 worker 假设，与 chat._ACTIVE_TURNS 同级）。
"""
import json
import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_admin
from app.models.system import SystemSetting
from app.models.user import AuditLog, User

router = APIRouter(tags=["system"])

_KEY = "maintenance"
_CACHE_TTL_S = 5.0
_cache: dict = {"value": False, "at": 0.0}


def _clear_cache() -> None:
    _cache["at"] = 0.0


async def is_maintenance_on(db: AsyncSession) -> bool:
    """维护模式是否开启（5s TTL 进程内缓存）。"""
    if time.monotonic() - _cache["at"] < _CACHE_TTL_S:
        return _cache["value"]
    row = (
        await db.execute(select(SystemSetting.value).where(SystemSetting.key == _KEY, SystemSetting.deleted_at.is_(None)))
    ).scalar_one_or_none()
    _cache["value"] = row == "1"
    _cache["at"] = time.monotonic()
    return _cache["value"]


@router.get("/api/system/status")
async def system_status(db: AsyncSession = Depends(get_db)) -> dict:
    """公开端点（无认证）：维护期间员工也要能轮询到恢复。"""
    return {"maintenance": await is_maintenance_on(db)}


class MaintenanceIn(BaseModel):
    enabled: bool


@router.put("/api/admin/system/maintenance")
async def set_maintenance(body: MaintenanceIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> dict:
    row = (
        await db.execute(select(SystemSetting).where(SystemSetting.key == _KEY, SystemSetting.deleted_at.is_(None)))
    ).scalar_one_or_none()
    value = "1" if body.enabled else "0"
    if row is None:
        row = SystemSetting(key=_KEY, value=value)
        db.add(row)
    else:
        row.value = value
    db.add(AuditLog(
        actor_user_id=admin.id,
        action="admin.maintenance_toggle",
        target_type="system",
        target_id=_KEY,
        detail=json.dumps({"enabled": body.enabled}, ensure_ascii=False),
    ))
    await db.commit()
    _clear_cache()  # 立即生效，不等 TTL
    return {"maintenance": body.enabled}
