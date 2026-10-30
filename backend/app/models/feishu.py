"""飞书私聊会话名册（M28.4）：发现过的 p2p 会话按用户持久化。

/search/v2/message 索引不回溯授权前的私聊（2026-09-22 林晓实测：重授权前的私聊消息搜不到，
发新消息激活后才能发现），且 /im/v1/chats 列表不含 p2p——发现成果不落库就会随活跃窗口流失。
名册记住「chat_id ↔ 对方姓名」，按人名找私聊先查名册，找到一次终身可查。
"""
from sqlalchemy import Column, Integer, String, UniqueConstraint

from app.models.base import BaseModel


class FeishuP2pChat(BaseModel):
    __tablename__ = "feishu_p2p_chats"
    __table_args__ = (UniqueConstraint("user_id", "chat_id", name="uq_feishu_p2p_chats_user_chat"),)

    user_id = Column(Integer, nullable=False, index=True)  # 属主（OREO 用户，各自授权各自的飞书视野）
    chat_id = Column(String(64), nullable=False)
    peer_name = Column(String(64), nullable=False, default="")
