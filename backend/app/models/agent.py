"""数字人体系（M2.5 二层模型）：模板层（is_template=true，IT 管）→ 员工领用/派发产生实例（owner_user_id=员工，复制 .md 与技能绑定）→ 会话层（运行时组装）。"""
from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from pgvector.sqlalchemy import Vector

from app.models.base import Base, BaseModel, utcnow

# agent 的 .md 文件固定 key
AGENT_FILE_KEYS = ("SOUL", "AGENTS", "MEMORY")  # MEMORY=实例的长期策展记忆（memory_update_curated 维护）


class Agent(BaseModel):
    __tablename__ = "agents"

    name = Column(String(64), nullable=False)
    avatar = Column(String(512), nullable=True)       # 头像 URL 或 emoji
    description = Column(String(512), nullable=False, default="")
    status = Column(String(16), nullable=False, default="active")  # active / archived
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    # 二层模型
    is_template = Column(Boolean, nullable=False, default=False)   # 模板（IT 管，可被领用）
    template_id = Column(Integer, ForeignKey("agents.id"), nullable=True)  # 实例 → 来源模板
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)  # 实例属主；模板为 NULL
    is_master = Column(Boolean, nullable=False, default=False)     # 主 agent：仅 admin 可见可聊，带只读管控工具
    # 模型与运行
    default_model_id = Column(Integer, ForeignKey("models.id"), nullable=True)
    model_allowlist = Column(Text, nullable=True)      # JSON 数组字符串；NULL=不限制（仍受模型授权约束）
    allow_code_exec = Column(Boolean, nullable=False, default=False)  # M3 用
    allow_web_search = Column(Boolean, nullable=False, default=False)  # M3.9 联网搜索/网页读取
    allow_api_request = Column(Boolean, nullable=False, default=False)  # M4d 调用白名单内外部 API
    allow_feishu = Column(Boolean, nullable=False, default=False)  # M7 读取用户飞书视野内容（文档/群聊）
    allow_zhishu = Column(Boolean, nullable=False, default=False)  # M9c 读取用户智书合同（tenant token + zhishu_user_id 过滤）
    allow_image_generate = Column(Boolean, nullable=False, default=False)  # M13a 文生图（火山方舟 Seedream，图片落工作区+对话内嵌）
    allow_wecom_archive = Column(Boolean, nullable=False, default=False)  # M31 预留：Phase 2 企微存档查询工具的权限开关（前端开关随 Phase 2 上）
    max_loop_rounds = Column(Integer, nullable=False, default=16)


class AgentFile(BaseModel):
    """数字人的 .md 文件（当前版本）。历史版本在 agent_file_versions。"""

    __tablename__ = "agent_files"
    __table_args__ = (UniqueConstraint("agent_id", "file_key", name="uq_agent_file"),)

    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    file_key = Column(String(32), nullable=False)      # SOUL / AGENTS / MEMORY
    content = Column(Text, nullable=False, default="")
    version = Column(Integer, nullable=False, default=1)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)


class AgentFileVersion(Base):
    """只增不改的历史版本。"""

    __tablename__ = "agent_file_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_file_id = Column(Integer, ForeignKey("agent_files.id"), nullable=False)
    version = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    saved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class AgentClaimGrant(BaseModel):
    """模板可领用范围（复用模型授权的 evaluate_grants 语义：user > department > all）。"""

    __tablename__ = "agent_claim_grants"

    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)  # 模板 id
    grantee_type = Column(String(16), nullable=False)    # user / department / all
    grantee_id = Column(Integer, nullable=True)          # all 时为 NULL


class Skill(BaseModel):
    """技能库。owner_user_id NULL=平台技能（IT 管，员工只读可 fork）；否则=个人技能。"""

    __tablename__ = "skills"

    name = Column(String(64), unique=True, nullable=False)  # 即 frontmatter name，/skill-name 唤起
    description = Column(String(512), nullable=False, default="")
    category = Column(String(64), nullable=False, default="")  # 分类：财务 / Excel / 写作……
    instructions = Column(Text, nullable=False, default="")  # 冗余=SKILL.md 内容（兼容旧读法，权威内容在 skill_files）
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    # M3.3 流通体系：署名 + 血缘（fork/收录自谁）+ fork 时源快照时间（判断"源有更新"）
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)      # 原作者署名（收录时=原属主）
    source_skill_id = Column(Integer, ForeignKey("skills.id"), nullable=True)  # 血缘：fork/收录的源技能
    source_updated_at = Column(DateTime(timezone=True), nullable=True)         # fork 时源的 updated_at 快照


class SkillFile(BaseModel):
    """技能文件夹：SKILL.md 为主文件，可有 scripts/xx.py 等附件。"""

    __tablename__ = "skill_files"
    __table_args__ = (UniqueConstraint("skill_id", "path", name="uq_skill_file"),)

    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False)
    path = Column(String(256), nullable=False)         # SKILL.md / scripts/x.py / templates/yy.txt
    content = Column(Text, nullable=False, default="")


class AgentSkill(BaseModel):
    """技能 ↔ agent 绑定（模板与实例都走这张表；领用时从模板复制）。"""

    __tablename__ = "agent_skills"
    __table_args__ = (UniqueConstraint("agent_id", "skill_id", name="uq_agent_skill"),)

    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    skill_id = Column(Integer, ForeignKey("skills.id"), nullable=False)


# ---------- 记忆与工作区（按 agent × user 隔离；实例一人一 agent 天然隔离） ----------

class AgentMemoryNote(Base):
    """每日笔记。检索限定 (agent_id, user_id)，embedding 可空（未配向量模型时走关键词）。"""

    __tablename__ = "agent_memory_notes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    note_date = Column(Date, nullable=False)
    content = Column(Text, nullable=False)
    embedding = Column(Vector(1024), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class WorkspaceFile(BaseModel):
    """workspace 按 (user, agent) 隔离；员工拖文件进对话即上传到这里。"""

    __tablename__ = "workspace_files"

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    filename = Column(String(256), nullable=False)
    pinned = Column(Boolean, nullable=False, default=False)  # 在所属文件夹内置顶
    mime = Column(String(128), nullable=False, default="application/octet-stream")
    size = Column(Integer, nullable=False, default=0)
    storage_path = Column(String(512), nullable=False)
    content_text = Column(Text, nullable=True)  # 文本类文件的抽取内容（截断），供引用/检索


class WorkspaceFolderPref(BaseModel):
    """M36：文件夹置顶与手动排序（服务端存，跨设备同步）。

    文件夹不是实体（由 WorkspaceFile.filename 前缀推导），故 prefs 单独成表，
    以 (user, agent, folder_path) 为键；无 pref 的文件夹按默认行为（不置顶、排尾部按字母）。
    """

    __tablename__ = "workspace_folder_prefs"
    __table_args__ = (UniqueConstraint("user_id", "agent_id", "folder_path", name="uq_ws_folder_pref"),)

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    folder_path = Column(String(256), nullable=False)
    pinned = Column(Boolean, nullable=False, default=False)
    sort_order = Column(Integer, nullable=False, default=0)  # 同 parent 兄弟组内升序


class WorkspaceShare(BaseModel):
    """工作区内容的跨用户只读共享（M5/M5.1）：A 把若干路径共享给 B/部门/全员。

    语义：实时共享（接收方永远看到最新内容），只读；接收方可"另存为"复制到自己工作区。
    paths 为 JSON 数组字符串（[] = 整个工作区）；元素可为文件或文件夹路径——
    覆盖判定 = agent_id 相同且（filename == 元素 或 filename 以"元素/"为前缀）。
    """

    __tablename__ = "workspace_shares"

    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
    paths = Column(Text, nullable=False, default="[]")  # JSON 数组；[] = 整个工作区
    grantee_type = Column(String(16), nullable=False, default="user")  # user / department / all
    grantee_id = Column(Integer, nullable=True)  # grantee_type=user/department 时必填


# ---------- 角色-模块权限 ----------

# 平台全部功能模块
MODULES = ("chat", "agents_admin", "skills_admin", "gateway_admin", "stats", "audit", "ops")
MODULE_NAMES = {
    "chat": "对话",
    "agents_admin": "数字人管理",
    "skills_admin": "技能库管理",
    "gateway_admin": "模型网关",
    "stats": "用量报表",
    "audit": "审计日志",
    "ops": "运行中心",
}


class RoleModule(BaseModel):
    """角色可见模块勾选。role 目前内置 admin/member，自定义角色后期扩展。"""

    __tablename__ = "role_modules"
    __table_args__ = (UniqueConstraint("role", "module", name="uq_role_module"),)

    role = Column(String(32), nullable=False)
    module = Column(String(32), nullable=False)
