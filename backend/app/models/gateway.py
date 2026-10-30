from sqlalchemy import Boolean, Column, ForeignKey, Integer, Numeric, String

from app.models.base import BaseModel, TimestampMixin
from app.models.base import Base as DeclBase


class ModelProvider(BaseModel):
    """模型供应商。api_key 仅存 Fernet 密文，绝不回传前端。"""

    __tablename__ = "model_providers"

    name = Column(String(128), nullable=False)
    slug = Column(String(64), unique=True, nullable=False)  # deepseek / qwen / kimi ...
    base_url = Column(String(512), nullable=False)          # OpenAI 兼容端点
    api_key_encrypted = Column(String(1024), nullable=False)
    provider_type = Column(String(32), nullable=False, default="openai-compatible")
    enabled = Column(Boolean, nullable=False, default=True)


class Model(BaseModel):
    __tablename__ = "models"

    provider_id = Column(Integer, ForeignKey("model_providers.id"), nullable=False)
    model_id = Column(String(128), nullable=False)       # 远端模型名，如 deepseek-chat
    display_name = Column(String(128), nullable=False)
    model_type = Column(String(16), nullable=False, default="chat")  # chat / embedding
    price_input_per_1k = Column(Numeric(12, 8), nullable=False, default=0)   # 元/千 token
    price_output_per_1k = Column(Numeric(12, 8), nullable=False, default=0)
    context_window = Column(Integer, nullable=True)
    supports_vision = Column(Boolean, nullable=False, default=False)  # 支持图片输入（多模态）
    enabled = Column(Boolean, nullable=False, default=True)


class ModelGrant(BaseModel):
    """模型授权：user 直授 > department > all。同一 model 可有多条。"""

    __tablename__ = "model_grants"

    model_id = Column(Integer, ForeignKey("models.id"), nullable=False)
    grantee_type = Column(String(16), nullable=False)  # user / department / all
    grantee_id = Column(Integer, nullable=True)        # grantee_type=all 时为 NULL


class ApiHost(BaseModel):
    """api_request 工具的域名白名单条目（M4d）。认证配置只存 Fernet 密文，绝不回传前端。

    auth_type：none=无认证 / headers=固定请求头 / login_session=登录换 Cookie。
    auth_config（加密前的 JSON）：
    - headers:       {"headers": {"Authorization": "Basic ..."}}
    - login_session: {"headers": {...可选门禁头...}, "login_path": "/api/auth/login", "login_body": {"phone_number": "...", "password": "..."}}
    """

    __tablename__ = "api_hosts"

    host = Column(String(255), unique=True, nullable=False)      # 纯域名小写，如 api.example.com
    label = Column(String(128), nullable=False, default="")      # 展示名，如「经营看板 BI」
    auth_type = Column(String(32), nullable=False, default="none")
    auth_config_enc = Column(String(4096), nullable=False, default="")  # Fernet 密文；none 时为空串
    allowed_methods = Column(String(64), nullable=False, default="GET,POST")  # 逗号分隔大写
    min_interval_ms = Column(Integer, nullable=False, default=0)  # 同域名两次调用的最小间隔（目标服务限流礼仪）
    enabled = Column(Boolean, nullable=False, default=True)


class UsageRecord(DeclBase, TimestampMixin):
    """逐次调用记录，用量统计与配额的数据源。只增不改，不做软删。"""

    __tablename__ = "usage_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    agent_id = Column(Integer, nullable=True)  # M2 起用
    conversation_id = Column(Integer, nullable=True)
    provider_id = Column(Integer, ForeignKey("model_providers.id"), nullable=False)
    model_id = Column(Integer, ForeignKey("models.id"), nullable=False)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    cached_tokens = Column(Integer, nullable=False, default=0)
    cost = Column(Numeric(14, 8), nullable=False, default=0)
    latency_ms = Column(Integer, nullable=False, default=0)


class Quota(BaseModel):
    __tablename__ = "quotas"

    scope_type = Column(String(16), nullable=False)  # user / department
    scope_id = Column(Integer, nullable=False)
    period = Column(String(8), nullable=False, default="day")  # day / month
    limit_tokens = Column(Integer, nullable=True)   # NULL = 不限制 token
    limit_cost = Column(Numeric(12, 4), nullable=True)  # NULL = 不限制费用
    enabled = Column(Boolean, nullable=False, default=True)
