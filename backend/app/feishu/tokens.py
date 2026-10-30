"""M7：per-user 飞书 user_access_token 的存储与 lazy 滚动刷新。

- token 三元组（access/refresh + 双侧过期时刻）Fernet 加密存 users 表，只进不出；
- 用工具前调用 get_feishu_user_token：未过期直接解密返回；临期（5 分钟内）用
  refresh_token 换新（飞书滚动续期：每次刷新 refresh_token 也换新、30 天重计）；
- refresh 也过期（>30 天没用）→ 返回 None，工具据此返回重新授权引导。
"""
import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import feishu
from app.core.security import decrypt_secret, encrypt_secret
from app.models.user import User

# 临期阈值：access_token 剩不到 5 分钟就刷新（避免用到一半过期）
_REFRESH_SKEW = timedelta(minutes=5)

# per-user 刷新锁（_host_lock 先例：锁绑定到运行中的 event loop，防 loop 关闭后复用）
_token_locks: dict[tuple[int, int], asyncio.Lock] = {}


def _loop_key() -> int:
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


def _user_lock(user_id: int) -> asyncio.Lock:
    key = (_loop_key(), user_id)
    lock = _token_locks.get(key)
    if lock is None:
        lock = _token_locks[key] = asyncio.Lock()
    return lock


def store_feishu_tokens(user: User, info: dict) -> None:
    """把 OAuth/刷新拿到的 token 三元组加密写到 user 对象上（调用方负责 commit）。
    info 结构见 auth/feishu.py 的 fetch_feishu_user / refresh_user_token 返回值。"""
    now = datetime.now(UTC)
    user.feishu_user_token_enc = encrypt_secret(info["access_token"])
    if info.get("refresh_token"):
        user.feishu_refresh_token_enc = encrypt_secret(info["refresh_token"])
    user.feishu_token_expires_at = now + timedelta(seconds=int(info.get("expires_in") or 7200))
    if info.get("refresh_expires_in"):
        user.feishu_refresh_expires_at = now + timedelta(seconds=int(info["refresh_expires_in"]))
    if info.get("scope"):
        user.feishu_scopes = info["scope"]


def token_state(user: User, now: datetime | None = None) -> str:
    """token 状态纯判断：valid（够用）/ refresh（该刷了）/ reauth（只能重新授权）/ none（从未授权）。"""
    now = now or datetime.now(UTC)
    if not user.feishu_user_token_enc:
        return "none"
    exp = user.feishu_token_expires_at
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    if exp is None or exp > now + _REFRESH_SKEW:
        return "valid"
    rexp = user.feishu_refresh_expires_at
    if rexp is not None and rexp.tzinfo is None:
        rexp = rexp.replace(tzinfo=UTC)
    if user.feishu_refresh_token_enc and (rexp is None or rexp > now + _REFRESH_SKEW):
        return "refresh"
    return "reauth"


async def get_feishu_user_token(db: AsyncSession, user: User) -> str | None:
    """取可用的 user_access_token；临期自动滚动刷新并回写。不可用（需重新授权）返回 None。"""
    state = token_state(user)
    if state == "valid":
        return decrypt_secret(user.feishu_user_token_enc)
    if state in ("none", "reauth"):
        return None
    async with _user_lock(user.id):
        # 持锁复查：可能已被并发请求刷新过
        if token_state(user) == "valid":
            return decrypt_secret(user.feishu_user_token_enc)
        try:
            info = await feishu.refresh_user_token(decrypt_secret(user.feishu_refresh_token_enc))
        except RuntimeError:
            return None
        store_feishu_tokens(user, info)
        await db.commit()
        return decrypt_secret(user.feishu_user_token_enc)
