"""记忆检索与写入。

- 配了 embedding 模型（models.model_type='embedding'）→ pgvector 余弦检索
- 没配 → 关键词 ILIKE 兜底（优雅降级，不阻塞使用）
- 写路径做注入扫描（借鉴 OpenClaw"写路径即安全边界"）
"""
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret
from app.models.agent import AgentFile, AgentFileVersion, AgentMemoryNote
from app.models.base import utcnow
from app.models.gateway import Model, ModelProvider

# 粗略的注入特征（够用即可，不是 WAF）
_INJECTION_PATTERNS = re.compile(
    r"(ignore\s+(all|previous)(\s+\w+)?\s+instructions|忽略(以上|之前|所有)指令|system prompt|你现在是|重新设定)",
    re.IGNORECASE,
)


def scan_memory_write(content: str) -> str | None:
    """返回 None 表示安全；否则返回拒绝原因。"""
    if _INJECTION_PATTERNS.search(content):
        return "内容包含疑似提示注入特征，已拒绝写入"
    if len(content) > 4000:
        return "单条记忆过长（>4000 字符）"
    return None


def scan_instructional_write(content: str, *, max_chars: int) -> str | None:
    """指令类内容（技能 SKILL.md 等）的写路径扫描：注入特征 + 长度上限（上限按场景传入）。"""
    if _INJECTION_PATTERNS.search(content):
        return "内容包含疑似提示注入特征，已拒绝写入"
    if len(content) > max_chars:
        return f"内容过长（>{max_chars} 字符），请精简后重试"
    return None


async def _get_embedding_model(db: AsyncSession) -> tuple[Model, ModelProvider] | None:
    m = (
        await db.execute(
            select(Model).where(Model.model_type == "embedding", Model.enabled.is_(True), Model.deleted_at.is_(None)).limit(1)
        )
    ).scalar_one_or_none()
    if m is None:
        return None
    p = (
        await db.execute(
            select(ModelProvider).where(ModelProvider.id == m.provider_id, ModelProvider.enabled.is_(True), ModelProvider.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    return (m, p) if p else None


async def embed_text(db: AsyncSession, text: str) -> list[float] | None:
    pair = await _get_embedding_model(db)
    if pair is None:
        return None
    model, provider = pair
    from openai import AsyncOpenAI

    client = AsyncOpenAI(base_url=provider.base_url, api_key=decrypt_secret(provider.api_key_encrypted), timeout=30)
    resp = await client.embeddings.create(model=model.model_id, input=text[:8000])
    return resp.data[0].embedding


async def memory_search(db: AsyncSession, *, agent_id: int, user_id: int, query: str, limit: int = 5) -> list[str]:
    """检索限定 (agent, user)——个人记忆互不可见。"""
    vec = await embed_text(db, query)
    if vec is not None:
        rows = (
            (
                await db.execute(
                    select(AgentMemoryNote.content)
                    .where(
                        AgentMemoryNote.agent_id == agent_id,
                        AgentMemoryNote.user_id == user_id,
                        AgentMemoryNote.embedding.is_not(None),
                    )
                    .order_by(AgentMemoryNote.embedding.cosine_distance(vec))
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)
    # 关键词兜底
    pattern = f"%{query[:64]}%"
    rows = (
        (
            await db.execute(
                select(AgentMemoryNote.content)
                .where(
                    AgentMemoryNote.agent_id == agent_id,
                    AgentMemoryNote.user_id == user_id,
                    AgentMemoryNote.content.ilike(pattern),
                )
                .order_by(AgentMemoryNote.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def memory_write(db: AsyncSession, *, agent_id: int, user_id: int, content: str) -> str:
    if err := scan_memory_write(content):
        return f"写入失败：{err}"
    vec = await embed_text(db, content)
    note = AgentMemoryNote(
        agent_id=agent_id,
        user_id=user_id,
        note_date=datetime.now(UTC).date(),
        content=content,
        embedding=vec,
    )
    db.add(note)
    await db.flush()
    return "已记录到今日笔记"


async def memory_update_curated(db: AsyncSession, *, agent_id: int, user_id: int, content: str) -> str:
    """长期策展记忆 = 实例的 MEMORY.md（整体重写，旧版进版本历史）。"""
    if err := scan_memory_write(content):
        return f"更新失败：{err}"
    f = (
        await db.execute(
            select(AgentFile).where(
                AgentFile.agent_id == agent_id, AgentFile.file_key == "MEMORY", AgentFile.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if f is None:
        f = AgentFile(agent_id=agent_id, file_key="MEMORY", content="", version=1, updated_by=None)
        db.add(f)
        await db.flush()
    db.add(AgentFileVersion(agent_file_id=f.id, version=f.version, content=f.content, saved_by=f.updated_by, created_at=utcnow()))
    f.content = content
    f.version += 1
    f.updated_by = None  # 数字人自维护
    await db.flush()
    return "长期记忆（MEMORY.md）已更新"


# ---------- M3.17 自进化一期：经验教训区（增量追加，ACE 式 delta 更新，禁整文件重写） ----------

LESSON_SECTION = "## 经验教训"
MAX_LESSONS = 30
MAX_LESSON_CHARS = 500

# 用户偏好与约定区（memory_append_curated）：用户显式「记住/以后都」类长期指令的固化通道
PREF_SECTION = "## 用户偏好与约定"
MAX_PREFS = 30

_SECTION_RE = re.compile(r"^## ", re.MULTILINE)


def _append_entry(content: str, section: str, entry: str, max_items: int) -> str:
    """把一条条目追加到指定「## 区」末尾（LESSON/PREF 共用的增量追加原语）。

    - 无该区 → 在文末新建；有该区 → 插到下一个「## 」标题前；
    - 条目数达 max_items → ValueError（工具层转成"请先合并精简"的提示）。
    entry 为不含换行的单行条目（"- [date] ..." 已由调用方拼好）。
    """
    entry = entry.strip()
    if not entry.startswith("- "):
        entry = f"- {entry}"
    lines = content.split("\n")
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == section)
    except StopIteration:
        base = content.rstrip("\n")
        sep = "\n\n" if base else ""
        return f"{base}{sep}{section}\n{entry}\n"
    # 区尾 = 下一个二级标题或文末
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    existing = [ln for ln in lines[start + 1 : end] if ln.strip().startswith("- ")]
    if len(existing) >= max_items:
        raise ValueError(f"「{section}」区已满 {max_items} 条")
    # 去掉区尾空行后追加
    tail = lines[start + 1 : end]
    while tail and not tail[-1].strip():
        tail.pop()
    new_lines = lines[: start + 1] + tail + [entry, ""] + lines[end:]
    return "\n".join(new_lines)


def append_lesson(content: str, lesson: str, *, max_lessons: int = MAX_LESSONS) -> str:
    """把一条经验教训追加到 MEMORY 的「## 经验教训」区末尾（见 _append_entry）。"""
    return _append_entry(content, LESSON_SECTION, lesson, max_lessons)


def append_pref(content: str, entry: str, *, max_prefs: int = MAX_PREFS) -> str:
    """把一条长期偏好/约定追加到 MEMORY 的「## 用户偏好与约定」区末尾（见 _append_entry）。"""
    return _append_entry(content, PREF_SECTION, entry, max_prefs)


async def memory_add_lesson(db: AsyncSession, *, agent_id: int, scene: str, mistake: str, correction: str, source: str = "") -> str:
    """往 MEMORY.md 的「经验教训」区增量追加一条（实例级，所有使用者共享）。"""
    date = utcnow().date().isoformat()
    parts = f"{scene.strip()} | 错误：{mistake.strip()} → 正确：{correction.strip()}"
    if source.strip():
        parts += f" | 来源：{source.strip()}"
    lesson = f"- [{date}] {parts}"
    if len(lesson) > MAX_LESSON_CHARS:
        return f"未记录：单条经验过长（>{MAX_LESSON_CHARS} 字符），请压缩到一两句话"
    if err := scan_memory_write(lesson):
        return f"未记录：{err}"
    f = (
        await db.execute(
            select(AgentFile).where(
                AgentFile.agent_id == agent_id, AgentFile.file_key == "MEMORY", AgentFile.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if f is None:
        f = AgentFile(agent_id=agent_id, file_key="MEMORY", content="", version=1, updated_by=None)
        db.add(f)
        await db.flush()
    try:
        new_content = append_lesson(f.content, lesson)
    except ValueError:
        return (
            f"未记录：经验教训区已满 {MAX_LESSONS} 条。请先用 memory_update_curated 整体重写 MEMORY.md，"
            "把同类/过时条目合并精简后，再记录新经验。"
        )
    db.add(AgentFileVersion(agent_file_id=f.id, version=f.version, content=f.content, saved_by=f.updated_by, created_at=utcnow()))
    f.content = new_content
    f.version += 1
    f.updated_by = None  # 数字人自维护
    await db.flush()
    return "已记录到 MEMORY.md 的「经验教训」区"


async def memory_append_curated(db: AsyncSession, *, agent_id: int, content: str) -> str:
    """往 MEMORY.md 的「用户偏好与约定」区增量追加一条（实例级，数字人自维护）。

    触发场景：用户显式说「记住/以后都/一直要」类长期指令——比 memory_write 的今日笔记更持久。
    """
    date = utcnow().date().isoformat()
    body = " ".join(content.strip().split())  # 压成单行
    entry = f"- [{date}] {body}"
    if len(entry) > MAX_LESSON_CHARS:
        return f"未记录：单条过长（>{MAX_LESSON_CHARS} 字符），请压缩到一两句话"
    if err := scan_memory_write(entry):
        return f"未记录：{err}"
    f = (
        await db.execute(
            select(AgentFile).where(
                AgentFile.agent_id == agent_id, AgentFile.file_key == "MEMORY", AgentFile.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if f is None:
        f = AgentFile(agent_id=agent_id, file_key="MEMORY", content="", version=1, updated_by=None)
        db.add(f)
        await db.flush()
    try:
        new_content = append_pref(f.content, entry)
    except ValueError:
        return (
            f"未记录：「用户偏好与约定」区已满 {MAX_PREFS} 条。请先用 memory_update_curated 整体重写 MEMORY.md，"
            "把同类/过时条目合并精简后，再记录新约定。"
        )
    db.add(AgentFileVersion(agent_file_id=f.id, version=f.version, content=f.content, saved_by=f.updated_by, created_at=utcnow()))
    f.content = new_content
    f.version += 1
    f.updated_by = None  # 数字人自维护
    await db.flush()
    return "已固化到 MEMORY.md 的「用户偏好与约定」区"


# ---------- M22c 一期：记忆提炼（笔记 → MEMORY.md 策展区，运行中心手动触发） ----------

CONSOLIDATE_MAX_NOTE_CHARS = 12000  # 喂给提炼模型的笔记总字符上限（超出截断，宁可分批不爆 context）


def section_body(content: str, section: str) -> str:
    """取指定「## 区」的正文（不含标题），无该区返回 ""。"""
    lines = content.split("\n")
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == section)
    except StopIteration:
        return ""
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start + 1 : end]).strip()


def replace_section(content: str, section: str, body: str) -> str:
    """整体替换指定「## 区」的正文；无该区则文末新建。其他区原样保留。"""
    body_lines = [ln for ln in body.split("\n") if ln.strip()]
    lines = content.split("\n")
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == section)
    except StopIteration:
        base = content.rstrip("\n")
        sep = "\n\n" if base else ""
        return f"{base}{sep}{section}\n" + "\n".join(body_lines) + "\n"
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    after = lines[end:]
    while after and not after[0].strip():
        after = after[1:]
    new_lines = lines[: start + 1] + body_lines + ([""] if after else []) + after
    return "\n".join(new_lines).rstrip("\n") + "\n"


def build_consolidate_prompt(existing_body: str, notes: list[tuple[str, str]]) -> list[dict]:
    """提炼提示词（纯函数可测）。notes = [(iso_date, content)]，按时间升序传入。"""
    notes_text = "\n".join(f"- ({d}) {c}" for d, c in notes)[:CONSOLIDATE_MAX_NOTE_CHARS]
    return [
        {
            "role": "system",
            "content": (
                "你在整理一个数字员工的长期记忆。把原始笔记中有长期价值的信息（用户偏好、约定、项目背景、关键结论）"
                "提炼合并进「用户偏好与约定」区。规则：每条一行，格式 \"- [YYYY-MM-DD] 内容\""
                "（日期沿用笔记日期；合并多条同类时取最新日期）；合并重复/同类条目；丢弃一次性、过时、闲聊内容；"
                "不得编造笔记里没有的事实；只输出条目列表本身（每行以 - 开头），不要输出标题、代码围栏、解释或其他内容。"
            ),
        },
        {
            "role": "user",
            "content": f"【现有条目】\n{existing_body or '（空）'}\n\n【原始笔记】\n{notes_text or '（无）'}",
        },
    ]


def parse_consolidate_output(text: str) -> list[str] | None:
    """校验并规整提炼输出：每行必须是 "- " 开头的条目。返回条目列表；格式非法返回 None（调用方不得写入）。"""
    lines = [ln.strip() for ln in text.strip().split("\n") if ln.strip()]
    lines = [ln for ln in lines if not ln.startswith("```")]  # 模型偶发包代码围栏
    items = []
    for ln in lines:
        if not ln.startswith("- "):
            return None  # 混入了解释/标题等，整体弃用
        items.append(ln[:MAX_LESSON_CHARS])
    return items[:MAX_PREFS]


async def consolidate_memory(db: AsyncSession, *, agent, actor, model, provider) -> dict:
    """把属主的笔记提炼合并进 MEMORY.md「用户偏好与约定」区（OpenClaw 2.0 consolidation 同款，手动触发）。

    - 只提炼属主本人的笔记（笔记 per-user、MEMORY.md 实例级——防多用户实例里偏好互相泄漏）
    - 整体替换该区（模型已做去重合并），旧版进版本历史可回滚；笔记表不动（仍供 memory_search）
    - 提炼输出格式非法 → 不写入，返回原因
    """
    from openai import AsyncOpenAI

    from app.gateway.service import ChatResult, record_usage

    notes = (
        (
            await db.execute(
                select(AgentMemoryNote)
                .where(AgentMemoryNote.agent_id == agent.id, AgentMemoryNote.user_id == agent.owner_user_id)
                .order_by(AgentMemoryNote.id.asc())
            )
        )
        .scalars()
        .all()
    )
    if not notes:
        return {"ok": False, "reason": "该数字人（属主）没有可提炼的笔记"}
    f = (
        await db.execute(
            select(AgentFile).where(
                AgentFile.agent_id == agent.id, AgentFile.file_key == "MEMORY", AgentFile.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    cur = f.content if f else ""
    messages = build_consolidate_prompt(section_body(cur, PREF_SECTION), [(n.note_date.isoformat(), n.content) for n in notes])

    client = AsyncOpenAI(base_url=provider.base_url, api_key=decrypt_secret(provider.api_key_encrypted), timeout=120)
    started = datetime.now(UTC)
    resp = await client.chat.completions.create(model=model.model_id, messages=messages)
    usage = resp.usage
    await record_usage(
        db,
        user=actor,
        model=model,
        result=ChatResult(
            content="",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            cached_tokens=0,
            latency_ms=int((datetime.now(UTC) - started).total_seconds() * 1000),
        ),
        agent_id=agent.id,
    )
    items = parse_consolidate_output(resp.choices[0].message.content or "")
    if items is None:
        return {"ok": False, "reason": "提炼输出格式异常（未纯条目列表），未写入"}
    if not items:
        return {"ok": False, "reason": "提炼结果为空（笔记可能都是一次性内容），未写入"}

    if f is None:
        f = AgentFile(agent_id=agent.id, file_key="MEMORY", content="", version=1, updated_by=None)
        db.add(f)
        await db.flush()
    db.add(AgentFileVersion(agent_file_id=f.id, version=f.version, content=f.content, saved_by=f.updated_by, created_at=utcnow()))
    f.content = replace_section(cur, PREF_SECTION, "\n".join(items))
    f.version += 1
    f.updated_by = None  # 系统提炼
    await db.flush()
    return {"ok": True, "notes_in": len(notes), "items_out": len(items), "preview": items[:10]}


async def daily_notes_for_turn(db: AsyncSession, *, agent_id: int, user_id: int, channel: str) -> list[str]:
    """本轮对话注入的今昨笔记。cron 会话不注入（M37：定时任务笔记隔离——会话 1554 定案：
    同 agent+user 的定时任务经笔记互相串味，且笔记正文进不了 messages 表、导出取证不可见）。
    cron 仍可写笔记（供人工对话回忆），只断读。feishu 人工会话保留注入。"""
    if channel == "cron":
        return []
    return await recent_daily_notes(db, agent_id=agent_id, user_id=user_id)


async def recent_daily_notes(db: AsyncSession, *, agent_id: int, user_id: int, days: int = 2) -> list[str]:
    """今/昨两天的笔记（借鉴 OpenClaw /new 时加载今昨两天）。"""
    today = datetime.now(UTC).date()
    rows = (
        (
            await db.execute(
                select(AgentMemoryNote.content)
                .where(
                    AgentMemoryNote.agent_id == agent_id,
                    AgentMemoryNote.user_id == user_id,
                    AgentMemoryNote.note_date >= today - timedelta(days=days - 1),
                )
                .order_by(AgentMemoryNote.id.asc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)
