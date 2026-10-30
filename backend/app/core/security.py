"""安全工具：JWT 签发/校验 + 模型 key 的 Fernet 加解密 + 账号密码哈希（M14b）。

红线：模型 api_key 只允许以密文形式落库，禁止进日志/前端响应/沙箱。
"""
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken
from jose import JWTError, jwt

from app.core.config import get_settings

settings = get_settings()
_ALGORITHM = "HS256"


# ---------- JWT ----------

def create_access_token(user_id: int) -> str:
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(UTC) + timedelta(hours=settings.JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> int | None:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[_ALGORITHM])
        return int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        return None


# ---------- 账号密码哈希（M14b，stdlib PBKDF2，零新依赖） ----------

_PBKDF2_ITERATIONS = 200_000


def hash_password(plain: str) -> str:
    """格式 pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>；每次随机盐，同密码不同哈希。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(plain: str, stored: str | None) -> bool:
    """常量时间比对；stored 为空或格式坏一律 False（不泄露账号是否有密码）。"""
    if not stored:
        return False
    try:
        scheme, iterations, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", plain.encode(), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# ---------- Fernet（模型 key 加密） ----------

def _fernet() -> Fernet:
    if not settings.FERNET_KEY:
        raise RuntimeError("FERNET_KEY 未配置，无法加解密模型密钥。请用 `python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` 生成。")
    return Fernet(settings.FERNET_KEY.encode())


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(cipher: str) -> str:
    try:
        return _fernet().decrypt(cipher.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError("模型密钥解密失败，FERNET_KEY 可能已变更") from e
