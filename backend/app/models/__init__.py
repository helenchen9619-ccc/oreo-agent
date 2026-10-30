from app.models.agent import (  # noqa: F401
    Agent,
    AgentClaimGrant,
    AgentFile,
    AgentFileVersion,
    AgentMemoryNote,
    AgentSkill,
    RoleModule,
    Skill,
    SkillFile,
    WorkspaceFile,
    WorkspaceShare,
)
from app.models.announcement import Announcement  # noqa: F401
from app.models.base import Base  # noqa: F401  (Alembic 需要)
from app.models.chat import Conversation, Message  # noqa: F401
from app.models.feishu import FeishuP2pChat  # noqa: F401
from app.models.gateway import ApiHost, Model, ModelGrant, ModelProvider, Quota, UsageRecord  # noqa: F401
from app.models.scheduler import ScheduledTask  # noqa: F401
from app.models.user import AuditLog, Department, User  # noqa: F401
from app.models.wecom import (  # noqa: F401
    WecomAlertReceiver,
    WecomContact,
    WecomKeyword,
    WecomMessage,
    WecomRoom,
    WecomViewer,
)
