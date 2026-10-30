"""系统级配置（M15 维护模式）：通用键值表，当前只用 key='maintenance'（value '1'/'0'）。"""
from sqlalchemy import Column, String

from app.models.base import BaseModel


class SystemSetting(BaseModel):
    __tablename__ = "system_settings"

    key = Column(String(64), unique=True, nullable=False)
    value = Column(String(255), nullable=False, default="")
