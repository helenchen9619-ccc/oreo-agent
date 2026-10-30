from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.models.base import BaseModel


class Department(BaseModel):
    __tablename__ = "departments"

    feishu_dept_id = Column(String(64), unique=True, nullable=True)
    name = Column(String(128), nullable=False)
    parent_id = Column(Integer, ForeignKey("departments.id"), nullable=True)


class User(BaseModel):
    __tablename__ = "users"

    feishu_open_id = Column(String(64), unique=True, nullable=True)
    # M7 飞书内容阅读：per-user user_access_token（Fernet 加密，只进不出）+ 滚动刷新
    feishu_user_token_enc = Column(Text, nullable=True)
    feishu_refresh_token_enc = Column(Text, nullable=True)
    feishu_token_expires_at = Column(DateTime(timezone=True), nullable=True)
    feishu_refresh_expires_at = Column(DateTime(timezone=True), nullable=True)
    feishu_scopes = Column(Text, nullable=True)  # M9b.2：scope 列表随 M7→M9 一路加，512 撑破（StringDataRightTruncationError）→ 改 Text
    # M9c 智书合同：飞书「用户ID」（user_id 字段，如 944f4998），登录自动抓取；
    # 非飞书用户才在管理端手填兜底。工具按 user_id+permission=1 过滤本人可见合同。
    zhishu_user_id = Column(String(64), nullable=True)
    username = Column(String(64), unique=True, nullable=False)
    # M14b 账号密码登录：PBKDF2 哈希（security.hash_password）；NULL=未设密码（纯飞书账号不能用密码登录）
    password_hash = Column(String(255), nullable=True)
    name = Column(String(128), nullable=False, default="")
    avatar_url = Column(String(512), nullable=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    role = Column(String(16), nullable=False, default="member")  # admin / member
    status = Column(String(16), nullable=False, default="active")  # active / disabled
    # M27 公告未读 NEW 标签：已读游标（已读到的最大公告 id），存服务端跨设备同步；NULL=从未读过
    last_read_announcement_id = Column(Integer, nullable=True)


class AuditLog(BaseModel):
    __tablename__ = "audit_logs"

    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    action = Column(String(64), nullable=False)  # e.g. provider.create / grant.update / quota.update
    target_type = Column(String(64), nullable=False)
    target_id = Column(String(64), nullable=True)
    detail = Column(String(4096), nullable=True)  # JSON 字符串，避免不同 DB 的 JSONB 依赖
