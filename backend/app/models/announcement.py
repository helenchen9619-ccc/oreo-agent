"""平台更新公告（M12）：管理员发布，全员只读。pub_date 手填展示日期（可补发历史公告），不按创建时间。"""
from sqlalchemy import Column, Integer, String, Text

from app.models.base import BaseModel


class Announcement(BaseModel):
    __tablename__ = "announcements"

    title = Column(String(200), nullable=False)
    pub_date = Column(String(10), nullable=False)  # 展示用日期 YYYY-MM-DD
    content = Column(Text, nullable=False)  # markdown 正文
    created_by = Column(Integer, nullable=True)  # 发布管理员 user id（仅记录，不外显）
