"""定时任务（cron job）：到点自动以任务属主身份对指定数字人发起一轮完整对话，结果落新会话。"""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from app.models.base import BaseModel


class ScheduledTask(BaseModel):
    __tablename__ = "scheduled_tasks"

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    prompt = Column(Text, nullable=False)
    cron_expr = Column(String(64), nullable=False)  # 5 段标准 cron（分 时 日 月 周）
    timezone = Column(String(64), nullable=False, default="Asia/Shanghai")
    model_id = Column(Integer, nullable=True)  # NULL → 运行时用 agent.default_model_id
    enabled = Column(Boolean, nullable=False, default=True)
    next_run_at = Column(DateTime(timezone=True), nullable=True)  # 禁用时悬空；启用必算
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    last_status = Column(String(16), nullable=True)  # NULL=从未跑 / running / ok / error
    last_error = Column(Text, nullable=True)  # 截断 500 字
    last_conversation_id = Column(Integer, nullable=True)
    run_count = Column(Integer, nullable=False, default=0)
    # M4b：跑完推飞书群自定义机器人（NULL=不推）；secret 用于签名校验；推送失败只记 last_push_error 不影响运行状态
    feishu_webhook = Column(String(512), nullable=True)
    feishu_secret = Column(String(128), nullable=True)
    last_push_error = Column(Text, nullable=True)
    # M8：跑完由应用机器人私聊推给任务属主本人（个人摘要含私聊视野，不能推群；与 webhook 二选一）
    push_to_me = Column(Boolean, nullable=False, default=False)
