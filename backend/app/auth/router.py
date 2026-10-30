import json
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import feishu
from app.feishu.tokens import store_feishu_tokens
from app.core.config import get_settings
from app.core.database import get_db
from app.core.deps import get_current_user, require_admin
from app.core.security import create_access_token, verify_password
from app.models.user import AuditLog, User

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()

# 内容权限 scope 前缀（M9b 修复：普通登录不带 scope，若无条件覆盖会把升级授权拿到的内容 token 清空）
_CONTENT_SCOPE_PREFIXES = ("mail:", "im:", "docx:", "wiki:", "drive:", "search:", "contact:", "approval:")


def _has_content_scope(scope: str) -> bool:
    return any(p in (scope or "") for p in _CONTENT_SCOPE_PREFIXES)


class DevLoginIn(BaseModel):
    username: str


class PasswordLoginIn(BaseModel):
    username: str
    password: str


class FeishuCallbackIn(BaseModel):
    code: str


class TokenOut(BaseModel):
    token: str
    user: dict


def _user_dict(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "name": u.name,
        "avatar_url": u.avatar_url,
        "role": u.role,
        "department_id": u.department_id,
        "zhishu_user_id": u.zhishu_user_id,
    }


def _issue_token(user: User) -> TokenOut:
    return TokenOut(token=create_access_token(user.id), user=_user_dict(user))


@router.get("/config")
async def auth_config() -> dict:
    """前端据此决定展示飞书登录还是 dev 登录。"""
    return {
        "feishu_enabled": bool(settings.FEISHU_APP_ID),
        "dev_login_enabled": settings.DEV_LOGIN_ENABLED,
        "password_login_enabled": settings.PASSWORD_LOGIN_ENABLED,
    }


@router.get("/feishu/authorize-url")
async def feishu_authorize_url(redirect_uri: str, upgrade: bool = False) -> dict:
    """upgrade=1 时显式申请 M7 数据 scope（增量授权）；普通登录不带 scope（保持最小权限、避免报错打死登录）。"""
    if not settings.FEISHU_APP_ID:
        raise HTTPException(400, "未配置飞书应用")
    return {"url": feishu.build_authorize_url(redirect_uri, scope=feishu.FEISHU_USER_SCOPES if upgrade else "")}


FeishuAction = Literal["login", "claim", "create_admin", "reject_disabled", "reject_unauthorized", "reject_ambiguous"]


def decide_feishu_login(bound_user: User | None, candidates: list[User], is_admin_openid: bool) -> FeishuAction:
    """飞书回调决策（纯函数）：
    - 已绑定 open_id：disabled → reject_disabled，否则 login；
    - 未绑定：恰好 1 个候选（未绑定+active+同名）→ claim 认领；
      0 候选 → 白名单 create_admin（冷启动兜底），否则 reject_unauthorized（默认拒绝，不自动建档）；
      ≥2 候选（重名歧义）→ reject_ambiguous。
    """
    if bound_user is not None:
        return "reject_disabled" if bound_user.status == "disabled" else "login"
    if len(candidates) == 1:
        return "claim"
    if len(candidates) >= 2:
        return "reject_ambiguous"
    return "create_admin" if is_admin_openid else "reject_unauthorized"


@router.post("/feishu/callback", response_model=TokenOut)
async def feishu_callback(body: FeishuCallbackIn, db: AsyncSession = Depends(get_db)) -> TokenOut:
    try:
        info = await feishu.fetch_feishu_user(body.code)
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e

    is_admin_openid = info["open_id"] in settings.admin_open_ids
    bound = (
        await db.execute(select(User).where(User.feishu_open_id == info["open_id"], User.deleted_at.is_(None)))
    ).scalar_one_or_none()
    candidates: list[User] = []
    if bound is None and info["name"]:
        # 认领候选：同名 + 未绑飞书 + 在用（disabled 不参与，防冒领已禁用账号）
        candidates = list(
            (await db.execute(
                select(User).where(
                    User.name == info["name"],
                    User.feishu_open_id.is_(None),
                    User.status == "active",
                    User.deleted_at.is_(None),
                )
            )).scalars().all()
        )

    action = decide_feishu_login(bound, candidates, is_admin_openid)
    if action == "reject_disabled":
        raise HTTPException(403, "账号已被禁用，请联系管理员")
    if action == "reject_unauthorized":
        # 冷启动引导：管理员从后端日志捞自己 open_id 填进 ADMIN_FEISHU_OPEN_IDS
        # （只打日志不进响应体——open_id 不发给未授权访问者）
        print(f"[feishu] 未授权登录被拒绝: open_id={info['open_id']} name={info['name']}")
        raise HTTPException(403, "你的飞书账号尚未授权，请联系管理员创建账号（账号姓名需与飞书姓名一致）")
    if action == "reject_ambiguous":
        print(f"[feishu] 同名歧义被拒绝: open_id={info['open_id']} name={info['name']} 候选={len(candidates)} 个")
        raise HTTPException(403, "存在多个同名账号，无法自动识别，请联系管理员处理")

    if action == "claim":
        user = candidates[0]
        user.feishu_open_id = info["open_id"]
        user.avatar_url = info["avatar_url"] or user.avatar_url
        if is_admin_openid:
            user.role = "admin"
        db.add(AuditLog(
            actor_user_id=user.id,
            action="auth.feishu_claim",
            target_type="user",
            target_id=str(user.id),
            detail=json.dumps({"open_id": info["open_id"], "name": info["name"]}, ensure_ascii=False),
        ))
    elif action == "create_admin":
        user = User(
            feishu_open_id=info["open_id"],
            username=info["open_id"],
            name=info["name"],
            avatar_url=info["avatar_url"],
            role="admin",
        )
        db.add(user)
    else:  # login
        user = bound
        user.name = info["name"] or user.name
        user.avatar_url = info["avatar_url"] or user.avatar_url
        if is_admin_openid:
            user.role = "admin"
    # M7：飞书登录/授权刷新 per-user token（含增量授权场景——consent 新 scope 后重走本流程即落库）。
    # M9b 修复：普通登录不带 scope（飞书只发最小 auth:user.id:read），若无条件覆盖会把升级授权
    # 拿到的内容 token（mail/im/doc/...）清空。只在「本次含内容 scope」或「用户尚无 token」时写，
    # 普通登录保留旧 token（open_id/name/avatar 已在上面更新）。
    if _has_content_scope(info.get("scope", "")) or not user.feishu_user_token_enc:
        store_feishu_tokens(user, info)
    # M9c：登录时同步飞书 user_id（用户ID）→ 智书合同过滤用；普通登录（最小 scope）拿不到就保留旧值
    if info.get("user_id"):
        user.zhishu_user_id = info["user_id"]
    await db.commit()
    return _issue_token(user)


# M14b 密码登录防爆破：内存计数，同一用户名 10 分钟内连错 5 次锁定（进程级，重启清零，够用）
_LOGIN_FAIL_WINDOW_S = 600
_LOGIN_FAIL_LIMIT = 5
_login_fails: dict[str, list[float]] = {}


def _login_locked(username: str) -> bool:
    fails = [t for t in _login_fails.get(username, []) if time.monotonic() - t < _LOGIN_FAIL_WINDOW_S]
    _login_fails[username] = fails
    return len(fails) >= _LOGIN_FAIL_LIMIT


def _record_login_fail(username: str) -> None:
    _login_fails.setdefault(username, []).append(time.monotonic())


@router.post("/password-login", response_model=TokenOut)
async def password_login(body: PasswordLoginIn, db: AsyncSession = Depends(get_db)) -> TokenOut:
    """账号密码登录（M14b）：面向测试账号/非飞书场景。密码由管理员在建号/编辑时设置（org_router）。
    统一错误文案，不区分「用户不存在 / 未设密码 / 密码错误」，防账号枚举。"""
    if not settings.PASSWORD_LOGIN_ENABLED:
        raise HTTPException(403, "账号密码登录已关闭")
    username = body.username.strip()
    if not username or not body.password:
        raise HTTPException(400, "用户名或密码错误")
    if _login_locked(username):
        raise HTTPException(429, "尝试次数过多，请 10 分钟后再试")
    user = (
        await db.execute(select(User).where(User.username == username, User.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        _record_login_fail(username)
        raise HTTPException(400, "用户名或密码错误")
    if user.status != "active":
        raise HTTPException(403, "账号已被禁用，请联系管理员")
    _login_fails.pop(username, None)
    return _issue_token(user)


@router.post("/dev-login", response_model=TokenOut)
async def dev_login(body: DevLoginIn, db: AsyncSession = Depends(get_db)) -> TokenOut:
    """开发模式登录：仅用于本地/内测，生产必须 DEV_LOGIN_ENABLED=false。
    用户名 admin 自动获得管理员角色，其余为 member。"""
    if not settings.DEV_LOGIN_ENABLED:
        raise HTTPException(403, "开发登录已关闭")
    username = body.username.strip()
    if not username:
        raise HTTPException(400, "用户名不能为空")
    user = (
        await db.execute(select(User).where(User.username == username, User.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if user is None:
        user = User(username=username, name=username, role="admin" if username == "admin" else "member")
        db.add(user)
        await db.commit()
    return _issue_token(user)


@router.get("/me")
async def me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    from app.auth.permissions import get_user_modules
    return {**_user_dict(user), "modules": await get_user_modules(db, user)}


@router.get("/users")
async def list_users(_: User = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> list[dict]:
    """admin 用户清单：派发数字人、按用户授权时用。"""
    rows = ((await db.execute(select(User).where(User.deleted_at.is_(None)).order_by(User.id.asc()))).scalars().all())
    return [{"id": u.id, "username": u.username, "name": u.name, "role": u.role, "department_id": u.department_id,
             "zhishu_user_id": u.zhishu_user_id, "feishu_bound": bool(u.feishu_open_id)} for u in rows]
