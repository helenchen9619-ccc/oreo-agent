from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 基础
    APP_NAME: str = "enterprise-agent"
    DEBUG: bool = False

    # 基础设施（默认仅匹配本地 dev compose 的口令；生产必须经 .env.prod 覆盖）
    DATABASE_URL: str = "postgresql+asyncpg://ea:oreo_dev_password@localhost:5432/enterprise_agent"
    REDIS_URL: str = "redis://localhost:6379/0"

    # 安全
    JWT_SECRET: str = "change-me-in-production"
    JWT_EXPIRE_HOURS: int = 12
    # 用于加密 model_providers.api_key 的 Fernet key（44 字符 base64）
    FERNET_KEY: str = ""

    # 飞书自建应用
    FEISHU_APP_ID: str = ""
    FEISHU_APP_SECRET: str = ""
    # 飞书管理员 open_id 列表（逗号分隔），登录时自动授予 admin
    ADMIN_FEISHU_OPEN_IDS: str = ""

    # 开发模式：未配置飞书时允许 dev-login（生产必须关闭）
    DEV_LOGIN_ENABLED: bool = True

    # M14b 账号密码登录（登录页小切换入口；面向测试账号/非飞书场景，密码由管理员在建号时设置）
    PASSWORD_LOGIN_ENABLED: bool = True

    # 沙箱服务
    SANDBOX_BASE_URL: str = "http://sandbox:8100"

    # 博查搜索 API（M3.9 联网搜索；空=未配置，web_search 工具返回配置提示）
    BOCHA_API_KEY: str = ""

    # 智书合同开放平台（M9c；公司级单凭证，tenant_access_token 模式，无 per-user OAuth）
    ZHISHU_APP_ID: str = ""
    ZHISHU_APP_SECRET: str = ""

    # 火山方舟文生图（M13a Seedream；空=未配置，image_generate 工具返回配置提示）
    VOLC_ARK_API_KEY: str = ""
    VOLC_ARK_IMAGE_MODEL: str = "doubao-seedream-5-0-lite-260128"

    # 火山大模型录音文件识别（M13b 语音输入；空=未配置，/api/chat/asr 返回 503 提示）
    VOLC_ASR_APP_KEY: str = ""
    VOLC_ASR_ACCESS_KEY: str = ""

    # 企业微信会话存档（M31；空=未配置，同步服务 no-op。值只在云上 .env.prod，永不进代码/文档/对话）
    WECOM_CORPID: str = ""
    WECOM_ARCHIVE_SECRET: str = ""
    WECOM_ARCHIVE_KEY_PATH: str = "/app/data/wecom_archive/private_key.pem"
    WECOM_ARCHIVE_PUBKEY_VER: str = "2"  # 后台公钥版本；换公钥前的历史消息永不可解（跳过即可）
    WECOM_ARCHIVE_SO_PATH: str = "/app/data/wecom_archive/libWeWorkFinanceSdk_C.so"
    WECOM_CONTACT_SECRET: str = ""  # 通讯录 secret（userid→姓名映射用）
    WECOM_ARCHIVE_SYNC_ENABLED: bool = True  # 逃生开关：SDK 原生崩溃时置 false 秒恢复服务（2026-09-23 事故教训）

    @property
    def admin_open_ids(self) -> set[str]:
        return {x.strip() for x in self.ADMIN_FEISHU_OPEN_IDS.split(",") if x.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
