"""API 域名白名单管理端（M4d）：api_request 工具的出口管控 + 认证配置。

红线：auth_config（密钥/账号密码）只进不出——存 Fernet 密文，任何响应不回传；
编辑时 auth_config 传 null/不传 = 不修改，前端永远拿不到旧值。
"""
import json
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_admin
from app.core.security import encrypt_secret
from app.models.base import utcnow
from app.models.gateway import ApiHost
from app.models.user import AuditLog, User

router = APIRouter(prefix="/api/admin/api-hosts", tags=["admin-api-hosts"])

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$")
_AUTH_TYPES = {"none", "headers", "login_session"}
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


async def _audit(db: AsyncSession, actor: User, action: str, target_id: object, detail: dict) -> None:
    db.add(AuditLog(actor_user_id=actor.id, action=action, target_type="api_host", target_id=str(target_id), detail=json.dumps(detail, ensure_ascii=False)))


def _normalize_host(raw: str) -> str:
    """容忍管理员粘贴完整 URL：提取纯域名小写。"""
    h = (raw or "").strip().lower()
    h = re.sub(r"^https?://", "", h).split("/")[0].split("?")[0]
    return h


def _validate_auth(auth_type: str, auth_config: dict | None) -> dict:
    """校验认证配置结构；返回可入库的规范化 dict。密钥值原样保留（落库前统一 Fernet）。"""
    if auth_type not in _AUTH_TYPES:
        raise HTTPException(400, f"auth_type 必须是 {'/'.join(sorted(_AUTH_TYPES))} 之一")
    cfg = auth_config or {}
    if auth_type == "none":
        return {}
    headers = cfg.get("headers") or {}
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise HTTPException(400, "headers 必须是 字符串→字符串 的对象")
    if auth_type == "headers":
        if not headers:
            raise HTTPException(400, "固定请求头认证至少要有一个请求头")
        return {"headers": headers}
    # login_session：门禁头可选，登录路径与登录体必填
    login_path = str(cfg.get("login_path") or "")
    login_body = cfg.get("login_body")
    if not login_path.startswith("/"):
        raise HTTPException(400, "login_path 必须以 / 开头（如 /api/auth/login）")
    if not isinstance(login_body, dict) or not login_body:
        raise HTTPException(400, "login_body 必须是非空 JSON 对象（如 {\"phone_number\": \"...\", \"password\": \"...\"}）")
    return {"headers": headers, "login_path": login_path, "login_body": login_body}


def _validate_methods(raw: str) -> str:
    methods = {m.strip().upper() for m in (raw or "").split(",") if m.strip()}
    bad = methods - _METHODS
    if bad or not methods:
        raise HTTPException(400, f"allowed_methods 含非法方法：{','.join(sorted(bad)) or '空'}（可选：{','.join(sorted(_METHODS))}）")
    return ",".join(sorted(methods))


def _host_out(h: ApiHost) -> dict:
    return {
        "id": h.id, "host": h.host, "label": h.label, "auth_type": h.auth_type,
        "has_auth_config": bool(h.auth_config_enc),
        "allowed_methods": h.allowed_methods, "min_interval_ms": h.min_interval_ms, "enabled": h.enabled,
    }


def _drop_session_cache(host_id: int) -> None:
    """配置变更/删除后丢掉缓存的登录 cookie（下次调用按新配置重新登录）。"""
    from app.runtime import tools as _tools

    _tools._API_SESSIONS.pop(host_id, None)


class ApiHostIn(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    label: str = Field(default="", max_length=128)
    auth_type: str = "none"
    auth_config: dict | None = None  # headers / login_session 必填；none 忽略
    allowed_methods: str = "GET,POST"
    min_interval_ms: int = Field(default=0, ge=0, le=60000)
    enabled: bool = True


class ApiHostUpdate(BaseModel):
    host: str | None = None
    label: str | None = None
    auth_type: str | None = None
    auth_config: dict | None = None  # 不传=不修改；auth_type 变更时必须一起传
    allowed_methods: str | None = None
    min_interval_ms: int | None = Field(default=None, ge=0, le=60000)
    enabled: bool | None = None


@router.get("")
async def list_hosts(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)) -> list[dict]:
    rows = (await db.execute(select(ApiHost).where(ApiHost.deleted_at.is_(None)).order_by(ApiHost.id.asc()))).scalars().all()
    return [_host_out(h) for h in rows]


@router.post("", status_code=201)
async def create_host(body: ApiHostIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> dict:
    host = _normalize_host(body.host)
    if not _HOST_RE.match(host):
        raise HTTPException(400, f"域名不合法：{host}（填纯域名，如 api.example.com）")
    dup = (await db.execute(select(ApiHost.id).where(ApiHost.host == host, ApiHost.deleted_at.is_(None)))).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(400, f"域名 {host} 已在白名单内")
    cfg = _validate_auth(body.auth_type, body.auth_config)
    h = ApiHost(
        host=host,
        label=body.label.strip(),
        auth_type=body.auth_type,
        auth_config_enc=encrypt_secret(json.dumps(cfg, ensure_ascii=False)) if cfg else "",
        allowed_methods=_validate_methods(body.allowed_methods),
        min_interval_ms=body.min_interval_ms,
        enabled=body.enabled,
    )
    db.add(h)
    await db.flush()
    await _audit(db, admin, "api_host.create", h.id, {"host": h.host, "auth_type": h.auth_type})
    await db.commit()
    return _host_out(h)


@router.patch("/{hid}")
async def update_host(hid: int, body: ApiHostUpdate, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> dict:
    h = (await db.execute(select(ApiHost).where(ApiHost.id == hid, ApiHost.deleted_at.is_(None)))).scalar_one_or_none()
    if h is None:
        raise HTTPException(404, "白名单条目不存在")
    changed = body.model_dump(exclude_none=True)
    if "host" in changed:
        host = _normalize_host(changed["host"])
        if not _HOST_RE.match(host):
            raise HTTPException(400, f"域名不合法：{host}")
        dup = (await db.execute(select(ApiHost.id).where(ApiHost.host == host, ApiHost.deleted_at.is_(None), ApiHost.id != hid))).scalar_one_or_none()
        if dup is not None:
            raise HTTPException(400, f"域名 {host} 已在白名单内")
        h.host = host
    if "label" in changed:
        h.label = changed["label"].strip()
    if "allowed_methods" in changed:
        h.allowed_methods = _validate_methods(changed["allowed_methods"])
    if "min_interval_ms" in changed:
        h.min_interval_ms = changed["min_interval_ms"]
    if "enabled" in changed:
        h.enabled = changed["enabled"]
    if "auth_type" in changed:
        # 换认证类型必须同时给新配置（none 除外），防止 类型=login_session 但配置还是旧 headers 的错位
        new_type = changed["auth_type"]
        cfg = _validate_auth(new_type, changed.get("auth_config") if "auth_config" in changed else ({} if new_type == "none" else None))
        h.auth_type = new_type
        h.auth_config_enc = encrypt_secret(json.dumps(cfg, ensure_ascii=False)) if cfg else ""
        _drop_session_cache(h.id)
    elif "auth_config" in changed:
        cfg = _validate_auth(h.auth_type, changed["auth_config"])
        h.auth_config_enc = encrypt_secret(json.dumps(cfg, ensure_ascii=False)) if cfg else ""
        _drop_session_cache(h.id)
    await _audit(db, admin, "api_host.update", h.id, {"fields": sorted(changed)})
    await db.commit()
    return _host_out(h)


@router.delete("/{hid}", status_code=204)
async def delete_host(hid: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> None:
    h = (await db.execute(select(ApiHost).where(ApiHost.id == hid, ApiHost.deleted_at.is_(None)))).scalar_one_or_none()
    if h is None:
        raise HTTPException(404, "白名单条目不存在")
    h.deleted_at = utcnow()
    _drop_session_cache(h.id)
    await _audit(db, admin, "api_host.delete", hid, {"host": h.host})
    await db.commit()
