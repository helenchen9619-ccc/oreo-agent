"""企业微信会话存档（M31）：本地存档明文落库 + 映射表 + 敏感词表。

安全纪律：Secret/私钥永不进代码；会话正文不落审计、不进日志。
"""
from sqlalchemy import BigInteger, Column, Integer, String, Text

from app.models.base import BaseModel


class WecomMessage(BaseModel):
    """存档消息明文。msgid 全局唯一（去重键）；seq 为拉取游标序号。"""

    __tablename__ = "wecom_messages"

    seq = Column(BigInteger, nullable=False, index=True)
    msgid = Column(String(128), nullable=False, unique=True)
    action = Column(String(16), nullable=False, default="send")  # send / recall / switch
    sender_userid = Column(String(64), nullable=False, default="")
    receivers = Column(Text, nullable=False, default="")  # JSON 数组（tolist）
    roomid = Column(String(64), nullable=False, default="")  # 空=私聊
    msgtype = Column(String(32), nullable=False, default="")
    text = Column(Text, nullable=False, default="")  # 文本类存正文；图片/语音/文件存占位（附件拉取二期）
    msgtime = Column(BigInteger, nullable=False, default=0)  # 毫秒时间戳


class WecomKeyword(BaseModel):
    """敏感词表（管理端维护）。word 唯一；软删时改名释放唯一约束（M9 技能导入同款教训）。"""

    __tablename__ = "wecom_keywords"

    word = Column(String(128), nullable=False, unique=True)


class WecomContact(BaseModel):
    """通讯录映射：userid → 姓名（通讯录 secret 定期全量刷新）。"""

    __tablename__ = "wecom_contacts"

    userid = Column(String(64), nullable=False, unique=True)
    name = Column(String(64), nullable=False, default="")


class WecomRoom(BaseModel):
    """内部群映射：roomid → 群名（msgaudit/groupchat/get 按需补查；外部非客户群无 API，roomname 留空兜底）。"""

    __tablename__ = "wecom_rooms"

    roomid = Column(String(64), nullable=False, unique=True)
    roomname = Column(String(128), nullable=False, default="")


class WecomViewer(BaseModel):
    """M31.1 授权查看人：非 admin 也能看存档消息（进不了状态/关键词/授权管理区）。
    user_id 是 int 不用软删改名套路——撤销即软删，再授权走复活（deleted_at=None）。"""

    __tablename__ = "wecom_viewers"

    user_id = Column(Integer, nullable=False, unique=True)


class WecomAlertReceiver(BaseModel):
    """M31.1 告警接收人：敏感词命中的飞书卡片推给名单内用户；空表时回落全体绑定飞书的 admin。"""

    __tablename__ = "wecom_alert_receivers"

    user_id = Column(Integer, nullable=False, unique=True)
