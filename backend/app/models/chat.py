from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from app.models.base import BaseModel, TimestampMixin
from app.models.base import Base as DeclBase


class Conversation(BaseModel):
    """会话按 (user_id, agent_id, channel) 隔离——任何人看不到他人会话。"""

    __tablename__ = "conversations"

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    agent_id = Column(Integer, nullable=True)  # M2 起用；M1 恒为 NULL
    channel = Column(String(16), nullable=False, default="web")  # web / feishu
    external_chat_id = Column(String(128), nullable=True)  # 飞书 chat_id
    title = Column(String(256), nullable=False, default="新对话")
    pinned = Column(Boolean, nullable=False, default=False)  # 置顶（列表排最前）
    compact_summary = Column(Text, nullable=True)          # compaction 摘要（替代被压缩的旧消息）
    last_context_tokens = Column(Integer, nullable=False, default=0)  # 最近一次组装的上下文估算 token 数
    forked_from_id = Column(Integer, ForeignKey("conversations.id"), nullable=True)  # M6a：分叉来源会话


class Message(DeclBase, TimestampMixin):
    """消息即 transcript 落库（等价 OpenClaw 的 JSONL transcript）。只增不改。"""

    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    role = Column(String(16), nullable=False)  # system / user / assistant / tool
    content = Column(Text, nullable=False, default="")
    tool_calls = Column(Text, nullable=True)       # assistant 发起工具调用的 JSON
    reasoning_content = Column(Text, nullable=True)  # M24.0：思考模型的推理过程（Kimi thinking 模式要求多轮回传，否则 400）
    tool_call_id = Column(String(64), nullable=True)  # role=tool 时对应的调用 id
    name = Column(String(64), nullable=True)       # role=tool 时的工具名
    compacted = Column(Boolean, nullable=False, default=False)  # 已被压缩进 compact_summary
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    trace_id = Column(String(64), nullable=True)
    attachments = Column(Text, nullable=True)  # 临时附件 JSON [{name,path,mime}]（图片等，落盘 data/tmp_uploads/）


class TurnRecord(BaseModel):
    """AGENT-OPS：每轮对话一条（只增不改）。落点 = chat/router.py `_run_turn_background`。

    健康度/耗时/工具统计的事实来源；模型级 tokens/费用仍查 usage_records（互补不重复）。
    """

    __tablename__ = "turn_records"

    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    agent_id = Column(Integer, nullable=False)
    user_id = Column(Integer, nullable=False)
    model_id = Column(Integer, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False)
    duration_ms = Column(Integer, nullable=False, default=0)
    rounds = Column(Integer, nullable=False, default=0)       # LLM 调用轮数（含无工具的首轮）
    tool_calls = Column(Integer, nullable=False, default=0)   # 工具调用次数
    tool_errors = Column(Integer, nullable=False, default=0)  # 工具执行异常次数
    status = Column(String(16), nullable=False, default="ok")  # ok / error / cancelled
    error_msg = Column(String(500), nullable=True)
