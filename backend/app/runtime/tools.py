"""agent loop 可用工具：schema 定义 + 分发执行。

- 基础工具：memory_*、read_skill（文件夹形态）、skill_create_draft（沉淀个人技能，M16）、workspace_*
- 代码执行工具（CODE_TOOL_SCHEMAS）：仅当 agent.allow_code_exec 时由 engine 挂载，run_code 走 M3 沙箱。
- 联网工具（WEB_TOOL_SCHEMAS）：仅当 agent.allow_web_search 时挂载，host 侧执行（M3.9）。
- 定时任务工具（SCHED_TOOL_SCHEMAS）：无条件挂载，属主=当前用户 × 当前数字人（M3.10）。
- 主 agent 只读管理工具（MASTER_TOOL_SCHEMAS）：仅当 agent.is_master 且用户是 admin 时由 engine 挂载。
"""
import base64
import asyncio
import ipaddress
import json
import mimetypes
import re
import shutil
import socket
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import decrypt_secret
from app.auth.feishu import app_access_token
from app.feishu.client import (
    FeishuApiError, approval_cc_instances, approval_instance_detail, approval_my_instances,
    approval_pending_tasks,
    bitable_add_field, bitable_add_records, bitable_create_app, bitable_create_table,
    bitable_delete_field,
    bitable_delete_records, bitable_fields, bitable_records,
    bitable_tables, bitable_update_field, bitable_update_record,
    chat_members, chat_messages, contact_user_names, download_im_resource,
    docx_append_blocks, docx_block_children, docx_blocks, docx_create, docx_create_descendants,
    docx_delete_children,
    drive_download_file, drive_download_media, drive_list_folder, drive_root_folder, drive_upload_file,
    get_chat_info, get_message, list_chats,
    mail_folders, mail_message_ids, mail_message_meta, mail_search, message_reactions,
    read_doc_raw, search_docs, search_messages, search_messages_page, sheet_create, sheet_float_images,
    sheet_meta, sheet_values,
    sheet_values_append, sheet_values_update, wiki_resolve,
)
from app.feishu.tokens import get_feishu_user_token
from app.zhishu.client import ZhishuApiError, contract_detail, contract_search, zhishu_token
from app.models.agent import Agent, AgentFile, AgentSkill, Skill, SkillFile, WorkspaceFile
from app.models.base import utcnow
from app.models.feishu import FeishuP2pChat
from app.models.gateway import ApiHost, Model, UsageRecord
from app.models.scheduler import ScheduledTask
from app.models.user import AuditLog, User
from app.runtime import memory as mem
from app.runtime import sandbox_client
from app.runtime.prompt import get_available_skills
from app.scheduler.cronutil import compute_next, is_valid_cron, is_valid_tz
from app.workspace.paths import INVALID_HINT as _INVALID_PATH_HINT, sanitize_name as _sanitize_name, valid_rel as _valid_ws_path

# M21：路径校验唯一实现迁到 app.workspace.paths（白名单 [\w.\-]+ → 按段黑名单，
# 放行空格/括号等真实世界文件名——ERP 导出的「物品导出清单-总部 (16).xlsx」曾被执行侧拦死）
_WS_WRITE_MAX = 200 * 1024  # agent 写文件单文件上限 200KB
_RUN_CODE_FILE_MAX = 100 * 1024 * 1024  # run_code 带入沙箱的单文件上限 100MB（与工作区上传上限一致）
_RUN_CODE_MAX_FILES = 8


def _workspace_root(user_id: int, agent_id: int) -> Path:
    return Path(getattr(get_settings(), "DATA_DIR", "data")) / "workspaces" / str(user_id) / str(agent_id)


_TRACE_KEEP = 20  # inline code 留痕每个 (用户, 数字人) 保留的份数（平台侧调试留痕，不进工作区）


def _trace_root(user_id: int, agent_id: int) -> Path:
    """inline code 的执行留痕目录：平台侧 DATA_DIR/traces/ 下，不登记 WorkspaceFile、
    不出现在工作区（中间过程代码刷屏观感差，还污染系统提示的文件清单）。"""
    return Path(getattr(get_settings(), "DATA_DIR", "data")) / "traces" / str(user_id) / str(agent_id)


_CREATED_FILES_CAP = 10  # 单轮消息最多挂的产出附件数（防爆气泡；工作区面板里全量都在）


def _note_created(ctx: "ToolContext", abs_path: str, ws_path: str, mime: str) -> None:
    """本轮工具落盘的产出登记到 ctx.created_files（引擎收尾时挂到 assistant 消息附件）。
    按 ws_path 去重（一轮多次覆写只留一条）、超上限丢弃。"""
    if any(f.get("ws_path") == ws_path for f in ctx.created_files):
        return
    if len(ctx.created_files) >= _CREATED_FILES_CAP:
        return
    ctx.created_files.append({"name": ws_path.split("/")[-1], "path": abs_path, "ws_path": ws_path, "mime": mime})


async def _workspace_write(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """agent 写工作区文件：路径严格限制在该 (user, agent) 工作区内，自动建子文件夹。"""
    path = (args.get("path") or "").strip().lstrip("/")
    content = args.get("content") or ""
    if not _valid_ws_path(path):
        return _INVALID_PATH_HINT
    if len(content.encode("utf-8")) > _WS_WRITE_MAX:
        return "内容超过 200KB 上限，请拆分写入"
    root = _workspace_root(ctx.user.id, ctx.agent.id).resolve()
    target = (root / path).resolve()
    if root not in target.parents and target != root:
        return "路径越界，已拒绝"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    # upsert 记录（content_text 截断 50000 与上传逻辑一致）
    row = (
        await ctx.db.execute(
            select(WorkspaceFile).where(
                WorkspaceFile.user_id == ctx.user.id,
                WorkspaceFile.agent_id == ctx.agent.id,
                WorkspaceFile.filename == path,
            )
        )
    ).scalar_one_or_none()
    size = len(content.encode("utf-8"))
    text = content.replace("\x00", "")[:50000]  # PostgreSQL text 禁止 NUL；JSON \u0000 会变真实控制字符
    if row is None:
        row = WorkspaceFile(
            user_id=ctx.user.id, agent_id=ctx.agent.id, filename=path,
            mime="text/plain", size=size, storage_path=str(target), content_text=text,
        )
        ctx.db.add(row)
    else:
        row.size = size
        row.storage_path = str(target)
        row.content_text = text
        row.deleted_at = None
    await ctx.db.commit()
    # M20：产物挂到本轮消息附件，前端气泡下直接可点预览
    _note_created(ctx, str(target), path, mimetypes.guess_type(path)[0] or "text/plain")
    return f"已写入 {path}（{size} 字节）"


async def _workspace_delete(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """agent 删工作区文件/文件夹：软删记录 + 删磁盘；一条查询同时覆盖文件与文件夹。"""
    path = (args.get("path") or "").strip().lstrip("/")
    if not _valid_ws_path(path):
        return _INVALID_PATH_HINT
    rows = (
        (
            await ctx.db.execute(
                select(WorkspaceFile).where(
                    WorkspaceFile.user_id == ctx.user.id,
                    WorkspaceFile.agent_id == ctx.agent.id,
                    WorkspaceFile.deleted_at.is_(None),
                    (WorkspaceFile.filename == path) | (WorkspaceFile.filename.startswith(path + "/")),
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return f"未找到 {path}（文件或文件夹不存在）"
    for f in rows:
        f.deleted_at = utcnow()
        try:
            Path(f.storage_path).unlink(missing_ok=True)
        except OSError:
            pass
    root = _workspace_root(ctx.user.id, ctx.agent.id).resolve()
    target = (root / path).resolve()
    if root in target.parents:
        shutil.rmtree(target, ignore_errors=True)
    await ctx.db.commit()
    return f"已删除 {path}（共 {len(rows)} 个文件）"


# ---------- 代码执行工具（M3 沙箱；仅 agent.allow_code_exec 时挂载） ----------
CODE_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_code",
            "description": (
                "在隔离沙箱中执行 Python 3.12 代码（已预装 pandas / numpy / openpyxl / matplotlib / pyarrow / rapidocr / pypdfium2，无外网）。"
                "适合：处理 workspace 里的大型 Excel/CSV/Parquet（pyarrow 可直接 pd.read_parquet，严禁手写二进制解码）、复杂计算、生成图表、"
                "解析 PDF（用 pypdfium2，严禁手写 PDF 解析器：import pypdfium2 as pdfium; pdf = pdfium.PdfDocument('./x.pdf')，"
                "每页 pdf[i].get_textpage().get_text_range() 即全文；扫描件文字层为空时用 pdf[i].render().to_pil() 转图再 rapidocr）、"
                "提取图片中的文字（用 rapidocr：from rapidocr import RapidOCR; result = RapidOCR()('./图片.png')，result.txts 即全部文字行）。"
                "注意：你自己的视觉识图能力只对用户直接发到对话里的图片附件生效，沙箱代码无法调用模型视觉；"
                "workspace 里的图片如果只是要其中的文字就用 rapidocr，如果需要理解图片语义（图表含义、画面内容），引导用户把图片直接发到对话里。"
                "用 files 参数把 workspace 文件带入沙箱（在代码里以 ./相对路径 访问）；"
                "两种用法：一次性的小探测直接传 code；复杂、要反复跑或要交付给用户的逻辑，"
                "先用 workspace_write 把代码写成当前任务项目文件夹下的 .py（与用户数据文件同目录，"
                "如 财务数据核对/reconcile.py；文件夹可以中文，但 .py 文件名必须用英文小写+下划线；"
                "不要新建与工作区已有文件夹并列的顶层 scripts/ 文件夹），"
                "再用 script 参数执行（可复跑、排错时改脚本重跑，不要重写全量代码）；"
                "inline code 的留痕在平台调试目录（不在工作区），排错时用你历史消息里的代码改完重跑；"
                "处理大 Excel（几十 MB）时用 openpyxl read_only=True 逐行流式读，或 pd.read_excel 指定 usecols 只取需要的列，"
                "避免一次性加载多份副本（沙箱内存 2G，整表多读几遍会 OOM）；"
                "同一份大文件需要多步处理时，第一步先把它转成 parquet 写到 ./output/（会自动回写 workspace），"
                "后续步骤直接带 parquet 进沙箱——pyarrow 读 parquet 比反复解析 xlsx 快几十倍，别每一步都重新解析同一个 Excel；"
                "产物落位规则：/work 下的新文件和被改写的输入文件都会按相对路径自动回传 workspace——"
                "已有项目文件夹时就直接写进该文件夹（如 ./财务数据核对/结果.xlsx → 落位 工作区 财务数据核对/ 下，不要再套一层产物目录），"
                "等价写法是 ./output/ 下同相对路径；不需要回传的临时文件一律写 /tmp（/work 下的新文件都会被收进 workspace）；"
                "百万行级操作避免逐行 Python 循环（iterrows/apply/bisect），改用 pandas 向量化原生方法"
                "（merge/drop_duplicates/searchsorted 等），通常能从几分钟降到秒级；"
                "要交付给用户的产物（结果表格、图表 PNG、转换好的 docx 等）按上面的落位规则写进对应项目文件夹，执行完会自动存回 workspace 并在结果中告知路径；"
                "想覆盖更新 workspace 里某个已有文件（例如 ETL 更新基线表、用户要求把结果「做进」某张已有表），"
                "在 ./output/ 下用相同的相对路径写一遍即可，会直接覆盖原文件；"
                "matplotlib 已配中文字体，中文标题可直接使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "要执行的 Python 代码（≤64KB）。与 script 二选一；都给了以 script 为准"},
                    "script": {
                        "type": "string",
                        "description": "可选：workspace 里的 .py 脚本相对路径（如 财务数据核对/reconcile.py）。给了 script 就执行该脚本（自动带入沙箱），code 可留空",
                    },
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选：要带人沙箱的 workspace 文件相对路径列表（≤8 个，每个 ≤100MB），代码里以 ./文件名 访问",
                    },
                    "timeout_s": {
                        "type": "integer",
                        "description": "可选：执行时间预算（秒），默认 600、最大 600。一般不用动；轻量探测可调小（如 30）让错误代码快速失败",
                    },
                },
            },
        },
    },
]
CODE_TOOL_NAMES = {s["function"]["name"] for s in CODE_TOOL_SCHEMAS}


async def _run_code(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """run_code：工作区文件 → 沙箱执行 → 产物回写工作区 + 审计。沙箱不可达/超时都降级为友好提示。

    两种模式：inline code（≤64KB，执行后自动留痕到 scripts/run_时间戳.py）；
    script 模式（执行工作区已有 .py，脚本带入沙箱，runpy 薄壳执行，不重复留痕）。
    """
    code = args.get("code") or ""
    script = (args.get("script") or "").strip().lstrip("/")
    if script:
        # script 模式：路径校验 + 必须 .py + 文件存在；实际执行 code 换成 runpy 薄壳
        if not _valid_ws_path(script) or not script.endswith(".py"):
            return f"script 路径不合法：{script}（需要 workspace 内的 .py 相对路径）"
        root0 = _workspace_root(ctx.user.id, ctx.agent.id).resolve()
        target0 = (root0 / script).resolve()
        if root0 not in target0.parents or not target0.is_file():
            return f"workspace 中不存在脚本：{script}"
        code = f"import runpy\nrunpy.run_path('./{script}', run_name='__main__')\n"
    if not code.strip():
        return "代码为空：请传 code（inline 代码）或 script（workspace 里的 .py 路径）"
    if len(code.encode("utf-8")) > 64 * 1024:
        return "代码超过 64KB 上限"

    # 执行时间预算：默认 600s（M3.5，大文件 ETL 常态）；模型可为轻探测调小快速失败
    timeout_s = args.get("timeout_s")
    if timeout_s is not None:
        if not isinstance(timeout_s, int) or isinstance(timeout_s, bool) or not (1 <= timeout_s <= 600):
            return "timeout_s 必须是 1-600 的整数（秒）"

    # 带入文件：路径校验 + 从该 (user, agent) 工作区磁盘读取
    root = _workspace_root(ctx.user.id, ctx.agent.id).resolve()
    payload_files: list[dict] = []
    requested = args.get("files") or []
    if not isinstance(requested, list):
        return "files 参数必须是路径数组"
    if len(requested) > _RUN_CODE_MAX_FILES:
        return f"带入文件超过 {_RUN_CODE_MAX_FILES} 个上限"
    for rel in requested:
        rel = str(rel).strip().lstrip("/")
        if not _valid_ws_path(rel):
            return f"带入文件路径不合法：{rel}（{_INVALID_PATH_HINT}）"
        target = (root / rel).resolve()
        if root not in target.parents or not target.is_file():
            return f"workspace 中不存在文件：{rel}"
        data = target.read_bytes()
        if len(data) > _RUN_CODE_FILE_MAX:
            return f"文件 {rel} 超过 100MB，无法带入沙箱"
        payload_files.append({"path": rel, "content_b64": base64.b64encode(data).decode()})

    # script 模式：把脚本本身也带入沙箱（薄壳 code 里按 ./相对路径 runpy 执行）
    if script and script not in [str(r).strip().lstrip("/") for r in requested]:
        data = (root / script).resolve().read_bytes()
        if len(data) > _RUN_CODE_FILE_MAX:
            return f"脚本 {script} 超过 100MB，无法带入沙箱"
        payload_files.append({"path": script, "content_b64": base64.b64encode(data).decode()})

    result = await sandbox_client.exec_code(code, payload_files, timeout_s)
    if result is None:
        return "沙箱服务暂不可用，请稍后重试或联系管理员检查沙箱服务状态"

    # inline code 留痕：写平台侧 traces/ 调试目录（不登记 WorkspaceFile、不出现在工作区），
    # 无论成败都留（出错的代码正是排查最需要的）；路径记在审计 detail 里。保留最近 N 份防无限增长。
    # script 模式脚本本身已在工作区，不重复留。秒级+微秒时间戳避免同秒两次执行撞名。
    trace_path: str | None = None
    if not script:
        tdir = _trace_root(ctx.user.id, ctx.agent.id)
        tdir.mkdir(parents=True, exist_ok=True)
        trace_file = tdir / f"run_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S_%f')}.py"
        trace_file.write_text(code, encoding="utf-8")
        trace_path = str(trace_file)
        for old in sorted(tdir.glob("run_*.py"))[:-_TRACE_KEEP]:
            old.unlink(missing_ok=True)

    # 产物回写工作区：按沙箱返回的相对路径原样落位（用户在哪个文件夹给的文件，产物就回哪个文件夹）；
    # 与已有文件同相对路径 → 直接覆盖更新原文件（ETL 更新基线、填充既有模板表等场景）。
    saved: list[str] = []
    overwritten: list[str] = []
    for o in result.get("outputs") or []:
        rel_path = str(o.get("path") or "").lstrip("/")
        if not rel_path or ".." in rel_path.split("/"):
            continue
        try:
            data = base64.b64decode(o.get("content_b64") or "")
        except Exception:
            continue
        existing = (
            await ctx.db.execute(
                select(WorkspaceFile).where(
                    WorkspaceFile.user_id == ctx.user.id,
                    WorkspaceFile.agent_id == ctx.agent.id,
                    WorkspaceFile.filename == rel_path,
                    WorkspaceFile.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            ws_path, row = rel_path, existing
        else:
            ws_path = rel_path
            row = (
                await ctx.db.execute(
                    select(WorkspaceFile).where(
                        WorkspaceFile.user_id == ctx.user.id,
                        WorkspaceFile.agent_id == ctx.agent.id,
                        WorkspaceFile.filename == ws_path,
                    )
                )
            ).scalar_one_or_none()
        target = (root / ws_path).resolve()
        if root not in target.parents:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        try:
            # NUL 是合法 UTF-8 但 PostgreSQL text 禁止 0x00：空 zip 等「能解码的二进制」按二进制登记
            text: str | None = None if b"\x00" in data else data.decode("utf-8")[:50000]
        except UnicodeDecodeError:
            text = None  # 二进制产物（PNG/xlsx 等）
        if row is None:
            row = WorkspaceFile(
                user_id=ctx.user.id, agent_id=ctx.agent.id, filename=ws_path,
                # 按扩展名猜 mime：图表 PNG 等产物此前写死 octet-stream 导致前端无法预览
                mime=mimetypes.guess_type(rel_path)[0] or "application/octet-stream",
                size=len(data), storage_path=str(target), content_text=text,
            )
            ctx.db.add(row)
        else:
            row.size = len(data)
            row.storage_path = str(target)
            row.content_text = text
            row.mime = mimetypes.guess_type(rel_path)[0] or row.mime
            row.deleted_at = None
        (overwritten if existing is not None else saved).append(ws_path)
        # M20：产物挂到本轮消息附件（覆写场景 ws_path 去重只留一条）
        _note_created(ctx, str(target), ws_path, row.mime or "application/octet-stream")

    # 审计留痕（M3 验收：所有执行留 audit 记录）
    ctx.db.add(
        AuditLog(
            actor_user_id=ctx.user.id,
            action="sandbox.exec",
            target_type="agent",
            target_id=str(ctx.agent.id),
            detail=json.dumps(
                {
                    "code_chars": len(code),
                    "script": script or None,
                    "trace": trace_path,
                    "files_in": [f["path"] for f in payload_files],
                    "exit_code": result.get("exit_code"),
                    "duration_ms": result.get("duration_ms"),
                    "outputs": saved + overwritten,
                    "overwritten": overwritten,
                    "error": result.get("error"),
                },
                ensure_ascii=False,
            )[:4000],
        )
    )
    await ctx.db.commit()

    # 组装返回给模型的结果
    if result.get("error"):
        head = f"执行失败：{result['error']}"
    else:
        head = f"执行完成（exit_code={result.get('exit_code')}，耗时 {result.get('duration_ms')}ms）"
    parts = [head]
    stdout = (result.get("stdout") or "").strip()
    stderr = (result.get("stderr") or "").strip()
    if stdout:
        parts.append(f"stdout:\n{stdout[:4096]}")
    if stderr:
        parts.append(f"stderr:\n{stderr[:2048]}")
    if overwritten:
        parts.append("已覆盖更新 workspace 原文件：\n" + "\n".join(f"- {p}" for p in overwritten))
    if saved:
        parts.append("产物已存回 workspace：\n" + "\n".join(f"- {p}" for p in saved))
    if script:
        parts.append(f"本次执行的是工作区脚本：{script}（改完可直接用 script 参数重跑）")
    return "\n".join(parts)


# ---------- 联网工具（M3.9；仅 agent.allow_web_search 时挂载；host 侧执行，不进沙箱） ----------

WEB_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索实时信息（新闻、行情、行业动态、政策法规等）。需要最新/不确定的信息时主动使用，不要凭记忆硬答。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，尽量具体"},
                    "freshness": {
                        "type": "string",
                        "enum": ["oneDay", "oneWeek", "oneMonth", "oneYear", "noLimit"],
                        "description": "时间范围，默认 noLimit（不限）",
                    },
                    "count": {"type": "integer", "description": "返回条数 1-10，默认 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "抓取并提取指定网页的正文内容。搜索结果摘要不够用时，用它读取原文。",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "要抓取的 http/https 网页地址"}},
                "required": ["url"],
            },
        },
    },
]
WEB_TOOL_NAMES = {s["function"]["name"] for s in WEB_TOOL_SCHEMAS}

_BOCHA_URL = "https://api.bochaai.com/v1/web-search"
_FRESHNESS_VALUES = {"oneDay", "oneWeek", "oneMonth", "oneYear", "noLimit"}
_SEARCH_OUT_MAX = 4000  # 搜索结果总输出上限（字符）
_SEARCH_SNIPPET_MAX = 300
_FETCH_MAX_BYTES = 2 * 1024 * 1024  # 网页原始响应上限（解析前截断）
_FETCH_TEXT_MAX = 8000  # 提取正文后返回上限
_FETCH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _check_fetch_url(url: str) -> str | None:
    """SSRF 防护（纯函数便于测试）：仅 http/https；解析 host，拒 环回/私网/链路本地/保留/组播地址。
    返回 None=通过，否则为拒绝原因文案。"""
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return "URL 不合法"
    if p.scheme not in ("http", "https") or not p.hostname:
        return "URL 不合法（仅支持 http/https）"
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return f"域名解析失败：{p.hostname}"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return f"目标地址 {ip} 属于内网/保留地址，已按安全策略拒绝访问"
    return None


async def _web_search(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """web_search：调博查 API，结果归一化为编号列表（标题/URL/摘要/来源），整体截断防上下文爆炸。"""
    query = (args.get("query") or "").strip()
    if not query:
        return "query 不能为空"
    api_key = get_settings().BOCHA_API_KEY
    if not api_key:
        return "平台未配置搜索服务（BOCHA_API_KEY 未设置），请联系管理员在服务端配置后重试"
    freshness = args.get("freshness") or "noLimit"
    if freshness not in _FRESHNESS_VALUES:
        freshness = "noLimit"
    count = args.get("count")
    if not isinstance(count, int) or isinstance(count, bool):
        count = 5
    count = max(1, min(10, count))

    started = time.monotonic()
    error: str | None = None
    values: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                _BOCHA_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={"query": query, "freshness": freshness, "summary": True, "count": count},
            )
        if resp.status_code != 200:
            error = f"搜索服务返回错误（HTTP {resp.status_code}）：{resp.text[:200]}"
        else:
            try:
                values = ((resp.json().get("data") or {}).get("webPages") or {}).get("value") or []
            except (json.JSONDecodeError, AttributeError):
                error = "搜索服务响应格式异常"
    except httpx.TimeoutException:
        error = "搜索请求超时（20s），请换个更具体的关键词重试"
    except httpx.HTTPError as e:
        error = f"搜索服务连接失败：{e!s}"

    _audit_tool(ctx, "web.search", {"query": query, "freshness": freshness, "count": count,
                                   "results": len(values), "duration_ms": int((time.monotonic() - started) * 1000),
                                   "error": error})
    if error:
        return error
    if not values:
        return f"未搜到与「{query}」相关的结果，可换关键词重试"

    blocks = []
    for i, v in enumerate(values, 1):
        if not isinstance(v, dict):
            continue
        snippet = (v.get("summary") or v.get("snippet") or "").strip()[:_SEARCH_SNIPPET_MAX]
        meta = "　".join(x for x in ((v.get("siteName") or "").strip(), (v.get("dateLastCrawled") or "")[:10]) if x)
        lines = [f"[{i}] {v.get('name') or '(无标题)'}", str(v.get("url") or "")]
        if snippet:
            lines.append(snippet)
        if meta:
            lines.append(meta)
        blocks.append("\n".join(lines))
    out = "\n\n".join(blocks)
    if len(out) > _SEARCH_OUT_MAX:
        out = out[:_SEARCH_OUT_MAX] + "\n…[结果过多已截断]"
    return out


async def _web_fetch(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """web_fetch：SSRF 校验 → 手动跟跳转（每跳重验）→ 流式读取限 2MB → 抽正文 → 截 8000 字。"""
    url = (args.get("url") or "").strip()
    err = _check_fetch_url(url)
    if err:
        return err

    started = time.monotonic()
    error: str | None = None
    raw = b""
    current = url
    try:
        async with httpx.AsyncClient(
            timeout=20, follow_redirects=False,
            headers={"User-Agent": _FETCH_UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        ) as client:
            for _ in range(5):
                async with client.stream("GET", current) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location") or ""
                        if not location:
                            error = f"抓取失败（HTTP {resp.status_code}，无跳转目标）"
                            break
                        current = urljoin(current, location)
                        err = _check_fetch_url(current)
                        if err:
                            error = f"跳转目标被拒绝：{err}"
                            break
                        continue
                    if resp.status_code != 200:
                        error = f"抓取失败（HTTP {resp.status_code}）"
                        break
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in resp.aiter_bytes(65536):
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= _FETCH_MAX_BYTES:
                            break
                    raw = b"".join(chunks)
                break
            else:
                error = "页面跳转次数过多（>5），已放弃"
    except httpx.TimeoutException:
        error = "网页抓取超时（20s）"
    except httpx.HTTPError as e:
        error = f"网页抓取失败：{e!s}"

    if error:
        _audit_tool(ctx, "web.fetch", {"url": url, "duration_ms": int((time.monotonic() - started) * 1000), "error": error})
        return error

    text = _extract_text(raw)
    _audit_tool(ctx, "web.fetch", {"url": url, "final_url": current, "bytes": len(raw),
                                  "text_chars": len(text), "duration_ms": int((time.monotonic() - started) * 1000)})
    if not text:
        return "未能从该页面提取到正文（可能是纯 JS 渲染页面，暂不支持浏览器渲染）"
    if len(text) > _FETCH_TEXT_MAX:
        return text[:_FETCH_TEXT_MAX] + "\n…[正文过长已截断]"
    return text


def _extract_text(raw: bytes) -> str:
    """HTML → 正文：trafilatura 优先，失败回退 lxml 纯文本。"""
    html = raw.decode("utf-8", errors="replace")
    try:
        import trafilatura

        text = trafilatura.extract(html, include_links=False, include_comments=False, include_images=False)
        if text:
            return text.strip()
    except Exception:  # trafilatura 对畸形 HTML 偶有异常，落回退
        pass
    try:
        from lxml import html as lxml_html

        doc = lxml_html.fromstring(html)
        for bad in doc.xpath("//script|//style|//noscript"):
            if bad.getparent() is not None:
                bad.getparent().remove(bad)
        text = re.sub(r"\n{3,}", "\n\n", doc.text_content()).strip()
        return text
    except Exception:
        return ""


def _audit_tool(ctx: "ToolContext", action: str, detail: dict[str, Any]) -> None:
    """联网工具审计（仿 sandbox.exec；write-only，无读取端点）。不 commit，随会话事务落库。"""
    ctx.db.add(
        AuditLog(
            actor_user_id=ctx.user.id,
            action=action,
            target_type="agent",
            target_id=str(ctx.agent.id),
            detail=json.dumps(detail, ensure_ascii=False)[:4000],
        )
    )


# ---------- 外部 API 工具（M4d；仅 agent.allow_api_request 时挂载；host 侧执行，不进沙箱） ----------
# 设计（学 openclaw/SuperMagic）：沙箱保持无网，外部 API 由 host 侧代发，域名白名单 + 管理员预置认证。
# 密钥/cookie 全程不出现在模型上下文：认证在工具内部完成，响应只回状态码与正文。

API_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "api_request",
            "description": (
                "调用管理员配置过白名单的外部 API（可用域名见系统提示「可调用的外部 API」清单，不在清单里的域名会被拒绝）。"
                "认证由平台自动完成（Basic/Bearer 门禁头、账号密码登录换 cookie 都已预置），你不需要也不应该试图获取任何密钥；"
                "你只需按接口文档拼 method/url/参数/请求体。接口定义读数字人的技能或工作区文档；"
                "分页接口按 page/page_size 逐页拉，不要一次要全量；响应被截断时加筛选条件或缩小日期范围再拉；"
                "批量取数串行进行，不要并发轰炸；失败（401/403/5xx）按返回文案处理，不要无脑重试。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {"type": "string", "description": "HTTP 方法：GET / POST / PUT / PATCH / DELETE（受该域名白名单配置限制）"},
                    "url": {"type": "string", "description": "完整 URL（含查询参数），域名必须在白名单内"},
                    "body": {"description": "可选：JSON 请求体（POST/PUT/PATCH 用），传 JSON 对象"},
                    "headers": {
                        "type": "object",
                        "description": "可选：额外请求头（如 Content-Type）。认证头由平台注入，不要在这里传 Authorization/Cookie",
                    },
                    "timeout_s": {"type": "integer", "description": "可选：超时秒数，默认 30、最大 120"},
                },
                "required": ["method", "url"],
            },
        },
    },
]
API_TOOL_NAMES = {s["function"]["name"] for s in API_TOOL_SCHEMAS}

_API_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_API_MAX_BYTES = 2 * 1024 * 1024  # 响应原始上限（解析前截断）
_API_TEXT_MAX = 8000              # 返回给模型的正文上限
_API_SESSIONS: dict[int, httpx.Cookies] = {}       # host_id → 登录拿到的 cookie（login_session 型）
_API_LOCKS: dict[int, tuple[Any, asyncio.Lock]] = {}  # host_id → (loop, lock)，每域名串行（目标服务限流礼仪）
_API_LAST_CALL: dict[int, float] = {}            # host_id → 上次调用结束的 monotonic
_BLOCKED_HEADER_KEYS = {"host", "cookie", "content-length", "authorization"}  # 模型不可设（认证头只由平台注入）


def _host_lock(host_id: int) -> asyncio.Lock:
    """每域名一把锁（串行调用 + 防并发重复登录）。记录所属 loop：pytest 每次新 loop，避免跨 loop 复用报错。"""
    loop = asyncio.get_running_loop()
    entry = _API_LOCKS.get(host_id)
    if entry is None or entry[0] is not loop:
        entry = (loop, asyncio.Lock())
        _API_LOCKS[host_id] = entry
    return entry[1]


async def _find_api_host(db: AsyncSession, host: str) -> ApiHost | None:
    return (
        await db.execute(
            select(ApiHost).where(
                ApiHost.host == host, ApiHost.enabled.is_(True), ApiHost.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()


def _api_auth_config(entry: ApiHost) -> dict[str, Any]:
    """解密认证配置；none 型或配置损坏都按空配置处理（不抛异常打死对话）。"""
    if entry.auth_type == "none" or not entry.auth_config_enc:
        return {}
    try:
        return json.loads(decrypt_secret(entry.auth_config_enc))
    except Exception:
        return {}


async def _api_do_login(client: httpx.AsyncClient, scheme: str, entry: ApiHost, cfg: dict[str, Any]) -> str | None:
    """login_session 型：带门禁头调登录接口拿 cookie。返回 None=成功，否则为错误文案。"""
    login_path = str(cfg.get("login_path") or "")
    login_body = cfg.get("login_body")
    if not login_path.startswith("/") or not isinstance(login_body, dict):
        return f"域名 {entry.host} 的登录配置不完整（缺 login_path/login_body），请联系管理员检查白名单配置"
    resp = await client.post(f"{scheme}://{entry.host}{login_path}", json=login_body,
                             headers={str(k): str(v) for k, v in (cfg.get("headers") or {}).items()})
    if resp.status_code >= 400:
        return f"登录 {entry.host} 失败（HTTP {resp.status_code}）。若是 401 可能是密码变更或账号锁定，请联系管理员"
    _API_SESSIONS[entry.id] = httpx.Cookies(client.cookies)
    return None


async def _api_request(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """api_request：白名单校验 → 认证注入（固定头/登录换 cookie）→ 每域名串行发请求 → 截断返回 + 审计。"""
    method = str(args.get("method") or "").upper().strip()
    if method not in _API_METHODS:
        return f"method 必须是 {'/'.join(sorted(_API_METHODS))} 之一"
    url = (args.get("url") or "").strip()
    try:
        p = urlparse(url)
    except ValueError:
        return "URL 不合法"
    if p.scheme not in ("http", "https") or not p.hostname:
        return "URL 不合法（需要完整的 http/https 地址）"
    host = p.hostname.lower()
    entry = await _find_api_host(ctx.db, host)
    if entry is None:
        return f"域名 {host} 不在白名单内（或未启用）。可用域名见系统提示清单；需要新增请联系管理员在「模型网关-API 白名单」配置"
    allowed = {m.strip().upper() for m in (entry.allowed_methods or "GET").split(",") if m.strip()}
    if method not in allowed:
        return f"该域名仅允许 {'/'.join(sorted(allowed))} 请求（此账号多为只读，写操作被管理员禁用）"
    timeout_s = args.get("timeout_s")
    if timeout_s is not None and (not isinstance(timeout_s, int) or isinstance(timeout_s, bool) or not (1 <= timeout_s <= 120)):
        return "timeout_s 必须是 1-120 的整数（秒）"
    body = args.get("body")
    if body is not None and not isinstance(body, dict):
        return "body 必须是 JSON 对象"
    headers = {
        str(k): str(v) for k, v in (args.get("headers") or {}).items()
        if str(k).lower() not in _BLOCKED_HEADER_KEYS
    }

    cfg = _api_auth_config(entry)
    headers.update({str(k): str(v) for k, v in (cfg.get("headers") or {}).items()})  # 平台预置头（门禁）覆盖一切

    started = time.monotonic()
    error: str | None = None
    status: int | None = None
    text = ""
    # 每域名串行：登录、频率间隔、请求全在一把锁里（目标服务多为单写者/限流敏感）
    async with _host_lock(entry.id):
        try:
            async with httpx.AsyncClient(timeout=timeout_s or 30, follow_redirects=False) as client:
                if entry.id in _API_SESSIONS:
                    client.cookies.update(_API_SESSIONS[entry.id])
                if entry.auth_type == "login_session" and entry.id not in _API_SESSIONS:
                    error = await _api_do_login(client, p.scheme, entry, cfg)
                for attempt in range(2):  # 第一轮 401 → login_session 型重登一次再试
                    if error:
                        break
                    wait = entry.min_interval_ms / 1000 - (time.monotonic() - _API_LAST_CALL.get(entry.id, 0.0))
                    if wait > 0:
                        await asyncio.sleep(wait)
                    resp = await client.request(method, url, headers=headers, json=body)
                    _API_LAST_CALL[entry.id] = time.monotonic()
                    status = resp.status_code
                    if resp.status_code == 401 and entry.auth_type == "login_session" and attempt == 0:
                        error = await _api_do_login(client, p.scheme, entry, cfg)  # 会话过期重登
                        continue
                    raw = await resp.aread()
                    if len(raw) > _API_MAX_BYTES:
                        raw = raw[:_API_MAX_BYTES]
                    text = raw.decode("utf-8", errors="replace").strip()
                    break
        except httpx.TimeoutException:
            error = f"请求超时（{timeout_s or 30}s）"
        except httpx.HTTPError as e:
            error = f"请求失败：{e!s}"

    _audit_tool(ctx, "api.request", {"host": host, "method": method, "path": p.path, "status": status,
                                    "duration_ms": int((time.monotonic() - started) * 1000), "error": error})
    if error:
        return error
    if status is None:
        return "请求未完成"
    if status and status >= 300:
        # 不自动跟跳转（防跨域把 cookie 带出去）；3xx 把 Location 告诉模型让它决定
        loc = ""
        if 300 <= status < 400 and text == "":
            loc = "（响应头含跳转地址，请检查 URL 是否少了前缀/拼错）"
        return f"HTTP {status}{loc}\n{text[:1000]}"
    try:
        pretty = json.dumps(json.loads(text), ensure_ascii=False, indent=1)
    except (json.JSONDecodeError, ValueError):
        pretty = text
    if len(pretty) > _API_TEXT_MAX:
        pretty = pretty[:_API_TEXT_MAX] + "\n…[响应过长已截断，分页/加筛选条件缩小范围]"
    return f"HTTP {status}\n{pretty}" if pretty else f"HTTP {status}（响应为空）"


# ---------- 文生图工具（M13a；仅 agent.allow_image_generate 时挂载；host 侧执行，火山方舟 Seedream） ----------
# OpenAI 兼容接口，静态 Bearer key（VOLC_ARK_API_KEY，只进不出）。生成的图落工作区 gen_imgs/ 并登记
# ctx.created_files——engine 在本轮结束时把它挂到 assistant 消息 attachments，前端气泡内直接展示。

IMG_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "image_generate",
            "description": (
                "根据文字描述生成一张图片（火山方舟 Seedream 5.0）。prompt 要改写成结构化中文画面描述"
                "（主体+风格+构图+光影+色彩，把用户的口语需求专业化），不要直接把用户原话丢进去。"
                "传 images 参考图后可做图生图/局部改图/风格迁移（保持参考图主体一致性，最多 10 张）。"
                "生成结果会自动展示在用户对话里并存入工作区 gen_imgs/，完成后用一句话说明构思即可，不要描述图片细节。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "结构化中文画面描述"},
                    "size": {
                        "type": "string",
                        "description": "尺寸：1024x1024(方图，默认) / 864x1152(竖版3:4) / 1152x864(横版4:3) / 1424x800(宽屏16:9) / 800x1424(竖屏9:16) / 1568x672(横幅21:9)",
                    },
                    "images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "可选：参考图的工作区相对路径列表（最多 10 张）。用户上传的图片就在工作区（workspace_list 可查）；"
                            "「参考这张图做海报」「把这张图的背景换掉」「按这张的风格再来一张」都必须传；"
                            "迭代修改上一轮产出时传 gen_imgs/ 里对应路径。"
                        ),
                    },
                },
                "required": ["prompt"],
            },
        },
    },
]
IMG_TOOL_NAMES = {s["function"]["name"] for s in IMG_TOOL_SCHEMAS}

_ARK_IMAGES_URL = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
# Seedream 5.0 Pro 文档档位（1K 档，每张约 0.30 元）：1:1 / 3:4 / 4:3 / 16:9 / 9:16 / 21:9
_IMG_SIZES = {"1024x1024", "864x1152", "1152x864", "1424x800", "800x1424", "1568x672"}
_IMG_MAX_BYTES = 10 * 1024 * 1024
_IMG_REF_MAX = 10  # Seedream 参考图上限
_IMG_REF_MIMES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def _load_ref_images(ctx: "ToolContext", raw: Any) -> tuple[list[str] | None, str | None]:
    """把 images 参数（工作区相对路径列表）解析为 base64 data URL 列表。

    返回 (data_urls, None) 或 (None, 报错文案)。路径严格限制在当前 (user, agent) 工作区内。
    """
    if raw is None:
        return [], None
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        return None, "images 必须是工作区图片路径的列表"
    if len(raw) > _IMG_REF_MAX:
        return None, f"参考图最多 {_IMG_REF_MAX} 张，请精简后重试"
    root = _workspace_root(ctx.user.id, ctx.agent.id).resolve()
    out: list[str] = []
    for p in raw:
        rel = p.strip().lstrip("/")
        ext = Path(rel).suffix.lower()
        if not _valid_ws_path(rel):
            return None, f"参考图路径不合法：{p}"
        if ext not in _IMG_REF_MIMES:
            return None, f"参考图必须是 png/jpg/jpeg/webp：{p}"
        target = (root / rel).resolve()
        if root not in target.parents and target != root:
            return None, f"参考图路径越界，已拒绝：{p}"
        if not target.is_file():
            return None, f"工作区里找不到参考图：{rel}（先 workspace_list 确认实际路径）"
        data = target.read_bytes()
        if len(data) > _IMG_MAX_BYTES:
            return None, f"参考图超过 10MB 上限：{rel}"
        out.append(f"data:{_IMG_REF_MIMES[ext]};base64,{base64.b64encode(data).decode()}")
    return out, None


async def _image_generate(ctx: "ToolContext", args: dict[str, Any]) -> str:
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return "prompt 不能为空"
    s = get_settings()
    if not s.VOLC_ARK_API_KEY:
        return "平台未配置文生图服务（VOLC_ARK_API_KEY 未设置），请联系管理员在服务端配置后重试"
    size = args.get("size") or "1024x1024"
    if size not in _IMG_SIZES:
        size = "1024x1024"
    ref_urls, ref_err = _load_ref_images(ctx, args.get("images"))
    if ref_err:
        return ref_err
    started = time.monotonic()
    error = None
    rel = None
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            body = {"model": s.VOLC_ARK_IMAGE_MODEL, "prompt": prompt, "size": size, "response_format": "url",
                    "watermark": False}  # 默认 true 会在图上叠「AI生成」水印；企业内部使用关闭
            if ref_urls:  # 图生图/参考图：Seedream images/generations 的 image 参数（data URL 列表）
                body["image"] = ref_urls
            resp = await client.post(
                _ARK_IMAGES_URL,
                headers={"Authorization": f"Bearer {s.VOLC_ARK_API_KEY}"},
                json=body,
            )
            if resp.status_code != 200:
                error = f"文生图服务返回错误（HTTP {resp.status_code}）：{resp.text[:200]}"
            else:
                url = ((resp.json().get("data") or [{}])[0]).get("url")
                if not url:
                    error = "文生图服务响应格式异常（无图片 URL）"
                else:
                    img = await client.get(url, timeout=60)
                    mime = (img.headers.get("content-type") or "").split(";")[0].strip()
                    if img.status_code != 200 or not mime.startswith("image/") or not img.content:
                        error = "生成的图片下载失败"
                    elif len(img.content) > _IMG_MAX_BYTES:
                        error = "生成的图片超过大小限制（10MB）"
                    else:
                        ext = ".jpg" if mime == "image/jpeg" else ".png"
                        rel = f"gen_imgs/{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}{ext}"
                        await _save_feishu_bytes(ctx, rel, img.content, mime)  # 落盘+登记+commit（同款范式）
                        _note_created(ctx, str((_workspace_root(ctx.user.id, ctx.agent.id) / rel).resolve()), rel, mime)
    except httpx.TimeoutException:
        error = "文生图请求超时（120s），请重试"
    except httpx.HTTPError as e:
        error = f"文生图服务连接失败：{e!s}"
    _audit_tool(ctx, "image.generate", {
        "size": size, "prompt_len": len(prompt), "ref_images": len(ref_urls or []),
        "duration_ms": int((time.monotonic() - started) * 1000), "error": error,
    })
    if error:
        return error
    return f"已生成图片并保存到工作区：{rel}（图片已直接展示给用户，无需再贴路径或描述图片内容）"


# ---------- 飞书内容工具（M7；仅 agent.allow_feishu 时挂载；user_access_token 身份 = 用户本人视野） ----------
# 读的是「当前会话属主」的飞书可见范围：文档/知识库/群聊与单聊历史。token 由平台存取+滚动刷新，
# 模型永远接触不到 token 本身；读不到（未授权/无权限）时返回引导文案而非报错。

FEISHU_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "feishu_doc_read",
            "description": (
                "读取飞书云文档内容（用户消息里贴了 feishu.cn 或 larksuite.com 链接时用这个）。"
                "支持：docx/wiki 文档（纯文本）、sheets 表格（列 sheet 页+读指定页数据，TSV 预览）、"
                "base 多维表格（列数据表+预览记录，如实报告真实总条数——预览条数是平台预算，不是飞书接口限制）、"
                "file 云盘文件（自动下载到工作区 feishu_files/，再用 run_code 分析）。文件夹链接用 feishu_folder_list。"
                "表格/多维表格要全量统计筛选分析时传 format='csv' 全量导出（≤1 万行）到工作区后用 run_code(pandas) 处理，"
                "不要把几千行数据贴进对话。读不到（无权限/需授权）时把返回的引导原样告诉用户，不要自己编内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "飞书文档/表格/多维表格/文件链接或 token"},
                    "sheet": {"type": "string", "description": "可选。表格链接专用：sheet 页名或 sheet_id，不传读第一页"},
                    "table": {"type": "string", "description": "可选。多维表格专用：数据表名或 table_id，不传读第一个表"},
                    "format": {"type": "string", "enum": ["text", "csv"],
                               "description": "可选。表格/多维表格专用：text=预览（默认）；csv=全量导出到工作区（≤1 万行），配 run_code 分析"},
                    "max_records": {"type": "integer",
                                    "description": "可选。多维表格 text 预览条数，默认 100，上限 1000"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_folder_list",
            "description": (
                "列出飞书云文件夹的一级子项（名称+类型+链接）：文档/表格/多维表格/子文件夹/文件。"
                "用户发文件夹链接（feishu.cn/drive/folder/... 或 /folder/...）时用；"
                "子文件夹可继续用本工具展开，文档类用 feishu_doc_read 读内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "飞书文件夹链接或 folder token"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_file_upload",
            "description": (
                "把工作区里的文件（如生成的 html/报告/整理后的数据）上传到飞书云盘文件夹。"
                "path 传工作区相对路径（用 workspace_list 确认）；folder 传目标文件夹链接，不传则传到我的空间根目录。"
                "单文件 ≤20MB。上传成功返回飞书文件链接，须原样发给用户。"
                "写入纪律同其他飞书写操作：上传前先用一句话说明要传哪个文件、传到哪。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "工作区相对路径（如 output/report.html）"},
                    "folder": {"type": "string", "description": "可选。目标文件夹链接（feishu.cn/drive/folder/...）或 folder token；不传则传到我的空间根目录"},
                    "name": {"type": "string", "description": "可选。上传后的文件名（默认用原文件名）"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_doc_append",
            "description": (
                "在飞书文档（docx/wiki 文档）文末追加内容，text 直接写 markdown："
                "# 一级标题、## 二级标题、- 无序列表、1. 有序列表、> 引用、``` 代码块、"
                "- [ ] 待办、--- 分割线、**加粗**，会自动转成飞书原生排版（不要把 # 等符号当纯文本）。"
                "不支持 markdown 表格。写入前先用一句话向用户说明要写什么、写到哪，写完报告结果。"
                "单次上限 50 块 / 10000 字，超长内容分次追加。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "飞书文档链接（docx 或 wiki 文档节点）"},
                    "text": {"type": "string", "description": "要追加的正文（markdown，多行即多块）"},
                },
                "required": ["url", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_doc_create",
            "description": (
                "在飞书云盘新建一份文档（docx）：可指定目标文件夹（folder 传文件夹链接），可同时把正文写进去。"
                "text 直接写 markdown（# 标题、- 列表、**加粗** 等，同 feishu_doc_append），自动转飞书原生排版。"
                "返回新文档链接，须原样发给用户。正文单次上限 50 块 / 10000 字，超长先建文档再用 feishu_doc_append 分次追加。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "文档标题"},
                    "folder": {"type": "string", "description": "可选。目标文件夹链接（feishu.cn/drive/folder/...）或 folder token；不传则建在我的空间根目录"},
                    "text": {"type": "string", "description": "可选。正文内容（markdown，多行即多块）"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_sheet_create",
            "description": (
                "在飞书云盘新建一份电子表格（spreadsheet）：可指定目标文件夹（folder 传文件夹链接）。"
                "返回新表格链接，须原样发给用户；往里写数据用 feishu_sheet_write。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "表格标题"},
                    "folder": {"type": "string", "description": "可选。目标文件夹链接（feishu.cn/drive/folder/...）或 folder token；不传则建在我的空间根目录"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_bitable_create",
            "description": (
                "不传 app_token：在飞书云盘新建一份多维表格（base），可指定目标文件夹（folder 传文件夹链接），"
                "自带一张默认数据表，返回新表链接须原样发给用户；"
                "传 app_token：在「已存在」的多维表格里新建一张数据表（title=数据表名，可同时用 fields 建列，"
                "含关联字段：{\"name\":\"客户\",\"type\":\"关联\",\"table_id\":\"对方表id\"}）。"
                "添加/修改记录用 feishu_bitable_write。仪表盘/图表没有官方接口，仍需用户在飞书界面手动配置。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "多维表格名称；传了 app_token 时=新数据表名（必填）"},
                    "folder": {"type": "string", "description": "可选。目标文件夹链接（feishu.cn/drive/folder/...）或 folder token；不传则建在我的空间根目录；传了 app_token 时忽略"},
                    "app_token": {"type": "string", "description": "可选。已有多维表格的链接（feishu.cn/base/... 或 wiki 节点）或 app token；传了=在该多维表格里新建数据表"},
                    "fields": {"type": "array", "items": {"type": "object"},
                               "description": "可选（仅配合 app_token）。新数据表的字段定义：[{\"name\":\"金额\",\"type\":\"number\"},{\"name\":\"状态\",\"type\":\"单选\",\"options\":[\"进行中\",\"已完成\"]}]；关联字段必须带 table_id"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_bitable_field",
            "description": (
                "查看、新建或删除多维表格的字段（列）。action=list 看某张数据表已有哪些列（写记录前不确定列名时先查）；"
                "action=add 批量建列（fields 传 [{name, type, options?}]，type 支持 文本/数字/单选/多选/日期/勾选/电话/超链接/附件/关联/双向关联"
                "（或 text/number/single_select/multi_select/date/checkbox/phone/url/attachment/link/duplex_link），"
                "单选/多选必须带 options 选项名数组，关联/双向关联必须带 table_id（被关联数据表的 table_id），单次最多 20 列）；"
                "action=update 改列名/类型（fields 传 [{name, new_name?, type?, options?}]，name 指现列名或 field_id，"
                "new_name/type 至少给一个；主列（第一张表首列）不能删不能移，但可以用它改名改类型）；"
                "action=delete 按列名批量删列（fields 传列名数组 [\"列A\",\"列B\"]，单次最多 20 列；"
                "删除不可恢复，且第一张数据表的首列/主列不能删，执行前必须先向用户说明并复述将删的列、得到明确同意）。"
                "往新建的多维表格写记录前，先用本工具把需要的列建好，再用 feishu_bitable_write 写记录。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "多维表格链接（base 或 wiki 节点）"},
                    "table": {"type": "string", "description": "可选。数据表名或 table_id；不传默认第一张表"},
                    "action": {"type": "string", "enum": ["list", "add", "delete", "update"],
                               "description": "list=查看字段（默认） / add=新建字段 / update=改列名或类型 / delete=删除字段"},
                    "fields": {
                        "type": "array",
                        "description": "action=add 时必填，字段定义数组：[{\"name\":\"金额\",\"type\":\"number\"},{\"name\":\"状态\",\"type\":\"单选\",\"options\":[\"进行中\",\"已完成\"]}]；action=update 时必填，改动数组：[{\"name\":\"文本\",\"new_name\":\"门店名称\"}]；action=delete 时必填，列名数组：[\"列A\",\"列B\"]",
                        "items": {"type": "object"},
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_doc_delete_tail",
            "description": (
                "删除飞书文档（docx/wiki 文档）文末最后 N 个段落/内容块，用于撤销刚才的追加、删掉文档结尾的某段。"
                "删除不可恢复：执行前必须先向用户说明将删除末尾几段、并复述其开头内容，得到明确同意后再执行；"
                "不确定该删几段时先 feishu_doc_read 读文档确认末尾内容。单次上限 50 段。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "飞书文档链接（docx 或 wiki 文档节点）"},
                    "count": {"type": "integer", "description": "要删除的末尾段数，默认 1，最大 50"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_doc_edit",
            "description": (
                "飞书文档（docx/wiki 文档）的段落级编辑，按内容块序号（0 起）定位："
                "action=list 列出全文带序号的块清单（改中间内容前先调用拿到序号）；"
                "action=replace 把第 index 块替换成新内容（text 写 markdown，同 feishu_doc_append，可一段变多段、可换块类型）；"
                "action=insert 在第 index 块之前插入新内容（index=块总数即插到文末）；"
                "action=delete 删除第 index 块起 count 块（默认 1，单次最多 50）。"
                "纪律：编辑前必须先 action=list 确认目标序号和当前内容；replace/delete 前先向用户复述将改/删的内容、得到明确同意；"
                "编辑后序号会移位，多步编辑请从后往前做或每步重新 list。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "飞书文档链接（docx 或 wiki 文档节点）"},
                    "action": {"type": "string", "enum": ["list", "replace", "insert", "delete"],
                               "description": "list=列出带序号块清单（默认）/ replace=替换某块 / insert=在某块前插入 / delete=删除某块起 N 块"},
                    "index": {"type": "integer", "description": "目标块序号（action=list 输出的 [序号]）；replace/insert/delete 必传"},
                    "count": {"type": "integer", "description": "仅 action=delete 用：从 index 起删除的块数，默认 1，最大 50"},
                    "text": {"type": "string", "description": "仅 action=replace/insert 用：新内容（markdown，多行即多块）"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_sheet_write",
            "description": (
                "写飞书表格（sheets/wiki 表格）：mode=update 写指定范围（覆盖该范围现有内容），"
                "mode=append 在已有数据末行之后追加行。"
                "写入前先用一句话向用户说明要写什么、写到哪（哪个表、哪个范围），写完报告结果。"
                "单次上限 100 行 × 30 列，超大批次分次写。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "飞书表格链接（sheets 或 wiki 表格节点）"},
                    "mode": {"type": "string", "description": "update=写指定范围（默认）；append=末行追加"},
                    "range": {"type": "string", "description": "目标范围，如 A1:B2 或 8x8kdn!A1:B2；不带 sheetId 前缀时默认第一个 sheet 页。mode=update 必传，mode=append 可省（默认首页 A1 起追加）"},
                    "values": {"type": "array", "items": {"type": "array"},
                               "description": "二维数组，每行一个数组，如 [[\"姓名\",\"数量\"],[\"小龙虾\",3]]；单元格可以是字符串/数字/null"},
                },
                "required": ["url", "values"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_bitable_write",
            "description": (
                "写飞书多维表格（base/wiki 多维表格）：mode=add 新增记录，mode=update 按 record_id 改记录，"
                "mode=delete 按 record_id 删除记录。改/删记录前先用 feishu_doc_read 读该表拿到 record_id（每行末尾括号里）。"
                "删除不可恢复：执行前必须先向用户说明要删哪几条、复述其内容并得到明确同意。"
                "写入前先用一句话向用户说明要写什么、写到哪张表，写完报告结果。"
                "上限：新增单次 50 条、更新单次 20 条、删除单次 50 条。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "飞书多维表格链接（base 或 wiki 多维表格节点）"},
                    "mode": {"type": "string", "description": "add=新增记录（默认）；update=按 record_id 改记录；delete=按 record_id 删记录"},
                    "table": {"type": "string", "description": "数据表名或 table_id；不传默认第一个数据表"},
                    "records": {"type": "array", "items": {"type": "object"},
                                "description": "记录数组，每条是字段字典，如 [{\"标题\":\"合同A\",\"金额\":5000}]；mode=update/delete 时每条必须带 record_id 键（delete 只需要 record_id）"},
                },
                "required": ["url", "records"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_doc_search",
            "description": (
                "在用户有权限的飞书云文档里按关键词搜索（找会议纪要/制度文档/方案等，用户没给链接时用）。"
                "返回标题+链接清单；拿到链接后用 feishu_doc_read 读正文。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "count": {"type": "integer", "description": "返回条数，默认 5、最多 10"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_chat_search",
            "description": (
                "在用户所在的飞书群聊里按群名查找（总结群聊前先定位群）；query 留空则列出全部群。"
                "找到后用 feishu_chat_history 拉消息。私聊没有群名可索引，按人名找私聊请直接用 feishu_chat_history 的 chat 参数传人名。"
                "用户给了大概聊天时间时（如「昨晚」「上周」），换算成 start/end 一起传，按人名找私聊命中率高得多。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "群名关键词；留空列出全部群"},
                    "start": {"type": "string", "description": "可选，时间窗起点，格式 YYYY-MM-DD 或 YYYY-MM-DD HH:MM（东八区）；用户给了大概聊天时间时换算填入"},
                    "end": {"type": "string", "description": "可选，时间窗终点，格式同 start；缺省到现在"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_chat_history",
            "description": (
                "拉取飞书会话的历史消息（群聊/私聊均可，用于总结讨论、提炼待办）。"
                "chat 传群名、私聊对方人名或 chat_id（不确定群名时先 feishu_chat_search）；days 控制回溯天数。"
                "用户给了大概聊天时间时（如「昨晚8点」「上周三下午」），换算成 start/end 时间窗传入——"
                "按人名找私聊和拉消息都限定在窗口内，命中率比按天数盲扫高得多（消息多的用户按条数扫只能覆盖最近几小时）。"
                "消息按时间升序返回「时间 发送者：内容」，长会话会截断——先做阶段性总结再继续拉。"
                "隐私纪律：这是用户本人的聊天视野，总结只给本人看，不要写进共享区或透露给其他用户。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chat": {"type": "string", "description": "群名、私聊对方人名或 chat_id（oc_ 开头）"},
                    "days": {"type": "number", "description": "回溯天数，默认 1（昨天到现在），最大 30；传了 start 则忽略"},
                    "max_messages": {"type": "integer", "description": "最多拉取条数，默认 200、最大 500"},
                    "start": {"type": "string", "description": "可选，时间窗起点，格式 YYYY-MM-DD 或 YYYY-MM-DD HH:MM（东八区）；只给日期=当天全天"},
                    "end": {"type": "string", "description": "可选，时间窗终点，格式同 start；缺省：start 到分钟粒度则到现在，到日期粒度则当天结束"},
                },
                "required": ["chat"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_chat_daily",
            "description": (
                "按时间窗全量扫描用户近几天的飞书消息，按会话分组给出原文素材"
                "（用于「总结我今天的飞书动态/日报」这类不指定会话的需求；指定单个会话请用 feishu_chat_history）。"
                "覆盖全部群聊和私聊（私聊段标「私聊·对方姓名」）；个别消息读取失败会如实标注条数跳过。"
                "person 参数可限定只看名字匹配的会话。你负责读素材并提炼：重要事项、待办、被分享的内容/链接、"
                "需要用户跟进但可能忘记的事。隐私纪律：这是用户本人的聊天视野，摘要只给本人看。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "number", "description": "回溯天数，默认 1（今天），最大 3"},
                    "max_chats": {"type": "integer", "description": "最多扫描几个会话，默认 10、最大 20"},
                    "person": {"type": "string", "description": "只看和某人的私聊（传人名；不传则扫全部活跃会话）"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_approval_daily",
            "description": (
                "查询用户在飞书审批里的三段动态：待我审批（当前待办）/ 我发起的 / 抄送我的"
                "（用于「我有什么审批要处理」「我发起的流程到哪了」，或和 feishu_chat_daily 一起组成每日动态总结）。"
                "以应用身份读取、按当前用户过滤，只读；你负责提炼——催办重点、被驳回的、卡住的流程。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "number", "description": "「我发起的/抄送我的」回溯天数，默认 30、最大 90（待我审批始终查当前全部待办）"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_approval_detail",
            "description": (
                "取单个审批实例的明细：表单内容（事由/金额/各项填写字段）+ 审批进度（每个节点谁已同意、谁在待处理）。"
                "instance_code 从 feishu_approval_daily 列表每行末尾括号里拿；**也支持直接传业务单号**"
                "（纯数字流水号如 202608280029，工具自动在你的待办/发起/抄送列表里匹配出真实实例 code）。"
                "用户追问某条审批「具体是什么」「卡在谁那了」时用本工具深挖。只读，以应用身份读取。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "instance_code": {"type": "string", "description": "审批实例 code（GUID，daily 列表行尾括号里）或业务单号（纯数字流水号）"},
                },
                "required": ["instance_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_mail_daily",
            "description": (
                "扫描用户本人飞书邮箱近几天的邮件，给出「主题 + 收发件人 + 时间 + 正文预览 + 附件文件名 "
                "+ 未读/重要标记」素材。默认同时扫收件箱（INBOX，含发件人）和发件箱（SENT，含收件人），"
                "用于「今天我有什么邮件」「总结我今天的邮件」，或和 feishu_chat_daily / feishu_approval_daily "
                "一起组成每日动态总结。以用户本人授权身份读取，只读；你负责提炼——需要回复的、重要通知、"
                "带附件的交付物。邮件全文需要时引导用户去飞书邮箱看原文。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "number", "description": "回溯天数，默认 1（今天），最大 7"},
                    "max_messages": {"type": "integer", "description": "每个文件夹最多拉取封数，默认 30、最大 50"},
                    "folder": {"type": "string", "description": "扫哪个文件夹：inbox 只收件箱 / sent 只发件箱 / both 两者都扫（默认 both）"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "feishu_mail_search",
            "description": (
                "按关键词搜用户本人飞书邮箱里的邮件（主题/正文/收发件人命中都算），返回"
                "「主题 + 收发件人 + 时间 + 正文预览 + 附件 + 未读/重要标记」素材。用于"
                "「搜一下含『合同』的邮件」「上个月那封 XX 邮件」这类按关键词找邮件的需求；"
                "和 feishu_mail_daily（按时间扫）互补——找特定邮件用本工具，看每日动态用 daily。"
                "以用户本人授权身份读取，只读；找不到时让用户补更精确的关键词。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词（必填，如「合同」「续费」「XX 供应商」）"},
                    "max_results": {"type": "integer", "description": "最多返回封数，默认 20、最大 50"},
                },
                "required": ["query"],
            },
        },
    },
]
FEISHU_TOOL_NAMES = {s["function"]["name"] for s in FEISHU_TOOL_SCHEMAS}

# M9c：智书合同（公司级 tenant token，与飞书 per-user OAuth 不同，单独开关 allow_zhishu）
ZHISHU_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "zhishu_contract_mine",
            "description": (
                "读取当前用户本人「可见 / 申请的」智书合同列表（合同名、编号、状态、对方主体、金额、"
                "发起人、提交/签约时间）。用于「我有什么合同要看」「我最近发起的合同到哪了」"
                "「总结我今天的合同动态」，或和 feishu_chat_daily / feishu_approval_daily / "
                "feishu_mail_daily 一起组成每日动态总结。支持按合同名称关键词过滤（query 传关键词，"
                "子串模糊匹配）：「找名字里带『申请函』的合同」「那份和 XX 公司的合同」。"
                "支持按时间范围过滤（days 回溯 N 天，或 start_date/end_date 显式区间，"
                "time_field 选时间口径）：「上个月提交审批的合同」「近 7 天新建的合同」。"
                "支持按状态过滤（status_in）。"
                "以公司级应用身份读取（智书按当前用户的"
                "智书 user_id 过滤本人可见合同），只读；你负责提炼——待签约/待审批/待付款的、"
                "金额异常的、对方主体的。边界：合同明细（表单字段/付款计划）不展开，需要全文时"
                "引导用户去智书看原文。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "可选。按合同名称关键词过滤（子串模糊匹配，如「申请函」）；不传则列出全部"},
                    "days": {"type": "integer", "description": "可选。回溯最近 N 天（按 time_field 口径，最大 365）；与 start_date/end_date 二选一，显式区间优先"},
                    "start_date": {"type": "string", "description": "可选。起始日期 YYYY-MM-DD（含当天），与 end_date 搭配；传了则忽略 days"},
                    "end_date": {"type": "string", "description": "可选。结束日期 YYYY-MM-DD（含当天）；只传 start_date 则拉到至今"},
                    "time_field": {"type": "string", "description": "可选。时间口径：submit=提交审批（默认）、create=创建、update=更新、archive=归档；仅在传了 days 或日期区间时生效"},
                    "status_in": {"type": "string", "description": "可选。合同状态过滤，逗号分隔的状态码：0=编辑中 1=已取消 2=已撤销 3=审批中 4=已驳回 5=已通过 6=签订中 7=已签约 8=归档中 9=已归档 10=变更中 11=已变更 12=我方已签约 13=对方已签约。如「3,6」=审批中+签订中"},
                    "permission": {"type": "integer", "description": "1=我可见（默认），2=我申请的"},
                    "max_items": {"type": "integer", "description": "期望拉取条数，默认 20、最大 250（自动翻页，单页 50）"},
                },
            },
        },
    },
]
ZHISHU_TOOL_NAMES = {s["function"]["name"] for s in ZHISHU_TOOL_SCHEMAS}

_FEISHU_TEXT_MAX = 8000   # 返回给模型的内容上限
_FEISHU_IMG_MAX = 3       # 每次 chat_history/daily 最多下载的图片数
_FEISHU_POST_IMG_MAX = 3  # M8c.1：每个富文本帖最多下载几张附图（与 image 共享全局预算）
_SHEET_IMG_MAX = 3        # M32a：每次读表格最多下载几张浮动图片
_FEISHU_IMG_SIZE_MAX = 5 * 1024 * 1024  # 单张图片上限 5MB
_FEISHU_IMG_TTL_DAYS = 3  # M8d：feishu_imgs/ 落盘图片保留天数，到期定时清扫（sweep_feishu_images）
_FEISHU_P2P_LOOKUP_MAX = 20  # 每次调用最多解析几个 p2p 会话的对方姓名（members+contact 逐个调，防爆量）
_FEISHU_DAILY_IDS_MAX = 100     # daily 搜索接口最多取几个 message_id
_FEISHU_DAILY_RESOLVE_MAX = 80  # daily 最多逐条解析几条消息（单条 GET 逐个调，防爆量）
# M28.1 定向深扫（按人名找私聊）：逐页扫+命中即停。群消息量大时 100 条只覆盖几小时（2026-09-22 林晓搜不到事故），
# 找人场景放宽窗口；典型情况 1~2 页命中秒回。M28.3 实测高密度用户 ≈220 条/小时（1000 条只覆盖 2.3 小时），
# 条数预算再深也够不到昨天的会话——预算温和加深到 60 页/1200 条（≈5 小时），更深一律走 start/end 时间窗。
_FEISHU_P2P_TARGET_PAGES_MAX = 60    # 定向深扫最多翻几页（20 条/页 → 1200 个 message_id）
_FEISHU_P2P_TARGET_RESOLVE_MAX = 800 # 定向深扫最多解析几条消息（10 并发批量取）
_FEISHU_P2P_TARGET_LOOKUP_MAX = 40   # 定向深扫最多解析几个 p2p 会话的对方姓名
_FEISHU_URL_RE = re.compile(r"(?:feishu\.cn|larksuite\.com)/(?:drive/)?(docx|docs|doc|wiki|sheets|base|file|folder)/([A-Za-z0-9]+)")

_FEISHU_AUTH_GUIDE = (
    "需要飞书数据授权：请用户打开 [飞书数据授权](/login?upgrade=1) 点一次授权（授权的是本人飞书视野，"
    "之后长期有效），完成后重试。请把这句话和链接原样转述给用户。"
)


def _parse_feishu_dt(s: str, end_of_day: bool = False) -> int | None:
    """M28.3：start/end 参数（"YYYY-MM-DD" 或 "YYYY-MM-DD HH:MM"，东八区）→ epoch 秒；非法返回 None。
    日期粒度且 end_of_day=True 时返回次日零点（即「当天全天」的右开边界）。"""
    s = s.strip().replace("/", "-")
    for fmt, day_only in (("%Y-%m-%d %H:%M", False), ("%Y-%m-%d", True)):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        except ValueError:
            continue
        ts = int(dt.timestamp())
        return ts + 86400 if (day_only and end_of_day) else ts
    return None


def parse_feishu_url(raw: str) -> tuple[str, str] | None:
    """从消息文本/链接里解析飞书文档类型与 token。返回 (kind, token)，kind ∈ docx/wiki/sheet/base/file/folder；非法返回 None。"""
    raw = raw.strip()
    m = _FEISHU_URL_RE.search(raw)
    if m:
        kind = m.group(1)
        return (("docx" if kind in ("docs", "doc") else "sheet" if kind == "sheets" else kind), m.group(2))
    if re.fullmatch(r"[A-Za-z0-9]{20,}", raw):
        return ("docx", raw)
    return None


def _feishu_err(e: Exception) -> str:
    if isinstance(e, FeishuApiError):
        msg = str(e)
        if "scope" in msg.lower() or "permission" in msg.lower():
            return f"飞书权限不足：{msg}。可能应用还没开通对应权限或用户授权缺 scope。{_FEISHU_AUTH_GUIDE}"
        return f"飞书接口报错：{msg}（文档/群可能不存在、已删除或用户无权限查看，如实告诉用户）"
    return f"飞书请求失败：{e!s}（稍后再试，不要无脑重试）"


def _has_any_scope(ctx: "ToolContext", *cands: str) -> bool:
    """用户授权 scopes 里是否含任一候选（readonly 与写 scope 任一满足即可，M10 读写两档互认）。
    按空格切词精确匹配：子串匹配会让 "docx:document:readonly" 误命中写守卫 "docx:document"。"""
    scopes = set((getattr(ctx.user, "feishu_scopes", "") or "").split())
    return any(c in scopes for c in cands)


_FEISHU_FILE_MAX = 50 * 1024 * 1024  # M10a：云盘文件下载上限 50MB
_FEISHU_UPLOAD_MAX = 20 * 1024 * 1024  # M29：upload_all 一次性上传上限 20MB（更大需 upload_prepare/part/finish 分片，未实现）
_SHEET_READ_ROWS = 200   # 表格读取行数上限
_SHEET_READ_COLS = 30    # 表格读取列数上限（A..T）
_BITABLE_READ_ROWS = 100    # M34：多维表格预览条数（全量分析走 format=csv）
_BITABLE_READ_MAX = 1000    # M34：text 模式 max_records 夹取上限
_BITABLE_CSV_MAX = 10000    # M34：CSV 导出行数上限（100 页×100 条，页间节流 0.3s）
_SHEET_CSV_MAX = 10000      # M34：sheet CSV 导出行数上限（500 行/块分块读）
_CSV_PAGE_INTERVAL = 0.3    # M34：大批量翻页页间节流秒数（bitable 接口限流 100 次/分钟）

# M10c：docx 内嵌表格块读取（raw_content 纯文本接口取不到内嵌块，需另走 blocks 列表发现）
_EMBED_SHEET_MAX = 3     # 单次最多自动读取几个内嵌电子表格
_EMBED_BITABLE_MAX = 2   # 单次最多自动读取几个内嵌多维表格
_EMBED_SHEET_ROWS = 100  # 内嵌电子表格读取行数（文档内嵌通常是小表，比独立读表 200 小）
_EMBED_SHEET_COLS = 20
_EMBED_BITABLE_ROWS = 30
_EMBED_SECTION_MAX = 1500        # 每个内嵌块的渲染上限（给正文留预算）
_DOCX_TEXT_WITH_EMBED_MAX = 5000  # 有内嵌块时正文的截断线


def _sheet_col_letter(n: int) -> str:
    """1→A，26→Z，27→AA（范围拼装用）。"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s or "A"


def _bitable_val(v: Any) -> str:
    """多维表格字段值 → 短文本（文本段[{text}]、人员[{name}]、链接{link/text}、数字直取）。"""
    if v is None:
        return ""
    if isinstance(v, list):
        return "、".join(x for x in (_bitable_val(i) for i in v) if x)
    if isinstance(v, dict):
        return str(v.get("text") or v.get("name") or v.get("link") or "")
    return str(v)


def _sheet_lines(values: list[list], cols: int) -> list[str]:
    """二维单元格值 → TSV 行（去行尾空格单元格）。_read_sheet 与 M10c 内嵌块渲染共用。"""
    lines = []
    for row in values:
        cells = ["" if c is None else str(c) for c in row[:cols]]
        while cells and cells[-1] == "":
            cells.pop()
        lines.append("\t".join(cells))
    return lines


async def _resolve_doc_target(client: httpx.AsyncClient, token: str, kind: str, doc_token: str):
    """wiki 节点解析为实体（docx/sheet/base）；非 wiki 直通。M10b 起读写工具共用。
    返回 (kind, token, title)；不支持的 wiki 类型返回错误文案字符串。"""
    if kind != "wiki":
        return kind, doc_token, ""
    node = await wiki_resolve(client, token, doc_token)
    wiki_kind = {"docx": "docx", "sheet": "sheet", "bitable": "base"}.get(node["obj_type"])
    if not wiki_kind:
        return f"该知识库节点是 {node['obj_type']} 类型，暂不支持（支持文档/表格/多维表格）"
    return wiki_kind, node["obj_token"], node["title"]


async def _embedded_sections(ctx: "ToolContext", client: httpx.AsyncClient, token: str,
                             doc_id: str) -> tuple[list[str], dict]:
    """M10c：发现 docx 内嵌的电子表格/多维表格块并读取内容附在正文后。

    内嵌块 token 格式：sheet.token={spreadsheet_token}_{sheet_id}，bitable.token={app_token}_{table_id}；
    按字段存在性判定（block_type 数值各版本口径不一，不写死）。
    逐块独立 try/except 降级——单块失败只换提示行，绝不拖垮正文。
    返回 (节文本列表, {"sheet": 成功数, "bitable": 成功数})（审计计数用）。
    """
    blocks = await docx_blocks(client, token, doc_id)
    # M10c.1：块列表按文档顺序返回——线性扫描维护标题栈，给每个内嵌块记章节归属
    # （内嵌节追加在正文末尾会丢位置，模型只能靠数据内容猜归属，多章节周报场景致命）
    sheet_toks: list[tuple[str, str]] = []   # (token, 章节路径)
    bitable_toks: list[tuple[str, str]] = []
    trail: dict[int, str] = {}               # 标题层级 → 文本

    def _path() -> str:
        return " > ".join(trail[lvl] for lvl in sorted(trail))[:80]

    for b in blocks:
        for lvl in range(1, 10):
            h = b.get(f"heading{lvl}")
            if isinstance(h, dict):
                text = "".join(
                    str((e.get("text_run") or {}).get("content") or "")
                    for e in h.get("elements") or [] if isinstance(e, dict)
                ).strip()[:30]
                if text:
                    trail[lvl] = text
                    for deeper in [k for k in trail if k > lvl]:
                        del trail[deeper]
                break
        st = str((b.get("sheet") or {}).get("token") or "")
        if "_" in st and st not in [t for t, _ in sheet_toks]:
            sheet_toks.append((st, _path()))
        bt = str((b.get("bitable") or {}).get("token") or "")
        if "_" in bt and bt not in [t for t, _ in bitable_toks]:
            bitable_toks.append((bt, _path()))
    if not sheet_toks and not bitable_toks:
        return [], {"sheet": 0, "bitable": 0}

    sections: list[str] = []
    ok = {"sheet": 0, "bitable": 0}

    if sheet_toks:
        if not _has_any_scope(ctx, "sheets:spreadsheet:readonly", "sheets:spreadsheet"):
            sections.append(f"【内嵌电子表格】检测到 {len(sheet_toks)} 个，读取需要表格授权：{_FEISHU_AUTH_GUIDE}")
        else:
            for i, (st, path) in enumerate(sheet_toks[:_EMBED_SHEET_MAX], 1):
                ss_token, sheet_id = st.split("_", 1)
                label = f"【内嵌电子表格 {i}】" + (f"位于「{path}」" if path else "")
                link = f"https://feishu.cn/sheets/{ss_token}"
                try:
                    rng = f"{sheet_id}!A1:{_sheet_col_letter(_EMBED_SHEET_COLS)}{_EMBED_SHEET_ROWS}"
                    values = await sheet_values(client, token, ss_token, rng)
                    body = "\n".join(_sheet_lines(values, _EMBED_SHEET_COLS)).strip() or "（空表）"
                    sec = f"{label}（前 {len(values)} 行；读全量/换页可用 feishu_doc_read 打开 {link}）：\n{body}"
                    ok["sheet"] += 1
                except Exception as e:
                    sec = f"{label}读取失败（可尝试用 feishu_doc_read 打开 {link}）：{_feishu_err(e)}"
                sections.append(sec[:_EMBED_SECTION_MAX])
            if len(sheet_toks) > _EMBED_SHEET_MAX:
                rest = "、".join(f"https://feishu.cn/sheets/{t.split('_', 1)[0]}" for t, _ in sheet_toks[_EMBED_SHEET_MAX:])
                sections.append(f"还有 {len(sheet_toks) - _EMBED_SHEET_MAX} 个内嵌电子表格未自动读取，可逐个打开：{rest}")

    if bitable_toks:
        if not _has_any_scope(ctx, "bitable:app:readonly", "bitable:app"):
            sections.append(f"【内嵌多维表格】检测到 {len(bitable_toks)} 个，读取需要多维表格授权：{_FEISHU_AUTH_GUIDE}")
        else:
            for i, (bt, path) in enumerate(bitable_toks[:_EMBED_BITABLE_MAX], 1):
                app_token, table_id = bt.split("_", 1)
                label = f"【内嵌多维表格 {i}】" + (f"位于「{path}」" if path else "")
                link = f"https://feishu.cn/base/{app_token}"
                try:
                    records, _ = await bitable_records(client, token, app_token, table_id, max_items=_EMBED_BITABLE_ROWS)
                    lines = []
                    for r in records:
                        kv = "；".join(f"{k}: {_bitable_val(v)}" for k, v in (r["fields"] or {}).items())
                        lines.append(f"- {kv or '（空记录）'}")
                    body = "\n".join(lines).strip() or "（空表）"
                    sec = f"{label}（前 {len(records)} 条记录；完整数据可用 feishu_doc_read 打开 {link}）：\n{body}"
                    ok["bitable"] += 1
                except Exception as e:
                    sec = f"{label}读取失败（可尝试用 feishu_doc_read 打开 {link}）：{_feishu_err(e)}"
                sections.append(sec[:_EMBED_SECTION_MAX])
            if len(bitable_toks) > _EMBED_BITABLE_MAX:
                rest = "、".join(f"https://feishu.cn/base/{t.split('_', 1)[0]}" for t, _ in bitable_toks[_EMBED_BITABLE_MAX:])
                sections.append(f"还有 {len(bitable_toks) - _EMBED_BITABLE_MAX} 个内嵌多维表格未自动读取，可逐个打开：{rest}")

    return sections, ok


async def _feishu_doc_read(ctx: "ToolContext", args: dict[str, Any]) -> str:
    parsed = parse_feishu_url(str(args.get("url") or ""))
    if not parsed:
        return "无法识别的飞书链接（支持 docx/wiki 文档、sheets 表格、base 多维表格、folder 文件夹、file 文件链接）"
    kind, token = parsed
    if kind == "folder":
        return "这是文件夹链接：请改用 feishu_folder_list 列出其中内容，再对具体文件用 feishu_doc_read。"
    # scope 早退守卫（取 token 之前；wiki 解析后类型才确定的，在 _read_* 里还有一道）
    if kind == "sheet" and not _has_any_scope(ctx, "sheets:spreadsheet:readonly", "sheets:spreadsheet"):
        return _FEISHU_AUTH_GUIDE
    if kind == "base" and not _has_any_scope(ctx, "bitable:app:readonly", "bitable:app"):
        return _FEISHU_AUTH_GUIDE
    if kind == "file" and not _has_any_scope(ctx, "drive:drive:readonly", "drive:drive"):
        return _FEISHU_AUTH_GUIDE
    token_str = await get_feishu_user_token(ctx.db, ctx.user)
    if not token_str:
        return _FEISHU_AUTH_GUIDE
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resolved = await _resolve_doc_target(client, token_str, kind, token)
            if isinstance(resolved, str):
                return resolved
            kind, token, title = resolved
            if kind == "docx":
                content = await read_doc_raw(client, token_str, token)
                # M10c：raw_content 纯文本接口取不到内嵌表格块，另走 blocks 列表发现并读取附在正文后
                try:
                    sections, embed_ok = await _embedded_sections(ctx, client, token_str, token)
                except Exception:
                    sections, embed_ok = [], {"sheet": 0, "bitable": 0}
                _audit_tool(ctx, "feishu.doc_read", {"kind": kind, "doc_token": token,
                                                     "chars": len(content), "embedded": embed_ok})
                if not content.strip() and not sections:
                    return "文档内容为空（或只有图片/表格块，纯文本接口取不到）"
                head = f"《{title}》\n" if title else ""
                if sections and len(content) > _DOCX_TEXT_WITH_EMBED_MAX:
                    content = content[:_DOCX_TEXT_WITH_EMBED_MAX] + "\n…[正文过长已截断，可针对重点章节提问]"
                elif len(content) > _FEISHU_TEXT_MAX:
                    content = content[:_FEISHU_TEXT_MAX] + "\n…[文档过长已截断，可针对重点章节提问]"
                out = head + content
                if sections:
                    out += "\n\n" + "\n\n".join(sections)
                if len(out) > _FEISHU_TEXT_MAX:
                    out = out[:_FEISHU_TEXT_MAX] + "\n…[内容过多已截断]"
                return out
            if kind == "sheet":
                return await _read_sheet(ctx, client, token_str, token, args, title)
            if kind == "base":
                return await _read_bitable(ctx, client, token_str, token, args, title)
            if kind == "file":
                return await _read_drive_file(ctx, client, token_str, token)
    except Exception as e:
        return _feishu_err(e)
    return f"暂不支持的链接类型：{kind}"


async def _read_sheet(ctx: "ToolContext", client: httpx.AsyncClient, token: str,
                      ss_token: str, args: dict[str, Any], title: str) -> str:
    """读 sheets 表格：先列 sheet 页，再读指定页（或首页）前 200 行 × 30 列，渲染 TSV。
    M32a：浮动图片（values 接口取不到）走 float_images 列表+drive 媒体下载，落 feishu_imgs/ 标注路径
    （聊天图片同款 OCR 链路）；单图失败跳过不阻断读表。已知边界：单元格内嵌图片无列表 API。"""
    if not _has_any_scope(ctx, "sheets:spreadsheet:readonly", "sheets:spreadsheet"):
        return _FEISHU_AUTH_GUIDE
    sheets = await sheet_meta(client, token, ss_token)
    if not sheets:
        return "该表格没有任何 sheet 页（或无权限）"
    want = str(args.get("sheet") or "").strip()
    cur = next((s for s in sheets if want and want in (s["title"], s["sheet_id"])), sheets[0])
    head_parts = [f"表格《{title}》" if title else "表格", f"共 {len(sheets)} 个 sheet 页："]
    sheet_list = "、".join(f"{s['title']}（{s['row_count']}行×{s['column_count']}列）" for s in sheets)
    if str(args.get("format") or "text").strip().lower() == "csv":
        # M34：全量导出通道（分块读 ≤1 万行），落工作区交给 run_code(pandas)；跳过浮动图片
        return await _sheet_csv_export(ctx, client, token, ss_token, cur, "".join(head_parts) + sheet_list)
    rng = f"{cur['sheet_id']}!A1:{_sheet_col_letter(_SHEET_READ_COLS)}{_SHEET_READ_ROWS}"
    values = await sheet_values(client, token, ss_token, rng)
    img_note, saved_imgs = await _sheet_float_imgs_note(ctx, client, token, ss_token, cur["sheet_id"])
    _audit_tool(ctx, "feishu.doc_read",
                {"kind": "sheet", "doc_token": ss_token, "sheet": cur["title"],
                 "rows": len(values), "images_saved": saved_imgs})
    head = "".join(head_parts) + sheet_list + f"。当前读取「{cur['title']}」前 {len(values)} 行"
    if cur["row_count"] > _SHEET_READ_ROWS or cur["column_count"] > _SHEET_READ_COLS:
        head += f"（原表 {cur['row_count']}行×{cur['column_count']}列，已截断到 {_SHEET_READ_ROWS} 行 × {_SHEET_READ_COLS} 列；可用 sheet 参数换页，或 format='csv' 全量导出到工作区用 run_code 分析）"
    out = head + "：\n\n" + "\n".join(_sheet_lines(values, _SHEET_READ_COLS)) + img_note
    if len(out) > _FEISHU_TEXT_MAX:
        out = out[:_FEISHU_TEXT_MAX] + "\n…[内容过多已截断]"
    return out


async def _sheet_float_imgs_note(ctx: "ToolContext", client: httpx.AsyncClient, token: str,
                                 ss_token: str, sheet_id: str) -> tuple[str, int]:
    """sheet 浮动图片：列表→下载前 _SHEET_IMG_MAX 张→落盘标注。任何一步失败降级为只有提示/空串。"""
    try:
        floats = await sheet_float_images(client, token, ss_token, sheet_id)
    except Exception:
        return "", 0  # 缺权限/接口失败不阻断读表
    if not floats:
        return "", 0
    paths: list[str] = []
    for fi in floats[:_SHEET_IMG_MAX]:
        try:
            data = await drive_download_media(client, token, fi["file_token"])
            if len(data) <= _FEISHU_IMG_SIZE_MAX:
                rel = f"feishu_imgs/sheet_{ss_token[-6:]}_{fi['file_token'][-6:]}.png"
                paths.append(await _save_feishu_bytes(ctx, rel, data, "image/png"))
        except Exception:
            pass
    anchors = "、".join(fi["range"] for fi in floats[:5] if fi.get("range"))
    where = f"（锚点 {anchors}）" if anchors else ""
    if paths:
        note = (f"已存 {len(paths)} 张到工作区：{'、'.join(paths)}，"
                "需要图中内容用 run_code（rapidocr）识别后与表格数据合并理解")
    else:
        note = "下载失败（可能缺 drive:drive:readonly 授权），可在飞书里查看"
    more = f"，另有 {len(floats) - _SHEET_IMG_MAX} 张未下载" if len(floats) > _SHEET_IMG_MAX else ""
    return (f"\n\n[本页含 {len(floats)} 张浮动图片{where}：{note}{more}。"
            "单元格内嵌图片无接口可读，只有浮动图片能取到]"), len(paths)


async def _sheet_csv_export(ctx: "ToolContext", client: httpx.AsyncClient, token: str,
                            ss_token: str, cur: dict, head: str) -> str:
    """M34：sheet 全量导出 CSV——500 行/块分块读（values 接口按 range 读，空块收尾），
    ≤_SHEET_CSV_MAX 行 × _SHEET_READ_COLS 列，落工作区交给 run_code(pandas)。"""
    cols_n = _SHEET_READ_COLS
    col_end = _sheet_col_letter(cols_n)
    all_rows: list[list] = []
    start = 1
    while len(all_rows) < _SHEET_CSV_MAX:
        end = min(start + 499, _SHEET_CSV_MAX)  # 每块最多 500 行
        block = await sheet_values(client, token, ss_token,
                                   f"{cur['sheet_id']}!A{start}:{col_end}{end}")
        if not block:
            break
        all_rows.extend(block)
        if len(block) < end - start + 1:
            break  # 块尾有空行被飞书裁掉 = 读完了
        start = end + 1
        if len(all_rows) < _SHEET_CSV_MAX:
            await asyncio.sleep(_CSV_PAGE_INTERVAL)
    if not all_rows:
        return head + f"。当前页「{cur['title']}」没有任何数据"
    lines = [",".join(_csv_cell(c) for c in row[:cols_n]) for row in all_rows]
    data = b"\xef\xbb\xbf" + "\n".join(lines).encode("utf-8")  # utf-8 BOM：Excel/pandas 直读不乱码
    safe = _sanitize_name(cur["title"]) or cur["sheet_id"]
    rel = await _save_feishu_bytes(ctx, f"feishu_files/{ss_token[-6:]}_{safe}.csv", data, "text/csv")
    _audit_tool(ctx, "feishu.doc_read",
                {"kind": "sheet", "doc_token": ss_token, "sheet": cur["title"],
                 "rows": len(all_rows), "format": "csv"})
    truncated = len(all_rows) >= _SHEET_CSV_MAX
    note = (f"（已达单次 {_SHEET_CSV_MAX} 行上限只导出前 {len(all_rows)} 行；"
            "完整数据请在飞书界面导出或分页处理）") if truncated else f"（共 {len(all_rows)} 行全量）"
    return (f"{head}。当前页「{cur['title']}」已导出到工作区：{rel}"
            f"（{len(data) // 1024}KB，{cols_n} 列）{note}。"
            "请用 run_code（pandas，read_csv 直接读，已带 utf-8-sig 头）做统计分析，不要把全量数据贴进对话。")


def _csv_cell(v: Any) -> str:
    """CSV 单元格转义（逗号/引号/换行）。M34 导出通道共用。"""
    s = "" if v is None else str(v)
    if any(c in s for c in ',"\n\r'):
        return '"' + s.replace('"', '""') + '"'
    return s


async def _read_bitable(ctx: "ToolContext", client: httpx.AsyncClient, token: str,
                        app_token: str, args: dict[str, Any], title: str) -> str:
    """读多维表格：列数据表。text=预览（如实报真实总条数，前 max_records 条，默认 100 夹取 1~1000）；
    format=csv=全量导出（≤1 万行，翻页节流）落工作区 feishu_files/，引导 run_code(pandas) 分析。
    M34：原「只能读 50 条」是本平台自己的预览预算，不是飞书接口限制。"""
    if not _has_any_scope(ctx, "bitable:app:readonly", "bitable:app"):
        return _FEISHU_AUTH_GUIDE
    tables = await bitable_tables(client, token, app_token)
    if not tables:
        return "该多维表格没有任何数据表（或无权限）"
    want = str(args.get("table") or "").strip()
    cur = next((t for t in tables if want and want in (t["name"], t["table_id"])), tables[0])
    head = (f"多维表格《{title}》" if title else "多维表格") + \
        f"，共 {len(tables)} 个数据表：" + "、".join(t["name"] for t in tables) + \
        f"。当前表「{cur['name']}」"
    if str(args.get("format") or "text").strip().lower() == "csv":
        return await _bitable_csv_export(ctx, client, token, app_token, cur, head)
    try:
        max_records = max(1, min(int(args.get("max_records") or _BITABLE_READ_ROWS), _BITABLE_READ_MAX))
    except (TypeError, ValueError):
        return "max_records 必须是整数（1~1000）"
    records, total = await bitable_records(client, token, app_token, cur["table_id"],
                                           max_items=max_records)
    _audit_tool(ctx, "feishu.doc_read",
                {"kind": "base", "doc_token": app_token, "table": cur["name"],
                 "rows": len(records), "total": total, "format": "text"})
    lines = []
    for r in records:
        kv = "；".join(f"{k}: {_bitable_val(v)}" for k, v in (r["fields"] or {}).items())
        lines.append(f"- {kv or '（空记录）'}（record_id: {r['record_id']}）")
    out = head + f"，共 {total} 条记录，预览前 {len(records)} 条：\n\n" + "\n".join(lines)
    if len(out) > _FEISHU_TEXT_MAX:
        out = out[:_FEISHU_TEXT_MAX] + "\n…[预览过长已截断；要全量分析请 format='csv' 导出到工作区后用 run_code 处理]"
    elif total > len(records):
        out += (f"\n\n（还有 {total - len(records)} 条未读：可调大 max_records（≤{_BITABLE_READ_MAX}），"
                "或 format='csv' 全量导出到工作区用 run_code 分析）")
    return out


async def _bitable_csv_export(ctx: "ToolContext", client: httpx.AsyncClient, token: str,
                              app_token: str, cur: dict, head: str) -> str:
    """M34：bitable 全量导出 CSV（≤_BITABLE_CSV_MAX 行，页间节流）落工作区，交给 run_code/pandas。"""
    records, total = await bitable_records(client, token, app_token, cur["table_id"],
                                           max_items=_BITABLE_CSV_MAX,
                                           page_interval=_CSV_PAGE_INTERVAL)
    if not records:
        return head + "：该表没有任何记录"
    try:
        cols = [f["name"] for f in await bitable_fields(client, token, app_token, cur["table_id"])]
    except Exception:
        cols = []
    seen = set(cols)
    for r in records:  # 补齐字段清单里没有的列（公式/系统等），保持出现顺序
        for k in (r["fields"] or {}):
            if k not in seen:
                seen.add(k)
                cols.append(k)
    lines = [",".join(_csv_cell(c) for c in cols)]
    for r in records:
        f = r["fields"] or {}
        lines.append(",".join(_csv_cell(_bitable_val(f.get(c))) for c in cols))
    data = b"\xef\xbb\xbf" + "\n".join(lines).encode("utf-8")  # utf-8 BOM：Excel/pandas 直读不乱码
    safe = _sanitize_name(cur["name"]) or cur["table_id"]
    rel = await _save_feishu_bytes(ctx, f"feishu_files/{app_token[-6:]}_{safe}.csv", data, "text/csv")
    _audit_tool(ctx, "feishu.doc_read",
                {"kind": "base", "doc_token": app_token, "table": cur["name"],
                 "rows": len(records), "total": total, "format": "csv"})
    note = (f"（该表共 {total} 条，已达单次 {_BITABLE_CSV_MAX} 行上限只导出前 {len(records)} 行；"
            "完整数据请在飞书界面导出或分表处理）") if total > len(records) else f"（共 {len(records)} 行全量）"
    return (f"{head}已导出到工作区：{rel}（{len(data) // 1024}KB，{len(cols)} 列）{note}。"
            "请用 run_code（pandas，read_csv 直接读，已带 utf-8-sig 头）做统计分析，不要把全量记录贴进对话。")


async def _read_drive_file(ctx: "ToolContext", client: httpx.AsyncClient, token: str, file_token: str) -> str:
    """云盘文件链接 → 下载落工作区 feishu_files/，引导 run_code/预览分析。"""
    if not _has_any_scope(ctx, "drive:drive:readonly", "drive:drive"):
        return _FEISHU_AUTH_GUIDE
    data, fname = await drive_download_file(client, token, file_token)
    if len(data) > _FEISHU_FILE_MAX:
        return f"文件 {len(data) / 1024 / 1024:.0f}MB 超过 {_FEISHU_FILE_MAX // 1024 // 1024}MB 上限，请用户下载后自行上传需要的部分"
    # M10a-1：清洗到工作区合法单段（M21 起只替换黑名单字符，空格/括号保留原名可读）
    safe = _sanitize_name(fname) or f"{file_token[-6:]}.bin"
    renamed = safe != fname
    rel = f"feishu_files/{file_token[-6:]}_{safe}"
    import mimetypes as _mt
    mime = _mt.guess_type(safe)[0] or "application/octet-stream"
    rel = await _save_feishu_bytes(ctx, rel, data, mime)
    _audit_tool(ctx, "feishu.doc_read", {"kind": "file", "doc_token": file_token, "bytes": len(data)})
    rename_note = f"（原文件名 {fname} 含特殊字符，已清洗）" if renamed else ""
    return (f"云盘文件已下载到工作区：{rel}（{len(data) / 1024:.0f}KB，{safe}）{rename_note}。"
            "用户可在工作区预览；需要分析内容时用 run_code 读取该文件（Excel 用 pandas/openpyxl，"
            "图片用 rapidocr，PDF 用 pypdfium2 提取文字（扫描件渲染成图后 rapidocr 识别），Word 用 python-docx）。")


# ---------- M10b：写入工具（docx 追加 / sheet 写值 / bitable 增改记录） ----------

_DOCX_APPEND_MAX_PARAS = 50     # 单次追加段落上限
_DOCX_APPEND_MAX_CHARS = 10000  # 单次追加总字数上限
_DOCX_TABLE_ROWS_MAX = 30       # M32b：单次写入表格行数上限（含表头）
_DOCX_TABLE_COLS_MAX = 10       # M32b：单次写入表格列数上限
_SHEET_WRITE_ROWS = 100         # 单次写表行数上限
_SHEET_WRITE_COLS = 30          # 单次写表列数上限
_BITABLE_ADD_MAX = 50           # 单次新增记录上限
_BITABLE_UPD_MAX = 20           # 单次更新记录上限
_BITABLE_DEL_MAX = 50           # 单次删除记录上限

_MD_HEADING_RE = re.compile(r"^(#{1,9})\s*(.+)$")
_MD_ORDERED_RE = re.compile(r"^\d+[.、)]\s+(.+)$")
_MD_TODO_RE = re.compile(r"^[-*]\s+\[( |x|X)\]\s*(.*)$")
_MD_BOLD_RE = re.compile(r"(\*\*[^*]+\*\*)")


def _md_inline(content: str) -> list[dict]:
    """行内解析：**粗体** → text_run + bold 样式，其余纯文本。"""
    elements = []
    for part in _MD_BOLD_RE.split(content):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            elements.append({"text_run": {"content": part[2:-2],
                                          "text_element_style": {"bold": True}}})
        else:
            elements.append({"text_run": {"content": part}})
    return elements or [{"text_run": {"content": ""}}]


_MD_TABLE_SEP_CELL_RE = re.compile(r":?-{3,}:?")


def _md_table_cells(line: str) -> list[str]:
    """一行 GFM 表格行 → 单元格文本列表（剥首尾 | 与空白）。"""
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _try_md_table(lines: list[str], i: int) -> tuple[dict, int] | None:
    """从 lines[i] 起尝试解析 GFM 表格（表头行+分隔行起步，列数须一致）。
    命中返回 ({"_table_rows": [[cell...]], "_table_truncated": bool} 哨兵块, 消耗行数)，否则 None。"""
    head = _md_table_cells(lines[i])
    if len(head) < 2 or i + 1 >= len(lines):
        return None
    sep = _md_table_cells(lines[i + 1])
    if len(sep) != len(head) or not all(_MD_TABLE_SEP_CELL_RE.fullmatch(c) for c in sep):
        return None
    rows = [head]
    j = i + 2
    while j < len(lines):
        s = lines[j].strip()
        if not s or "|" not in s:
            break
        cells = _md_table_cells(s)
        if len(cells) != len(head):
            break
        rows.append(cells)
        j += 1
    truncated = False
    if len(rows) > _DOCX_TABLE_ROWS_MAX:
        rows, truncated = rows[:_DOCX_TABLE_ROWS_MAX], True
    if len(head) > _DOCX_TABLE_COLS_MAX:
        rows = [r[:_DOCX_TABLE_COLS_MAX] for r in rows]
        truncated = True
    return {"_table_rows": rows, "_table_truncated": truncated}, j - i


def _md_to_blocks(text: str) -> list[dict]:
    """轻量 markdown → 飞书 docx 原生块：# 标题 1-9、- 无序列表、1. 有序列表、> 引用、
    ``` 代码块、--- 分割线、- [ ]/- [x] 待办、| GFM 表格 |（哨兵块，写入方走 descendant 接口建真表格），
    其余非空行 → 纯文本段落。不做：嵌套列表缩进（拍平）。"""
    blocks: list[dict] = []
    in_code = False
    code_lines: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("```"):
            if in_code:
                blocks.append({"block_type": 14, "code": {
                    "elements": [{"text_run": {"content": "\n".join(code_lines)}}],
                    "style": {"language": 1}}})  # 1=PlainText
                code_lines, in_code = [], False
            else:
                in_code = True
            i += 1
            continue
        if in_code:
            code_lines.append(ln)
            i += 1
            continue
        s = ln.strip()
        if not s:
            i += 1
            continue
        if "|" in s:
            tbl = _try_md_table(lines, i)
            if tbl:
                sentinel, consumed = tbl
                blocks.append(sentinel)
                i += consumed
                continue
        m = _MD_HEADING_RE.match(s)
        if m:
            level = len(m.group(1))
            key = f"heading{level}"
            blocks.append({"block_type": 2 + level, key: {"elements": _md_inline(m.group(2))}})
            i += 1
            continue
        if s in ("---", "***", "___"):
            blocks.append({"block_type": 22, "divider": {}})
            i += 1
            continue
        if s.startswith(">"):
            blocks.append({"block_type": 15, "quote": {"elements": _md_inline(s.lstrip("> ").strip())}})
            i += 1
            continue
        m = _MD_TODO_RE.match(s)
        if m:
            blocks.append({"block_type": 17, "todo": {
                "elements": _md_inline(m.group(2)),
                "style": {"done": m.group(1).lower() == "x"}}})
            i += 1
            continue
        if s.startswith(("- ", "* ")):
            blocks.append({"block_type": 12, "bullet": {"elements": _md_inline(s[2:])}})
            i += 1
            continue
        m = _MD_ORDERED_RE.match(s)
        if m:
            blocks.append({"block_type": 13, "ordered": {"elements": _md_inline(m.group(1))}})
            i += 1
            continue
        blocks.append({"block_type": 2, "text": {"elements": _md_inline(s)}})
        i += 1
    if code_lines:  # 未闭合的 ``` 围栏兜底：按代码块落
        blocks.append({"block_type": 14, "code": {
            "elements": [{"text_run": {"content": "\n".join(code_lines)}}],
            "style": {"language": 1}}})
    return blocks


def _table_descendants(rows: list[list[str]], tag: str) -> list[dict]:
    """GFM 表格行 → docx 表格嵌套块：table(31)→R×C 个 table_cell(32)→每格 1 个 text(2)，
    临时 block_id 客户端自带（t1/t1c0_0/t1c0_0t），供 descendant 接口建真实表格。"""
    r = len(rows)
    c = max(len(row) for row in rows)
    cell_ids = [f"{tag}c{i}_{j}" for i in range(r) for j in range(c)]
    out = [{"block_id": tag, "block_type": 31, "children": cell_ids,
            "table": {"property": {"row_size": r, "column_size": c}}}]
    for i in range(r):
        for j in range(c):
            cid = f"{tag}c{i}_{j}"
            tid = f"{cid}t"
            cell_text = rows[i][j] if j < len(rows[i]) else ""
            out.append({"block_id": cid, "block_type": 32, "children": [tid], "table_cell": {}})
            out.append({"block_id": tid, "block_type": 2,
                        "text": {"elements": _md_inline(cell_text)}})
    return out


async def _docx_write_blocks(client: httpx.AsyncClient, token_str: str, doc_id: str,
                             blocks: list[dict], index: int | None = None) -> tuple[int, bool]:
    """分段下发：连续普通块走 docx_append_blocks，表格哨兵块走 docx_create_descendants。
    index 传了=从该位次插入（逐段递增保持顺序）。返回 (写入顶层块数, 是否有表格被截断)。"""
    written, table_truncated = 0, False
    seg: list[dict] = []

    async def flush() -> None:
        nonlocal written, index
        if not seg:
            return
        batch = list(seg)  # 传副本：seg 马上清空复用，不能把同一列表对象交给调用方
        seg.clear()
        n = await docx_append_blocks(client, token_str, doc_id, batch, index=index)
        written += n
        if index is not None:
            index += n

    tno = 0
    for b in blocks:
        rows = b.get("_table_rows")
        if not rows:
            seg.append(b)
            continue
        await flush()
        tno += 1
        tag = f"t{tno}"
        await docx_create_descendants(client, token_str, doc_id, [tag],
                                      _table_descendants(rows, tag), index=index)
        written += 1
        if index is not None:
            index += 1
        if b.get("_table_truncated"):
            table_truncated = True
    await flush()
    return written, table_truncated


def _blocks_plain_len(blocks: list[dict]) -> int:
    """块列表的纯文本字符数（剔除 markdown 符号，审计/上限用）。表格哨兵按单元格文本计。"""
    total = 0
    for b in blocks:
        rows = b.get("_table_rows")
        if rows:
            total += sum(len(c) for r in rows for c in r)
            continue
        for key in _BLOCK_TEXT_KEYS + ("code",):
            node = b.get(key)
            if isinstance(node, dict):
                total += sum(len(e.get("text_run", {}).get("content", ""))
                             for e in node.get("elements") or [] if isinstance(e, dict))
                break
    return total


def _md_blocks_capped(text: str) -> tuple[list[dict], bool]:
    """markdown → 块列表并套用单次上限（块数/纯文本字数）。返回 (块列表, 是否截断)。"""
    blocks = _md_to_blocks(text)
    truncated = False
    if len(blocks) > _DOCX_APPEND_MAX_PARAS:
        blocks, truncated = blocks[:_DOCX_APPEND_MAX_PARAS], True
    kept, acc = [], 0
    for b in blocks:
        blen = _blocks_plain_len([b])
        if acc + blen > _DOCX_APPEND_MAX_CHARS:
            truncated = True
            break
        kept.append(b)
        acc += blen
    return kept, truncated


async def _feishu_doc_append(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """docx 文末追加纯文本段落（按行拆段，去空行）。"""
    parsed = parse_feishu_url(str(args.get("url") or ""))
    if not parsed:
        return "无法识别的飞书链接（支持 docx/wiki 文档链接）"
    kind, token = parsed
    if kind not in ("docx", "wiki"):
        return f"这是 {kind} 类型的链接：表格写入用 feishu_sheet_write，多维表格用 feishu_bitable_write，feishu_doc_append 只支持文档"
    if not _has_any_scope(ctx, "docx:document"):
        return _FEISHU_AUTH_GUIDE
    text = str(args.get("text") or "").strip()
    if not text:
        return "text 为空：请给出要追加的正文内容"
    token_str = await get_feishu_user_token(ctx.db, ctx.user)
    if not token_str:
        return _FEISHU_AUTH_GUIDE
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resolved = await _resolve_doc_target(client, token_str, kind, token)
            if isinstance(resolved, str):
                return resolved
            kind, token, title = resolved
            if kind != "docx":
                return f"该知识库节点是{'表格' if kind == 'sheet' else '多维表格'}：请改用对应的写入工具"
            paras, truncated = _md_blocks_capped(text)
            if not paras:
                return "内容为空：请给出有效正文（当前内容转换后没有任何可写入的块）"
            n, tbl_trunc = await _docx_write_blocks(client, token_str, token, paras)
            _audit_tool(ctx, "feishu.doc_append",
                        {"doc_token": token, "paragraphs": n, "chars": _blocks_plain_len(paras)})
    except Exception as e:
        return _feishu_err(e)
    head = f"《{title}》" if title else "文档"
    note = "（内容超上限已截断，剩余部分请分次追加）" if truncated else ""
    tbl_note = "（表格超出 30 行 × 10 列上限已截断）" if tbl_trunc else ""
    return f"已在{head}文末追加 {n} 个内容块（标题/段落/列表/表格等）{note}{tbl_note}。请向用户说明写了什么、写到哪。"


def _parse_folder_token(raw: str) -> str:
    """文件夹链接/裸 token → folder_token；识别不了返回空串。"""
    raw = raw.strip()
    parsed = parse_feishu_url(raw)
    if parsed and parsed[0] == "folder":
        return parsed[1]
    if re.fullmatch(r"[A-Za-z0-9]{20,}", raw):
        return raw
    return ""


async def _feishu_doc_create(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """新建 docx 文档：可指定目标文件夹，可同时写入正文。"""
    if not _has_any_scope(ctx, "docx:document"):
        return _FEISHU_AUTH_GUIDE
    title = str(args.get("title") or "").strip() or "未命名文档"
    folder_raw = str(args.get("folder") or "").strip()
    folder_token = ""
    if folder_raw:
        folder_token = _parse_folder_token(folder_raw)
        if not folder_token:
            return "无法识别的文件夹链接（支持 feishu.cn/drive/folder/... 链接或 folder token；不传 folder 则建在我的空间根目录）"
    text = str(args.get("text") or "").strip()
    token_str = await get_feishu_user_token(ctx.db, ctx.user)
    if not token_str:
        return _FEISHU_AUTH_GUIDE
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            doc = await docx_create(client, token_str, title, folder_token)
            if not doc["document_id"]:
                return "新建文档失败：飞书未返回 document_id，请重试"
            n, truncated = 0, False
            empty_body = False
            if text:
                paras, truncated = _md_blocks_capped(text)
                if not paras:
                    empty_body = True
                else:
                    n, _ = await _docx_write_blocks(client, token_str, doc["document_id"], paras)
            _audit_tool(ctx, "feishu.doc_create",
                        {"doc_token": doc["document_id"], "in_folder": bool(folder_token),
                         "paragraphs": n})
    except Exception as e:
        return _feishu_err(e)
    url = f"https://feishu.cn/docx/{doc['document_id']}"
    where = "指定文件夹里" if folder_token else "我的空间根目录"
    if empty_body:
        return (f"已在{where}新建文档《{doc['title']}》：{url}，但正文为空未写入"
                "（转换后没有有效内容块，请用有效 markdown 正文通过 feishu_doc_append 补写）。")
    body_part = f"，已写入 {n} 个内容块（标题/段落/列表项等）" if n else ""
    note = "（正文超上限已截断，剩余部分可用 feishu_doc_append 分次追加）" if truncated else ""
    return f"已在{where}新建文档《{doc['title']}》：{url}{body_part}{note}。请把链接原样发给用户。"


async def _feishu_sheet_create(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """新建电子表格（spreadsheet）：可指定目标文件夹。"""
    if not _has_any_scope(ctx, "sheets:spreadsheet"):
        return _FEISHU_AUTH_GUIDE
    title = str(args.get("title") or "").strip() or "未命名表格"
    folder_raw = str(args.get("folder") or "").strip()
    folder_token = ""
    if folder_raw:
        folder_token = _parse_folder_token(folder_raw)
        if not folder_token:
            return "无法识别的文件夹链接（支持 feishu.cn/drive/folder/... 链接或 folder token；不传 folder 则建在我的空间根目录）"
    token_str = await get_feishu_user_token(ctx.db, ctx.user)
    if not token_str:
        return _FEISHU_AUTH_GUIDE
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            ss = await sheet_create(client, token_str, title, folder_token)
            if not ss["spreadsheet_token"]:
                return "新建电子表格失败：飞书未返回 spreadsheet_token，请重试"
            _audit_tool(ctx, "feishu.sheet_create",
                        {"ss_token": ss["spreadsheet_token"], "in_folder": bool(folder_token)})
    except Exception as e:
        return _feishu_err(e)
    url = ss["url"] or f"https://feishu.cn/sheets/{ss['spreadsheet_token']}"
    where = "指定文件夹里" if folder_token else "我的空间根目录"
    return (f"已在{where}新建电子表格《{ss['title']}》：{url}。请把链接原样发给用户；"
            "往里写数据用 feishu_sheet_write。")


async def _feishu_bitable_create(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """不传 app_token=新建多维表格（base app，自带默认数据表）；传 app_token=在已有多维表格里新建数据表（M32c）。"""
    if not _has_any_scope(ctx, "bitable:app"):
        return _FEISHU_AUTH_GUIDE
    title = str(args.get("title") or "").strip()
    app_raw = str(args.get("app_token") or "").strip()
    if app_raw:
        if not title:
            return "在已有多维表格里新建数据表时，title 是新数据表的名字，不能为空"
        return await _feishu_bitable_create_table(ctx, app_raw, title, args.get("fields"))
    title = title or "未命名多维表格"
    folder_raw = str(args.get("folder") or "").strip()
    folder_token = ""
    if folder_raw:
        folder_token = _parse_folder_token(folder_raw)
        if not folder_token:
            return "无法识别的文件夹链接（支持 feishu.cn/drive/folder/... 链接或 folder token；不传 folder 则建在我的空间根目录）"
    token_str = await get_feishu_user_token(ctx.db, ctx.user)
    if not token_str:
        return _FEISHU_AUTH_GUIDE
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            app = await bitable_create_app(client, token_str, title, folder_token)
            if not app["app_token"]:
                return "新建多维表格失败：飞书未返回 app_token，请重试"
            _audit_tool(ctx, "feishu.bitable_create",
                        {"app_token": app["app_token"], "in_folder": bool(folder_token)})
    except Exception as e:
        return _feishu_err(e)
    url = app["url"] or f"https://feishu.cn/base/{app['app_token']}"
    where = "指定文件夹里" if folder_token else "我的空间根目录"
    table_note = (f"它自带一张默认数据表（table_id：{app['default_table_id']}），" if app["default_table_id"]
                  else "它自带一张默认数据表，")
    return (f"已在{where}新建多维表格《{app['name']}》：{url}。请把链接原样发给用户；"
            f"{table_note}加记录用 feishu_bitable_write（url 传上面的链接）。")


async def _feishu_bitable_create_table(ctx: "ToolContext", app_raw: str, name: str,
                                       fields_raw: Any) -> str:
    """M32c：在已有多维表格（base/wiki 链接或 app token）里新建数据表，可带字段定义一次建列。
    字段离线校验不过 = 不碰网络；仪表盘/视图无官方 API，不在这里做。"""
    field_items: list[dict] | None = None
    if fields_raw is not None:
        if not isinstance(fields_raw, list) or not fields_raw or not all(isinstance(f, dict) for f in fields_raw):
            return ('fields 必须是非空数组：[{"name":"金额","type":"number"}]；type 支持：'
                    + "/".join(dict.fromkeys(_BITABLE_FIELD_TYPES)))
        if len(fields_raw) > _BITABLE_FIELD_ADD_MAX:
            return f"单次建列上限 {_BITABLE_FIELD_ADD_MAX} 个，请分批"
        field_items = []
        for spec in fields_raw:
            r = _bitable_spec_to_payload(spec)
            if isinstance(r, str):
                return r
            fname, tno, prop = r
            item: dict = {"field_name": fname, "type": tno}
            if prop:
                item["property"] = prop
            field_items.append(item)
    token_str = await get_feishu_user_token(ctx.db, ctx.user)
    if not token_str:
        return _FEISHU_AUTH_GUIDE
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            app_token = app_raw
            parsed = parse_feishu_url(app_raw)
            if parsed:
                kind, tok = parsed
                if kind not in ("base", "wiki"):
                    return f"这是 {kind} 类型的链接：app_token 要传多维表格（base/wiki）链接或 app token"
                resolved = await _resolve_doc_target(client, token_str, kind, tok)
                if isinstance(resolved, str):
                    return resolved
                kind, app_token, _ = resolved
                if kind != "base":
                    return f"该知识库节点是{'文档' if kind == 'docx' else '表格'}：app_token 要传多维表格"
            elif not re.fullmatch(r"[A-Za-z0-9]{20,}", app_raw):
                return "无法识别的 app_token（传多维表格链接 feishu.cn/base/... 或 app token）"
            tbl = await bitable_create_table(client, token_str, app_token, name, field_items)
            if not tbl["table_id"]:
                return "新建数据表失败：飞书未返回 table_id，请重试"
            _audit_tool(ctx, "feishu.bitable_create",
                        {"app_token": app_token, "table": name, "kind": "table",
                         "fields": len(field_items or [])})
    except Exception as e:
        return _feishu_err(e)
    cols = f"，含 {len(field_items)} 个字段" if field_items else ""
    return (f"已在多维表格里新建数据表「{name}」（table_id：{tbl['table_id']}）{cols}。"
            f"加记录用 feishu_bitable_write（url 传多维表格链接、table 传「{name}」）。"
            "提醒：仪表盘/图表没有官方接口，需要用户在飞书界面手动配置。")


_DOC_DELETE_TAIL_MAX = 50  # 单次删除末尾段数上限

# 块类型 → 装文本 elements 的字段名（text=2，heading1-9=3-11，bullet=12，ordered=13，quote=15，todo=17 等）
_BLOCK_TEXT_KEYS = ("text", "heading1", "heading2", "heading3", "heading4", "heading5",
                    "heading6", "heading7", "heading8", "heading9", "bullet", "ordered",
                    "quote", "todo")


def _block_text(block: dict) -> str:
    """从 docx 块里提取纯文本（删除确认预览用）；非文本类块（图片/表格等）返回空。"""
    for key in _BLOCK_TEXT_KEYS:
        node = block.get(key)
        if isinstance(node, dict):
            return "".join(e.get("text_run", {}).get("content", "")
                           for e in node.get("elements") or [] if isinstance(e, dict))
    return ""


async def _feishu_doc_delete_tail(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """删除 docx 文末最后 N 个内容块（撤销追加/删减结尾场景；不可恢复）。"""
    parsed = parse_feishu_url(str(args.get("url") or ""))
    if not parsed:
        return "无法识别的飞书链接（支持 docx/wiki 文档链接）"
    kind, token = parsed
    if kind not in ("docx", "wiki"):
        return f"这是 {kind} 类型的链接：feishu_doc_delete_tail 只支持文档"
    if not _has_any_scope(ctx, "docx:document"):
        return _FEISHU_AUTH_GUIDE
    try:
        count = int(args.get("count") or 1)
    except (TypeError, ValueError):
        return "count 必须是整数（要删除的末尾段数）"
    if not 1 <= count <= _DOC_DELETE_TAIL_MAX:
        return f"count 范围 1~{_DOC_DELETE_TAIL_MAX}（单次最多删 {_DOC_DELETE_TAIL_MAX} 段）"
    token_str = await get_feishu_user_token(ctx.db, ctx.user)
    if not token_str:
        return _FEISHU_AUTH_GUIDE
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resolved = await _resolve_doc_target(client, token_str, kind, token)
            if isinstance(resolved, str):
                return resolved
            kind, token, title = resolved
            if kind != "docx":
                return f"该知识库节点是{'表格' if kind == 'sheet' else '多维表格'}：feishu_doc_delete_tail 只支持文档"
            blocks = await docx_block_children(client, token_str, token)
            if not blocks:
                return "文档没有任何内容块，无需删除"
            if count > len(blocks):
                return (f"文档一共只有 {len(blocks)} 个内容块，要删的 {count} 段超过总数。"
                        "为避免误删整篇，请先用 feishu_doc_read 确认内容后调整 count")
            start = len(blocks) - count
            doomed = blocks[start:]
            await docx_delete_children(client, token_str, token, start, len(blocks))
            previews = [t for t in (_block_text(b) for b in doomed) if t]
            _audit_tool(ctx, "feishu.doc_delete", {"doc_token": token, "count": count})
    except Exception as e:
        return _feishu_err(e)
    head = f"《{title}》" if title else "文档"
    preview = "；".join(p[:30] for p in previews[:3])
    preview_part = f"，删除内容开头：{preview}…" if preview else ""
    return (f"已删除{head}文末 {count} 段{preview_part}。"
            "删除不可恢复，请向用户说明删掉了什么。")


# docx 块类型 → 中文名（feishu_doc_edit list 动作用；未收录显示「类型N」）
_DOCX_BLOCK_TYPE_NAMES = {
    2: "文本", 3: "标题1", 4: "标题2", 5: "标题3", 6: "标题4", 7: "标题5",
    8: "标题6", 9: "标题7", 10: "标题8", 11: "标题9", 12: "无序列表", 13: "有序列表",
    14: "代码块", 15: "引用", 17: "待办", 22: "分割线",
}
_DOC_EDIT_LIST_MAX = 300  # list 动作最多展示的块数


async def _feishu_doc_edit(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """docx 段落级编辑：list 看块序号 / replace 先删后插 / insert 指定位置插入 / delete 删任意区间。"""
    parsed = parse_feishu_url(str(args.get("url") or ""))
    if not parsed:
        return "无法识别的飞书链接（支持 docx/wiki 文档链接）"
    kind, token = parsed
    if kind not in ("docx", "wiki"):
        return f"这是 {kind} 类型的链接：feishu_doc_edit 只支持文档（表格用 feishu_sheet_write、多维表格用 feishu_bitable_write/feishu_bitable_field）"
    if not _has_any_scope(ctx, "docx:document"):
        return _FEISHU_AUTH_GUIDE
    action = str(args.get("action") or "list").strip()
    if action not in ("list", "replace", "insert", "delete"):
        return "action 只支持 list（列出带序号的内容块）/ replace（替换某块）/ insert（在某块前插入）/ delete（删除某块起 N 块）"
    try:
        index = int(args.get("index")) if args.get("index") is not None else None
    except (TypeError, ValueError):
        return "index 必须是整数（内容块序号，先用 action=list 查看）"
    if action != "list" and index is None:
        return f"action={action} 必须传 index（内容块序号，先用 action=list 查看）"
    try:
        count = int(args.get("count") or 1)
    except (TypeError, ValueError):
        return "count 必须是整数（从 index 起删除的块数）"
    if action == "delete" and not 1 <= count <= _DOC_DELETE_TAIL_MAX:
        return f"count 范围 1~{_DOC_DELETE_TAIL_MAX}（单次最多删 {_DOC_DELETE_TAIL_MAX} 块）"
    # replace/insert 的 markdown 先离线转换：转换失败/为空 = 不碰文档
    blocks_new: list[dict] = []
    truncated = False
    if action in ("replace", "insert"):
        text = str(args.get("text") or "").strip()
        if not text:
            return f"action={action} 必须传 text（markdown 正文，写法同 feishu_doc_append）"
        blocks_new, truncated = _md_blocks_capped(text)
        if not blocks_new:
            return "内容为空：请给出有效正文（当前内容转换后没有任何可写入的块）"
    token_str = await get_feishu_user_token(ctx.db, ctx.user)
    if not token_str:
        return _FEISHU_AUTH_GUIDE
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resolved = await _resolve_doc_target(client, token_str, kind, token)
            if isinstance(resolved, str):
                return resolved
            kind, token, title = resolved
            if kind != "docx":
                return f"该知识库节点是{'表格' if kind == 'sheet' else '多维表格'}：feishu_doc_edit 只支持文档"
            blocks = await docx_block_children(client, token_str, token)
            total = len(blocks)
            if action == "list":
                _audit_tool(ctx, "feishu.doc_edit",
                            {"doc_token": token, "action": "list", "count": total})
            elif action == "insert":
                if not 0 <= index <= total:
                    return f"index 范围 0~{total}（当前共 {total} 块，index={total} 即插入到文末）"
                n, _ = await _docx_write_blocks(client, token_str, token, blocks_new, index=index)
                _audit_tool(ctx, "feishu.doc_edit",
                            {"doc_token": token, "action": "insert", "index": index, "blocks": n})
            elif action == "delete":
                if not 0 <= index < total:
                    return f"index 范围 0~{total - 1}（当前共 {total} 块）"
                if index + count > total:
                    return f"从 {index} 起删 {count} 块超出文档末尾（共 {total} 块），请缩小 count"
                previews = [t for t in (_block_text(b) for b in blocks[index:index + count]) if t]
                await docx_delete_children(client, token_str, token, index, index + count)
                _audit_tool(ctx, "feishu.doc_edit",
                            {"doc_token": token, "action": "delete", "index": index, "count": count})
            else:  # replace：先删后插；快照原文，插失败尝试恢复
                if not 0 <= index < total:
                    return f"index 范围 0~{total - 1}（当前共 {total} 块）"
                old_text = _block_text(blocks[index])
                await docx_delete_children(client, token_str, token, index, index + 1)
                try:
                    n, _ = await _docx_write_blocks(client, token_str, token, blocks_new, index=index)
                except Exception as ins_err:
                    restored = False
                    if old_text:
                        try:
                            await docx_append_blocks(client, token_str, token,
                                                     _md_to_blocks(old_text) or [], index=index)
                            restored = True
                        except Exception:
                            pass
                    if restored:
                        return (f"替换失败（{_feishu_err(ins_err)}），已把原内容恢复原位。请换更简单的写法重试。")
                    return (f"替换失败且恢复原内容也失败（{_feishu_err(ins_err)}）。"
                            f"第 {index} 块已被删除，原文是：{old_text or '（非文本块，无文字内容）'}——"
                            "请用 feishu_doc_edit insert 把原文插回该位置。")
                _audit_tool(ctx, "feishu.doc_edit",
                            {"doc_token": token, "action": "replace", "index": index, "blocks": n})
    except Exception as e:
        return _feishu_err(e)
    head = f"《{title}》" if title else "文档"
    if action == "list":
        if not blocks:
            return f"{head}没有任何内容块。"
        shown = blocks[:_DOC_EDIT_LIST_MAX]
        lines = []
        for i, b in enumerate(shown):
            tname = _DOCX_BLOCK_TYPE_NAMES.get(b.get("block_type")) or f"类型{b.get('block_type')}"
            preview = _block_text(b)[:60]
            lines.append(f"[{i}] {tname} | {preview}" if preview else f"[{i}] {tname}")
        more = f"\n…仅显示前 {_DOC_EDIT_LIST_MAX} 块（共 {total} 块）" if total > _DOC_EDIT_LIST_MAX else ""
        return (f"{head}共 {total} 个内容块（序号从 0 起）：\n" + "\n".join(lines) + more
                + "\n用 replace/insert/delete 时传这里的序号；编辑后序号会移位，多步编辑请从后往前做或每步重新 list。")
    note = "（内容超上限已截断，剩余部分可分次 insert）" if truncated else ""
    if action == "insert":
        return f"已在{head}第 {index} 块前插入 {n} 个内容块{note}。"
    if action == "delete":
        preview = "；".join(p[:30] for p in previews[:3])
        preview_part = f"，删除内容开头：{preview}…" if preview else ""
        return f"已删除{head}第 {index} 块起 {count} 块{preview_part}。删除不可恢复，请向用户说明删掉了什么。"
    old_part = f"（原块内容：{old_text[:40]}…）" if old_text else ""
    return f"已替换{head}第 {index} 块{old_part}为 {n} 个内容块{note}。"


async def _feishu_sheet_write(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """表格写入：mode=update 写指定范围 / mode=append 末行追加。"""
    parsed = parse_feishu_url(str(args.get("url") or ""))
    if not parsed:
        return "无法识别的飞书链接（支持 sheets/wiki 表格链接）"
    kind, token = parsed
    if kind not in ("sheet", "wiki"):
        return f"这是 {kind} 类型的链接：feishu_sheet_write 只支持表格（文档用 feishu_doc_append、多维表格用 feishu_bitable_write）"
    if not _has_any_scope(ctx, "sheets:spreadsheet"):
        return _FEISHU_AUTH_GUIDE
    mode = str(args.get("mode") or "update").strip()
    if mode not in ("update", "append"):
        return "mode 只支持 update（写指定范围）/ append（末行追加）"
    values = args.get("values")
    if not isinstance(values, list) or not values or not all(isinstance(r, list) for r in values):
        return 'values 必须是非空二维数组，如 [["姓名","数量"],["小龙虾",3]]'
    if len(values) > _SHEET_WRITE_ROWS or any(len(r) > _SHEET_WRITE_COLS for r in values):
        return f"单次写入上限 {_SHEET_WRITE_ROWS} 行 × {_SHEET_WRITE_COLS} 列，请分批写入"
    rng = str(args.get("range") or "").strip()
    if mode == "update" and not rng:
        return "mode=update 必须传 range（如 A1:B2；不带 sheetId 前缀时默认第一个 sheet 页）"
    token_str = await get_feishu_user_token(ctx.db, ctx.user)
    if not token_str:
        return _FEISHU_AUTH_GUIDE
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resolved = await _resolve_doc_target(client, token_str, kind, token)
            if isinstance(resolved, str):
                return resolved
            kind, token, title = resolved
            if kind != "sheet":
                return f"该知识库节点是{'文档' if kind == 'docx' else '多维表格'}：请改用对应的写入工具"
            if "!" not in rng:
                sheets = await sheet_meta(client, token_str, token)
                if not sheets:
                    return "该表格没有任何 sheet 页（或无权限）"
                rng = f"{sheets[0]['sheet_id']}!{rng or 'A1'}"
            if mode == "append":
                await sheet_values_append(client, token_str, token, rng, values)
            else:
                await sheet_values_update(client, token_str, token, rng, values)
            cols = max(len(r) for r in values)
            _audit_tool(ctx, "feishu.sheet_write",
                        {"doc_token": token, "mode": mode, "rows": len(values), "cols": cols})
    except Exception as e:
        return _feishu_err(e)
    head = f"《{title}》" if title else "表格"
    act = "末行追加" if mode == "append" else "写入范围"
    return f"已在{head}{act} {len(values)} 行（{rng}）。请向用户确认写入结果。"


async def _feishu_bitable_write(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """多维表格写入：mode=add 新增记录 / mode=update 按 record_id 改记录。"""
    parsed = parse_feishu_url(str(args.get("url") or ""))
    if not parsed:
        return "无法识别的飞书链接（支持 base/wiki 多维表格链接）"
    kind, token = parsed
    if kind not in ("base", "wiki"):
        return f"这是 {kind} 类型的链接：feishu_bitable_write 只支持多维表格（文档用 feishu_doc_append、表格用 feishu_sheet_write）"
    if not _has_any_scope(ctx, "bitable:app"):
        return _FEISHU_AUTH_GUIDE
    mode = str(args.get("mode") or "add").strip()
    if mode not in ("add", "update", "delete"):
        return "mode 只支持 add（新增记录）/ update（按 record_id 改记录）/ delete（按 record_id 删记录）"
    records = args.get("records")
    if not isinstance(records, list) or not records or not all(isinstance(r, dict) for r in records):
        return ('records 必须是非空数组，每条是字段字典，如 [{"标题":"合同A","金额":5000}]；'
                'mode=update/delete 时每条需带 record_id')
    token_str = await get_feishu_user_token(ctx.db, ctx.user)
    if not token_str:
        return _FEISHU_AUTH_GUIDE
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resolved = await _resolve_doc_target(client, token_str, kind, token)
            if isinstance(resolved, str):
                return resolved
            kind, token, title = resolved
            if kind != "base":
                return f"该知识库节点是{'文档' if kind == 'docx' else '表格'}：请改用对应的写入工具"
            tables = await bitable_tables(client, token_str, token)
            if not tables:
                return "该多维表格没有任何数据表（或无权限）"
            want = str(args.get("table") or "").strip()
            cur = next((t for t in tables if want and want in (t["name"], t["table_id"])), tables[0])
            if mode == "add":
                if len(records) > _BITABLE_ADD_MAX:
                    return f"单次新增上限 {_BITABLE_ADD_MAX} 条，请分批"
                n = await bitable_add_records(client, token_str, token, cur["table_id"], records)
            elif mode == "delete":
                ids = [str(r.get("record_id") or "") for r in records]
                if any(not i for i in ids):
                    return "mode=delete 时每条记录必须带 record_id（先 feishu_doc_read 读该表，每行末尾括号里有 record_id）"
                if len(records) > _BITABLE_DEL_MAX:
                    return f"单次删除上限 {_BITABLE_DEL_MAX} 条，请分批"
                n = await bitable_delete_records(client, token_str, token, cur["table_id"], ids)
            else:
                ids = [str(r.get("record_id") or "") for r in records]
                if any(not i for i in ids):
                    return "mode=update 时每条记录必须带 record_id（先 feishu_doc_read 读该表，每行末尾括号里有 record_id）"
                if len(records) > _BITABLE_UPD_MAX:
                    return f"单次更新上限 {_BITABLE_UPD_MAX} 条，请分批"
                for rid, r in zip(ids, records):
                    fields = {k: v for k, v in r.items() if k != "record_id"}
                    await bitable_update_record(client, token_str, token, cur["table_id"], rid, fields)
                n = len(records)
            _audit_tool(ctx, "feishu.bitable_write",
                        {"doc_token": token, "table": cur["name"], "mode": mode, "count": n})
    except Exception as e:
        return _feishu_err(e)
    head = f"《{title}》" if title else "多维表格"
    act = {"add": "新增", "update": "更新", "delete": "删除"}[mode]
    return f"已在{head}的「{cur['name']}」表{act} {n} 条记录。请向用户确认结果。"


# 多维表格字段类型：别名（英文/中文）→ 飞书类型编号
_BITABLE_FIELD_TYPES = {
    "text": 1, "文本": 1,
    "number": 2, "数字": 2,
    "single_select": 3, "单选": 3,
    "multi_select": 4, "多选": 4,
    "date": 5, "日期": 5,
    "checkbox": 7, "勾选": 7,
    "phone": 13, "电话": 13,
    "url": 15, "超链接": 15,
    "attachment": 17, "附件": 17,
    "link": 18, "关联": 18, "单向关联": 18,
    "duplex_link": 21, "双向关联": 21,
}
_BITABLE_FIELD_TYPE_NAMES = {  # list 动作用：编号 → 中文名（未收录的显示编号）
    1: "文本", 2: "数字", 3: "单选", 4: "多选", 5: "日期", 7: "勾选", 11: "人员",
    13: "电话", 15: "超链接", 17: "附件", 18: "关联", 19: "查找引用", 20: "公式",
    21: "双向关联", 22: "地理位置", 23: "群聊", 1001: "创建时间", 1002: "修改时间",
    1003: "创建人", 1004: "修改人", 1005: "自动编号",
}
_BITABLE_FIELD_ADD_MAX = 20  # 单次建/删列上限


def _bitable_spec_to_payload(spec: dict) -> tuple[str, int, dict] | str:
    """字段定义 spec → (name, 类型编号, property)；不合法返回错误文案（离线校验，不过=一列都不建）。
    单选/多选必须带 options；关联类(18/21)必须带 table_id（被关联数据表），可选 multiple。"""
    name_ = str(spec.get("name") or "").strip()
    tkey = str(spec.get("type") or "").strip()
    if not name_ or tkey not in _BITABLE_FIELD_TYPES:
        return (f"字段定义无效：{spec}（name 不能为空；type 只支持 "
                + "/".join(dict.fromkeys(_BITABLE_FIELD_TYPES)) + "）")
    tno = _BITABLE_FIELD_TYPES[tkey]
    prop: dict = {}
    if tno in (3, 4):
        opts = spec.get("options")
        if not isinstance(opts, list) or not opts or not all(isinstance(o, str) and o.strip() for o in opts):
            return f"单选/多选字段「{name_}」必须带 options 选项名数组"
        prop["options"] = [{"name": o.strip()} for o in opts]
    elif tno == 5:
        prop["date_formatter"] = "yyyy/MM/dd"
    elif tno in (18, 21):
        tid = str(spec.get("table_id") or "").strip()
        if not tid:
            return (f"关联字段「{name_}」必须带 table_id（被关联数据表的 table_id；"
                    "先用 feishu_bitable_field action=list 看对方表的字段，或从飞书界面复制）")
        prop["table_id"] = tid
        if isinstance(spec.get("multiple"), bool):
            prop["multiple"] = spec["multiple"]
    return name_, tno, prop


async def _feishu_bitable_field(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """多维表格字段（列）：action=list 看结构 / action=add 批量建列。"""
    parsed = parse_feishu_url(str(args.get("url") or ""))
    if not parsed:
        return "无法识别的飞书链接（支持 base/wiki 多维表格链接）"
    kind, token = parsed
    if kind not in ("base", "wiki"):
        return f"这是 {kind} 类型的链接：feishu_bitable_field 只支持多维表格"
    if not _has_any_scope(ctx, "bitable:app"):
        return _FEISHU_AUTH_GUIDE
    action = str(args.get("action") or "list").strip()
    if action not in ("list", "add", "delete", "update"):
        return "action 只支持 list（查看字段）/ add（新建字段）/ update（改列名/类型）/ delete（删除字段）"
    # add 动作先离线校验字段定义、生成执行计划：校验不过 = 一列都不建，也不碰网络
    plan: list[tuple[str, int, dict]] = []
    if action == "add":
        specs = args.get("fields")
        if not isinstance(specs, list) or not specs or not all(isinstance(f, dict) for f in specs):
            return ('fields 必须是非空数组，如 [{"name":"金额","type":"number"},'
                    '{"name":"状态","type":"单选","options":["进行中","已完成"]}]；'
                    "type 支持：" + "/".join(dict.fromkeys(_BITABLE_FIELD_TYPES)))
        if len(specs) > _BITABLE_FIELD_ADD_MAX:
            return f"单次建列上限 {_BITABLE_FIELD_ADD_MAX} 个，请分批"
        for spec in specs:
            r = _bitable_spec_to_payload(spec)
            if isinstance(r, str):
                return r
            plan.append(r)
    del_names: list[str] = []
    if action == "delete":
        raw = args.get("fields")
        if not isinstance(raw, list) or not raw or not all(isinstance(f, str) and f.strip() for f in raw):
            return 'action=delete 时 fields 传要删的列名（或 field_id）数组，如 ["备注","占位列"]'
        if len(raw) > _BITABLE_FIELD_ADD_MAX:
            return f"单次删列上限 {_BITABLE_FIELD_ADD_MAX} 个，请分批"
        del_names = [f.strip() for f in raw]
    upd_plan: list[tuple[str, str, int, dict]] = []  # (现列名/field_id, 新名, 新类型编号, property)
    if action == "update":
        specs = args.get("fields")
        if not isinstance(specs, list) or not specs or not all(isinstance(f, dict) for f in specs):
            return ('action=update 时 fields 传改动数组，如 [{"name":"文本","new_name":"门店名称"},'
                    '{"name":"状态","type":"多选","options":["进行中","已完成","已逾期"]}]；'
                    'name 指现列名或 field_id，new_name/type 至少给一个')
        if len(specs) > _BITABLE_FIELD_ADD_MAX:
            return f"单次改列上限 {_BITABLE_FIELD_ADD_MAX} 个，请分批"
        for spec in specs:
            key = str(spec.get("name") or "").strip()
            new_name = str(spec.get("new_name") or "").strip()
            tkey = str(spec.get("type") or "").strip()
            if not key:
                return f"字段定义无效：{spec}（name 指现列名或 field_id，不能为空）"
            if not new_name and not tkey:
                return f"字段「{key}」没说要改什么：new_name（新列名）和 type（新类型）至少给一个"
            tno = 0
            prop: dict = {}
            if tkey:
                if tkey not in _BITABLE_FIELD_TYPES:
                    return (f"字段「{key}」的 type 无效：{tkey}（只支持 "
                            + "/".join(dict.fromkeys(_BITABLE_FIELD_TYPES)) + "）")
                tno = _BITABLE_FIELD_TYPES[tkey]
                if tno in (3, 4):
                    opts = spec.get("options")
                    if not isinstance(opts, list) or not opts or not all(isinstance(o, str) and o.strip() for o in opts):
                        return f"单选/多选字段「{key}」必须带 options 选项名数组"
                    prop["options"] = [{"name": o.strip()} for o in opts]
                elif tno == 5:
                    prop["date_formatter"] = "yyyy/MM/dd"
                elif tno in (18, 21):
                    tid = str(spec.get("table_id") or "").strip()
                    if not tid:
                        return f"关联字段「{key}」改类型时必须带 table_id（被关联数据表的 table_id）"
                    prop["table_id"] = tid
                    if isinstance(spec.get("multiple"), bool):
                        prop["multiple"] = spec["multiple"]
            upd_plan.append((key, new_name, tno, prop))
    token_str = await get_feishu_user_token(ctx.db, ctx.user)
    if not token_str:
        return _FEISHU_AUTH_GUIDE
    added: list[str] = []
    fname = ""
    fields: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resolved = await _resolve_doc_target(client, token_str, kind, token)
            if isinstance(resolved, str):
                return resolved
            kind, token, title = resolved
            if kind != "base":
                return f"该知识库节点是{'文档' if kind == 'docx' else '表格'}：请改用对应的工具"
            tables = await bitable_tables(client, token_str, token)
            if not tables:
                return "该多维表格没有任何数据表（或无权限）"
            want = str(args.get("table") or "").strip()
            cur = next((t for t in tables if want and want in (t["name"], t["table_id"])), tables[0])
            if action == "list":
                fields = await bitable_fields(client, token_str, token, cur["table_id"])
                _audit_tool(ctx, "feishu.bitable_field",
                            {"doc_token": token, "table": cur["name"], "action": "list",
                             "count": len(fields)})
            elif action == "delete":
                cur_fields = await bitable_fields(client, token_str, token, cur["table_id"])
                id_of = {f["name"]: f["field_id"] for f in cur_fields}
                id_of.update({f["field_id"]: f["field_id"] for f in cur_fields})  # 也接受 field_id
                unknown = [n for n in del_names if n not in id_of]
                if unknown:
                    existing = [f["name"] for f in cur_fields]
                    return f"找不到这些列：{unknown}（「{cur['name']}」表现有列：{existing}）。未删除任何列。"
                for fname in del_names:
                    await bitable_delete_field(client, token_str, token, cur["table_id"], id_of[fname])
                    added.append(fname)
                _audit_tool(ctx, "feishu.bitable_field",
                            {"doc_token": token, "table": cur["name"], "action": "delete",
                             "count": len(added)})
            elif action == "update":
                cur_fields = await bitable_fields(client, token_str, token, cur["table_id"])
                id_of = {f["name"]: f["field_id"] for f in cur_fields}
                id_of.update({f["field_id"]: f["field_id"] for f in cur_fields})
                unknown = [k for k, _, _, _ in upd_plan if k not in id_of]
                if unknown:
                    existing = [f["name"] for f in cur_fields]
                    return f"找不到这些列：{unknown}（「{cur['name']}」表现有列：{existing}）。未改动任何列。"
                for key, new_name, tno, prop in upd_plan:
                    fname = new_name or key
                    await bitable_update_field(client, token_str, token, cur["table_id"],
                                               id_of[key], new_name, tno, prop)
                    added.append(fname)
                _audit_tool(ctx, "feishu.bitable_field",
                            {"doc_token": token, "table": cur["name"], "action": "update",
                             "count": len(added)})
            else:
                for fname, tno, prop in plan:
                    await bitable_add_field(client, token_str, token, cur["table_id"], fname, tno, prop)
                    added.append(fname)
                _audit_tool(ctx, "feishu.bitable_field",
                            {"doc_token": token, "table": cur["name"], "action": "add",
                             "count": len(added)})
    except Exception as e:
        if added and action in ("add", "delete", "update"):
            verb = {"add": "建", "delete": "删", "update": "改"}[action]
            return f"{verb}列中途失败：已{verb} {len(added)} 列 {added}，「{fname}」报错：{_feishu_err(e)}"
        return _feishu_err(e)
    head = f"《{title}》" if title else "多维表格"
    if action == "list":
        if not fields:
            return f"{head}的「{cur['name']}」表还没有任何字段。"
        lines = []
        for f in fields:
            tname = _BITABLE_FIELD_TYPE_NAMES.get(f["type"]) or f"类型{f['type']}"
            line = f"- {f['name']}（{tname}）"
            if f.get("options"):
                line += f"：选项 [{'、'.join(f['options'])}]"
            lines.append(line)
        return f"{head}的「{cur['name']}」表现有 {len(fields)} 列：\n" + "\n".join(lines)
    if action == "delete":
        return f"已在{head}的「{cur['name']}」表删除 {len(added)} 列：{'、'.join(added)}。"
    if action == "update":
        return f"已在{head}的「{cur['name']}」表修改 {len(added)} 列：{'、'.join(added)}。"
    return f"已在{head}的「{cur['name']}」表新建 {len(added)} 列：{'、'.join(added)}。现在可以用 feishu_bitable_write 写记录了。"


async def _feishu_folder_list(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """列云文件夹一级子项（名称+类型+可用链接），M10a。"""
    folder_token = _parse_folder_token(str(args.get("url") or ""))
    if not folder_token:
        return "无法识别的文件夹链接（支持 feishu.cn/drive/folder/... 或 feishu.cn/folder/... 链接）"
    if not _has_any_scope(ctx, "drive:drive:readonly", "drive:drive"):
        return _FEISHU_AUTH_GUIDE
    token_str = await get_feishu_user_token(ctx.db, ctx.user)
    if not token_str:
        return _FEISHU_AUTH_GUIDE
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            items = await drive_list_folder(client, token_str, folder_token)
    except Exception as e:
        return _feishu_err(e)
    _audit_tool(ctx, "feishu.folder_list", {"folder_token": folder_token, "items": len(items)})
    if not items:
        return "文件夹是空的（或无权限查看其中内容）"
    _TYPE_URL = {"docx": "docx", "doc": "docx", "sheet": "sheets", "bitable": "base",
                 "file": "file", "folder": "drive/folder"}
    _TYPE_CN = {"docx": "文档", "doc": "旧版文档", "sheet": "表格", "bitable": "多维表格",
                "file": "文件", "folder": "文件夹", "mindnote": "思维笔记", "shortcut": "快捷方式"}
    lines = []
    for it in items:
        t = it["type"]
        label = _TYPE_CN.get(t, t or "未知")
        path = _TYPE_URL.get(t)
        link = f"https://feishu.cn/{path}/{it['token']}" if path else ""
        lines.append(f"- [{label}] {it['name']}" + (f"\n  {link}" if link else ""))
    out = (f"文件夹共 {len(items)} 项：\n\n" + "\n".join(lines)
           + "\n\n表格/多维表格/文档可直接用 feishu_doc_read 读；子文件夹用 feishu_folder_list 继续展开。")
    if len(out) > _FEISHU_TEXT_MAX:
        out = out[:_FEISHU_TEXT_MAX] + "\n…[条目过多已截断]"
    return out


async def _feishu_file_upload(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """把工作区文件上传到飞书云盘文件夹（M29，upload_all ≤20MB）。"""
    path = (args.get("path") or "").strip().lstrip("/")
    if not path:
        return "path 不能为空（工作区相对路径，先 workspace_list 确认）"
    if not _valid_ws_path(path):
        return _INVALID_PATH_HINT
    root = _workspace_root(ctx.user.id, ctx.agent.id).resolve()
    target = (root / path).resolve()
    if root not in target.parents and target != root:
        return "路径越界，已拒绝"
    if not target.is_file():
        return f"工作区里没找到「{path}」（先 workspace_list 确认路径；文件夹不能整体上传，请逐个文件传）"
    if not _has_any_scope(ctx, "drive:file:upload", "drive:drive"):
        return _FEISHU_AUTH_GUIDE
    size = target.stat().st_size
    if size > _FEISHU_UPLOAD_MAX:
        return (f"文件 {size / 1024 / 1024:.1f}MB 超过单次上传上限 20MB——"
                "请用户在工作区下载后手动传到飞书（或拆分文件）")
    folder_raw = str(args.get("folder") or "").strip()
    folder_token = ""
    if folder_raw:
        folder_token = _parse_folder_token(folder_raw)
        if not folder_token:
            return "无法识别的文件夹链接（支持 feishu.cn/drive/folder/... 或 feishu.cn/folder/... 链接）"
    name = (args.get("name") or "").strip() or target.name
    token_str = await get_feishu_user_token(ctx.db, ctx.user)
    if not token_str:
        return _FEISHU_AUTH_GUIDE
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            if not folder_token:
                # M29.1：root_folder/meta 只认 drive:drive / drive:drive.metadata:readonly（99991679 实测
                # drive:drive:readonly 不覆盖）——老 token 没这 scope 时直接引导，不白打一次 API
                if not _has_any_scope(ctx, "drive:drive.metadata:readonly", "drive:drive"):
                    return ("不传 folder 时我需要先定位「我的空间」根目录，这需要云盘元数据只读授权、本次授权未包含。"
                            "两个办法：①用户重新授权（[飞书数据授权](/login?upgrade=1)）后重试；"
                            "②直接给我目标文件夹链接，我传到指定文件夹（不受此限）。请把选择原样转述给用户。")
                folder_token = await drive_root_folder(client, token_str)
                if not folder_token:
                    return "拿不到「我的空间」根目录信息——请改传目标文件夹链接（folder 参数）再试"
            data = await drive_upload_file(
                client, token_str, folder_token, name, target.read_bytes(),
                mimetypes.guess_type(name)[0] or "",
            )
    except Exception as e:
        return _feishu_err(e)
    file_token = data.get("file_token") or ""
    _audit_tool(ctx, "feishu.file_upload",
                {"path": path, "folder_token": folder_token, "name": name, "size": size})
    link = f"\n链接：https://feishu.cn/file/{file_token}" if file_token else ""
    return f"已上传到飞书云盘：{name}（{size / 1024:.1f}KB）{link}（链接须原样发给用户）"


async def _feishu_doc_search(ctx: "ToolContext", args: dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "query 不能为空"
    count = args.get("count") or 5
    count = max(1, min(int(count), 10))
    token_str = await get_feishu_user_token(ctx.db, ctx.user)
    if not token_str:
        return _FEISHU_AUTH_GUIDE
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            hits = await search_docs(client, token_str, query, count)
    except Exception as e:
        return _feishu_err(e)
    _audit_tool(ctx, "feishu.doc_search", {"query": query, "hits": len(hits)})
    if not hits:
        return f"没有搜到与「{query}」相关的飞书文档（换个关键词试试）"
    lines = [f"{i}. {h['title'] or '(无标题)'} [{h['obj_type']}]\n   {h['url']}" for i, h in enumerate(hits, 1)]
    return "搜索结果（用 feishu_doc_read 读正文）：\n" + "\n".join(lines)


async def _feishu_p2p_peers(client: httpx.AsyncClient, token: str, self_id: str, chats: list[dict]) -> dict[str, str]:
    """p2p 会话 → 对方姓名（chat_id → name）。逐个 members 去掉自己再 contact 批量解析；
    任何一步失败静默降级（该会话显示「私聊」无姓名）。解析数量受 _FEISHU_P2P_LOOKUP_MAX 限制。"""
    p2p = [c for c in chats if c.get("chat_mode") == "p2p"][:_FEISHU_P2P_LOOKUP_MAX]
    if not p2p:
        return {}
    peer_ids: dict[str, str] = {}  # chat_id → 对方 open_id
    for c in p2p:
        try:
            members = await chat_members(client, token, c["chat_id"])
        except Exception:
            continue
        others = [m for m in members if m != self_id and m.startswith("ou_")]
        if others:
            peer_ids[c["chat_id"]] = others[0]
    if not peer_ids:
        return {}
    try:
        names = await contact_user_names(client, token, sorted(set(peer_ids.values())))
    except Exception:
        return {}
    return {cid: names.get(oid, "") for cid, oid in peer_ids.items() if names.get(oid)}


async def _feishu_p2p_peer_name(client: httpx.AsyncClient, token: str, chat_id: str, self_id: str) -> str:
    """单个 p2p 会话 → 对方姓名（M8b：daily 搜到的私聊不在 /im/v1/chats 列表里，单独解析）。
    members 去掉自己 → contact 解析；机器人类会话 members 只有自己 → 返回 ''（标签退化为「私聊」）。"""
    try:
        members = await chat_members(client, token, chat_id)
        others = [m for m in members if m != self_id and m.startswith("ou_")]
        if not others:
            return ""
        names = await contact_user_names(client, token, others[:1])
        return names.get(others[0], "")
    except Exception:
        return ""


def _feishu_chat_label(chat: dict, peers: dict[str, str]) -> str:
    """会话显示名：群用群名；p2p 用「私聊·对方姓名」（解析不出姓名则「私聊」）。"""
    if chat.get("chat_mode") == "p2p":
        peer = peers.get(chat["chat_id"])
        return f"私聊·{peer}" if peer else f"私聊（{chat['chat_id'][-6:]}）"
    return chat.get("name") or chat["chat_id"]


async def _try_get_message(client: httpx.AsyncClient, token: str, message_id: str) -> dict | None:
    """定向深扫的批量取消息：单条失败（权限边界/已撤回等）返回 None，不拖垮整批。"""
    try:
        return await get_message(client, token, message_id)
    except Exception:
        return None


async def _feishu_roster_load(db, user_id: int) -> dict[str, str]:
    """M28.4 私聊名册：该用户发现过的 p2p 会话 chat_id → 对方姓名（未软删）。"""
    rows = (await db.execute(
        select(FeishuP2pChat).where(FeishuP2pChat.user_id == user_id, FeishuP2pChat.deleted_at.is_(None))
    )).scalars().all()
    return {r.chat_id: r.peer_name for r in rows if r.peer_name}


async def _feishu_roster_upsert(db, user_id: int, mapping: dict[str, str]) -> None:
    """M28.4：把新解析出的 p2p 会话写进名册（存在则按需更新姓名）。不 commit，随会话事务落库。"""
    items = [(cid, n) for cid, n in mapping.items() if cid and n]
    if not items:
        return
    rows = (await db.execute(
        select(FeishuP2pChat).where(
            FeishuP2pChat.user_id == user_id,
            FeishuP2pChat.chat_id.in_([cid for cid, _ in items]),
            FeishuP2pChat.deleted_at.is_(None),
        )
    )).scalars().all()
    existing = {r.chat_id: r for r in rows}
    for cid, name in items:
        row = existing.get(cid)
        if row is None:
            db.add(FeishuP2pChat(user_id=user_id, chat_id=cid, peer_name=name))
        elif row.peer_name != name:
            row.peer_name = name


async def _feishu_recent_p2p(client: httpx.AsyncClient, token: str, self_id: str, days: int = 30,
                             target: str = "", start_ts: int = 0, end_ts: int = 0,
                             meta: dict | None = None) -> dict[str, str]:
    """近 N 天有消息的 p2p 会话 → {chat_id: 对方姓名}（M8b）。
    /im/v1/chats 列表不含 p2p（2026-08-28 实测），只能靠消息搜索发现私聊再逐个解析对方姓名。
    target 非空 = M28.1 定向深扫（按人名找人）：逐页扫描、命中即停、窗口放宽到 1200 条消息
    （群消息量大时前 100 条只覆盖几小时，昨天私聊过的人会掉出窗口，2026-09-22 实测）。
    M28.3：start_ts>0 时深扫窗口改为 [start_ts, end_ts or now]——高密度用户条数窗口只覆盖几小时，
    按时间窗扫（模型把「昨晚8点」换算成起止时间）才能定到几天前的私聊；
    meta 非 None 时回填 pages/resolved/earliest_ts（扫到消息的最早时间），供未命中覆盖报告。"""
    if target:
        target = target.lower()
        out: dict[str, str] = {}
        seen: set[str] = set()
        resolved = 0
        page_token = ""
        lo = start_ts or int(time.time()) - days * 86400
        hi = end_ts or int(time.time())
        earliest_ts = 0
        pages = 0
        try:
            for _ in range(_FEISHU_P2P_TARGET_PAGES_MAX):
                ids, page_token, has_more = await search_messages_page(
                    client, token, lo, hi, "", page_token)
                pages += 1
                for i in range(0, len(ids), 10):
                    batch = await asyncio.gather(
                        *(_try_get_message(client, token, mid) for mid in ids[i:i + 10]))
                    for m in batch:
                        if m is None:
                            continue
                        resolved += 1
                        ts = int(m.get("send_time") or 0)  # _normalize_msg 结构：秒
                        if ts:
                            earliest_ts = ts if not earliest_ts else min(earliest_ts, ts)
                        cid = m.get("chat_id", "")
                        if not cid or cid in seen:
                            continue
                        seen.add(cid)
                        try:
                            info = await get_chat_info(client, token, cid)
                        except Exception:
                            continue
                        if info["chat_mode"] != "p2p":
                            continue
                        peer = await _feishu_p2p_peer_name(client, token, cid, self_id) if len(out) < _FEISHU_P2P_TARGET_LOOKUP_MAX else ""
                        if peer:
                            out[cid] = peer
                            if target in peer.lower():
                                if meta is not None:
                                    meta.update(pages=pages, resolved=resolved, earliest_ts=earliest_ts)
                                return out  # 命中即停
                    if resolved >= _FEISHU_P2P_TARGET_RESOLVE_MAX:
                        if meta is not None:
                            meta.update(pages=pages, resolved=resolved, earliest_ts=earliest_ts)
                        return out
                if not has_more:
                    if meta is not None:
                        meta.update(pages=pages, resolved=resolved, earliest_ts=earliest_ts)
                    return out
        except Exception:
            if meta is not None:
                meta.update(pages=pages, resolved=resolved, earliest_ts=earliest_ts)
            return out
        return out
    try:
        ids = await search_messages(client, token, int(time.time()) - days * 86400, int(time.time()), "", _FEISHU_DAILY_IDS_MAX)
    except Exception:
        return {}
    out = {}
    for mid in ids[:_FEISHU_DAILY_RESOLVE_MAX]:
        try:
            m = await get_message(client, token, mid)
        except FeishuApiError:
            continue
        cid = m.get("chat_id", "")
        if not cid or cid in out:
            continue
        try:
            info = await get_chat_info(client, token, cid)
        except Exception:
            continue
        if info["chat_mode"] == "p2p":
            peer = await _feishu_p2p_peer_name(client, token, cid, self_id)
            if peer:
                out[cid] = peer
        if len(out) >= _FEISHU_P2P_LOOKUP_MAX:
            break
    return out


async def _resolve_feishu_chat(client: httpx.AsyncClient, token: str, chat: str, self_id: str = "",
                               start_ts: int = 0, end_ts: int = 0,
                               db=None, user_id: int = 0) -> tuple[str, str] | str:
    """chat 参数（chat_id / 群名 / 私聊对方人名）→ (chat_id, 显示名)；找不到返回错误文案。
    M28.3：start_ts>0 时按人名找私聊的深扫限定在 [start_ts, end_ts or now] 时间窗（高密度用户条数窗口够不到几天前）。
    M28.4：db 非空时先查私聊名册（发现过的 p2p 持久化，授权前私聊不进搜索索引也能按名字找到），新发现回写名册。"""
    chat = chat.strip()
    if chat.startswith("oc_"):
        return (chat, chat)
    chats = await list_chats(client, token)
    if not chats:
        return "你没有可访问的飞书会话（或授权缺少 im:chat 权限）"
    q = chat.lower()
    groups = [c for c in chats if c.get("chat_mode") != "p2p"]
    exact = [c for c in groups if c["name"].lower() == q]
    fuzzy = [c for c in groups if q in c["name"].lower()]
    hits = exact or fuzzy
    if not hits:
        # 群没命中 → 试试私聊：/im/v1/chats 通常不含 p2p，退到消息搜索发现近期私聊再按对方姓名匹配
        peers = await _feishu_p2p_peers(client, token, self_id, chats)
        p2p_named = {cid: n for cid, n in peers.items() if n}
        if db is not None:  # M28.4 名册打底（实时解析覆盖旧值）
            roster = await _feishu_roster_load(db, user_id)
            p2p_named = {**roster, **p2p_named}
        scan_meta: dict = {}
        if not any(q in n.lower() for n in p2p_named.values()):
            found = await _feishu_recent_p2p(  # M28.1 定向深扫（翻页更深+命中即停）；M28.3 支持时间窗+覆盖报告
                client, token, self_id, target=q, start_ts=start_ts, end_ts=end_ts, meta=scan_meta)
            p2p_named.update(found)
            if db is not None and found:
                await _feishu_roster_upsert(db, user_id, found)  # M28.4 新发现回写名册
        p2p_hits = [(cid, name) for cid, name in p2p_named.items() if q in name.lower()]
        if len(p2p_hits) == 1:
            cid, peer = p2p_hits[0]
            return (cid, f"私聊·{peer}")
        if len(p2p_hits) > 1:
            lines = "\n".join(f"- 私聊·{p}（{cid}）" for cid, p in p2p_hits[:10])
            return f"「{chat}」匹配到多个私聊，请确认是哪一个：\n{lines}"
        names = "、".join(c["name"] for c in groups[:15] if c["name"])
        peer_names = "、".join(sorted(set(p2p_named.values())))
        suffix = f"；近期私聊联系人有：{peer_names}" if peer_names else ""
        coverage = ""
        if scan_meta.get("resolved"):  # M28.3 覆盖报告：扫了多深如实告知，引导补时间窗重试
            earliest = scan_meta.get("earliest_ts") or 0
            hours = max(1, round(((end_ts or int(time.time())) - earliest) / 3600)) if earliest else 0
            span = f"最近约 {hours} 小时" if earliest else "最近消息"
            coverage = (f"（已按时间倒序扫了{span}共 {scan_meta['resolved']} 条消息；"
                        f"若记得大概聊天时间，补一句「什么时候聊的」可按时间窗定向找。"
                        f"若补了时间窗还是没有：你们最近的私聊可能早于本次飞书授权（授权前的私聊消息不进搜索索引）"
                        f"——在飞书里给ta发一条消息，会话激活后我就能找到，找到一次以后会长期记住）")
        return f"没找到名字含「{chat}」的群或私聊。你所在的群有：{names}{' …' if len(groups) > 15 else ''}{suffix}{coverage}"
    if len(hits) > 1:
        lines = "\n".join(f"- {c['name']}（{c['chat_id']}）" for c in hits[:10])
        return f"「{chat}」匹配到多个群，请确认是哪一个：\n{lines}"
    return (hits[0]["chat_id"], hits[0]["name"])


def _post_text(raw: str) -> str:
    """M8c：富文本（post）消息 content JSON → 可读文本。

    兼容两种形状：直给 {"title","content":[[...]]} 与带语言包 {"post":{"zh_cn":{...}}}。
    content 是段落二维数组；元素渲染：text→原文、a→文字(链接)、at→@姓名、img→[图片]。
    JSON 解析失败返回原始 raw（与 text 分支同款降级）。
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return raw
    if not isinstance(data, dict):
        return raw
    post = data.get("post")
    if isinstance(post, dict):  # 带语言包：优先中文，否则取第一个语言
        data = post.get("zh_cn") or next((v for v in post.values() if isinstance(v, dict)) or {}, {})
    if not isinstance(data, dict):
        return raw

    def _el(e: dict) -> str:
        tag = e.get("tag")
        if tag == "text":
            return str(e.get("text") or "")
        if tag == "a":
            t = str(e.get("text") or "")
            href = str(e.get("href") or "")
            return f"{t}({href})" if href else t
        if tag == "at":
            return "@" + str(e.get("user_name") or e.get("user_id") or "")
        if tag == "img":
            return "[图片]"
        if tag == "emotion":
            return "[表情]"
        return str(e.get("text") or "")  # 未知 tag：有 text 字段取 text，否则跳过

    lines = []
    title = str(data.get("title") or "").strip()
    if title:
        lines.append(f"【{title}】")
    for para in data.get("content") or []:
        if not isinstance(para, list):
            continue
        line = "".join(_el(e) for e in para if isinstance(e, dict)).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _feishu_msg_text(msg_type: str, raw: str, mentions: list[dict] | None = None) -> str:
    """消息体 JSON → 可读文本；非文本消息给占位。text 消息把 @_user_N 占位符还原成真实姓名。"""
    if msg_type == "text":
        try:
            text = json.loads(raw).get("text", "")
        except (json.JSONDecodeError, ValueError):
            text = raw
        # M8e：正文里的 @_user_1 是匿名占位符，真实姓名在 body.mentions（key→name 映射）
        for mt in mentions or []:
            key, name = mt.get("key") or "", mt.get("name") or ""
            if key and name:
                text = text.replace(key, "@" + name)
        return text
    if msg_type == "post":  # M8c：富文本帖解析正文，空帖回落占位
        return _post_text(raw).strip() or "[富文本消息]"
    return {"image": "[图片]", "file": "[文件]", "audio": "[语音]", "media": "[视频]",
            "sticker": "[表情]", "interactive": "[卡片消息]",
            "merge_forward": "[合并转发]", "share_chat": "[分享群]", "share_user": "[分享名片]",
            }.get(msg_type, f"[{msg_type}消息]")


async def _feishu_sender_names(client: httpx.AsyncClient, token: str, msgs: list[dict]) -> dict[str, str]:
    """批量把 sender open_id 解析成姓名；失败（缺 contact 权限）静默降级为 open_id。"""
    ids = sorted({m["sender"] for m in msgs if m["sender"]})[:50]
    if not ids:
        return {}
    try:
        return await contact_user_names(client, token, ids)
    except Exception:
        return {}


# M32d：表情回复 emoji_type key → 字形（未收录的降级为 [key] 原样显示）
_FEISHU_EMOJI = {
    "THUMBSUP": "👍", "OK": "👌", "DONE": "✅", "HEART": "❤️", "SMILE": "😄",
    "LAUGH": "😂", "CLAP": "👏", "THANKS": "🙏", "FIST": "✊", "MUSCLE": "💪",
    "PARTY": "🎉", "FIRE": "🔥", "CRY": "😭", "THUMBSDOWN": "👎", "LOVE": "😍",
    "BLUSH": "😊", "SALUTE": "🫡", "ROCKET": "🚀", "EYES": "👀", "ROSE": "🌹",
}
_FEISHU_REACTION_CAP = 20  # 每次最多拉多少条消息的表情回复（一条消息一次 API 调用，防爆量）


async def _feishu_reactions_map(client: httpx.AsyncClient, token: str,
                                msgs: list[dict], cap: int = _FEISHU_REACTION_CAP) -> dict[str, str]:
    """最近 ≤cap 条带 message_id 的消息并发拉表情回复，聚合渲染成 {message_id: "👍 张三、李四 ❤️×2"}。
    单条失败静默跳过；缺 scope 时整批降级空表（不影响读消息本身）。"""
    targets = [m for m in msgs if m.get("message_id")][-cap:]
    if not targets:
        return {}
    results = await asyncio.gather(
        *(message_reactions(client, token, m["message_id"]) for m in targets),
        return_exceptions=True)
    pairs = [(m["message_id"], r) for m, r in zip(targets, results)
             if not isinstance(r, Exception) and r]
    if not pairs:
        return {}
    op_ids = sorted({x["operator"] for _, rs in pairs for x in rs if x.get("operator")})[:50]
    names: dict[str, str] = {}
    if op_ids:
        try:
            names = await contact_user_names(client, token, op_ids)
        except Exception:
            pass  # 缺 contact 权限降级为计数
    out: dict[str, str] = {}
    for mid, rs in pairs:
        by_emoji: dict[str, list[str]] = {}
        for x in rs:
            by_emoji.setdefault(x["emoji"], []).append(x.get("operator") or "")
        segs = []
        for emoji, ops in by_emoji.items():
            glyph = _FEISHU_EMOJI.get(emoji.upper(), f"[{emoji}]")
            resolved = [names[o] for o in ops if o in names]
            if resolved:
                shown = "、".join(resolved[:5])
                segs.append(f"{glyph} {shown}" + (f" 等{len(resolved)}人" if len(resolved) > 5 else ""))
            else:
                segs.append(f"{glyph}×{len(ops)}")
        out[mid] = " ".join(segs)
    return out


async def _feishu_chat_search(ctx: "ToolContext", args: dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    # M28.3 可选时间窗：按人名找私聊时把「昨晚/上周」换算成 start/end，深扫限定窗口内（高密度用户条数窗口够不到几天前）
    start_s, end_s = str(args.get("start") or "").strip(), str(args.get("end") or "").strip()
    start_ts = end_ts = 0
    if start_s:
        start_ts = _parse_feishu_dt(start_s) or 0
        if not start_ts:
            return f"start 格式不对：{start_s}（应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM，东八区）"
        end_ts = (_parse_feishu_dt(end_s, end_of_day=True) or 0) if end_s else 0
        if end_s and not end_ts:
            return f"end 格式不对：{end_s}（应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM，东八区）"
    token_str = await get_feishu_user_token(ctx.db, ctx.user)
    if not token_str:
        return _FEISHU_AUTH_GUIDE
    self_id = ctx.user.feishu_open_id or ctx.user.username
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            chats = await list_chats(client, token_str)
            peers = await _feishu_p2p_peers(client, token_str, self_id, chats)
    except Exception as e:
        return _feishu_err(e)
    q = query.lower()
    hits = [c for c in chats if not q or q in _feishu_chat_label(c, peers).lower()]
    roster = await _feishu_roster_load(ctx.db, ctx.user.id)  # M28.4：发现过的私聊持久可查（授权前私聊不进搜索索引也能按名字找到）
    roster_hits = [(cid, n) for cid, n in roster.items() if q and q in n.lower()]
    # M28：/im/v1/chats 实测不含 p2p——列表+名册都落空时兜底搜近期私聊（镜像 _resolve_feishu_chat 的分支）；
    # 空 query（列全部）或群/名册已命中时不触发：消息搜索+逐条解析太重
    extra_p2p: list[tuple[str, str]] = []
    if q and not hits and not roster_hits:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                recent = await _feishu_recent_p2p(  # M28.1 定向深扫；M28.3 支持时间窗
                    client, token_str, self_id, target=q, start_ts=start_ts, end_ts=end_ts)
        except Exception:
            recent = {}
        extra_p2p = [(cid, n) for cid, n in recent.items() if q in n.lower()]
        if extra_p2p:
            await _feishu_roster_upsert(ctx.db, ctx.user.id, dict(extra_p2p))  # M28.4 新发现回写名册
    roster_hits = [(cid, n) for cid, n in roster_hits if cid not in dict(extra_p2p)]
    _audit_tool(ctx, "feishu.chat_search", {"query": query, "hits": len(hits) + len(roster_hits) + len(extra_p2p), "window": bool(start_ts)})
    if not hits and not roster_hits and not extra_p2p:
        tip = ("；若记得大概聊天时间，用 start/end 时间窗再试（如 start='2026-09-21 19:00' end='2026-09-21 21:00'）；"
               "若时间窗内也没有，你们最近的私聊可能早于本次授权（授权前的私聊不进搜索索引）——在飞书里给ta发一条消息激活后就能找到" if (q and not start_ts) else "")
        return f"没找到名字含「{query}」的会话（只能看到你本人所在的群和近期私聊{tip}）" if q else "你没有任何飞书会话"
    head = f"匹配到 {len(hits) + len(roster_hits) + len(extra_p2p)} 个会话：" if q else f"你共有 {len(hits)} 个会话（群 + 私聊）："
    lines = [f"- {_feishu_chat_label(c, peers)}（chat_id: {c['chat_id']}）" for c in hits[:20]]
    lines += [f"- 私聊·{n}（chat_id: {cid}）" for cid, n in (roster_hits + extra_p2p)[:20]]
    return head + "\n" + "\n".join(lines)


async def _save_feishu_bytes(ctx: "ToolContext", rel: str, data: bytes, mime: str) -> str:
    """飞书二进制（群聊图片/云盘文件）落工作区并登记 WorkspaceFile（无 content_text），返回工作区相对路径。"""
    root = _workspace_root(ctx.user.id, ctx.agent.id).resolve()
    target = (root / rel).resolve()
    if root not in target.parents:
        raise ValueError("路径越界")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    row = (
        await ctx.db.execute(
            select(WorkspaceFile).where(
                WorkspaceFile.user_id == ctx.user.id,
                WorkspaceFile.agent_id == ctx.agent.id,
                WorkspaceFile.filename == rel,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        ctx.db.add(WorkspaceFile(
            user_id=ctx.user.id, agent_id=ctx.agent.id, filename=rel,
            mime=mime, size=len(data), storage_path=str(target),
        ))
    else:
        row.size = len(data)
        row.storage_path = str(target)
        row.mime = mime
        row.deleted_at = None
    await ctx.db.commit()
    return rel


async def sweep_feishu_images(db: AsyncSession) -> int:
    """M8d：定时清扫超过 _FEISHU_IMG_TTL_DAYS 的 feishu_imgs/ 落盘图片。

    DB 是事实源（不扫磁盘）：软删记录 + unlink 磁盘文件，照搬 _workspace_delete 的双写。
    落盘是幂等的（_save_feishu_bytes 会复活软删行），清理后重拉同一条消息图片会自动重新下载。
    返回清理数；磁盘文件缺失/异常只跳过不中断整批。系统保洁，不落 audit_logs。
    """
    cutoff = utcnow() - timedelta(days=_FEISHU_IMG_TTL_DAYS)
    rows = (
        (
            await db.execute(
                select(WorkspaceFile).where(
                    WorkspaceFile.filename.like("feishu_imgs/%"),
                    WorkspaceFile.deleted_at.is_(None),
                    WorkspaceFile.created_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    for f in rows:
        f.deleted_at = utcnow()
        try:
            Path(f.storage_path).unlink(missing_ok=True)
        except OSError:
            pass
    if rows:
        await db.commit()
    return len(rows)


async def _save_feishu_image(ctx: "ToolContext", chat_id: str, message_id: str, data: bytes, suffix: str = "") -> str:
    """群聊图片二进制落工作区 feishu_imgs/（M10a 起底层走 _save_feishu_bytes）。
    suffix：M8c.1 post 帖多图传 _p1/_p2…，独立图片消息不传（路径格式不变）。"""
    rel = f"feishu_imgs/{chat_id[-6:]}_{message_id[-6:]}{suffix}.png"
    return await _save_feishu_bytes(ctx, rel, data, "image/png")


async def _feishu_download_images(ctx: "ToolContext", client: httpx.AsyncClient, token: str, msgs: list[dict], chat_id: str = "") -> dict[str, list[str]]:
    """消息里的图片下载前几张存工作区（模型可用 run_code OCR，用户可在界面预览）；单张失败静默降级。
    返回 message_id → 已存路径列表：image 消息单元素；M8c.1 起 post 帖附图也下载（每帖上限
    _FEISHU_POST_IMG_MAX 张，与 image 共享全局 _FEISHU_IMG_MAX 预算）。
    chat_id 可省略——消息结构自带 chat_id 时优先用消息的（daily 跨会话场景）。"""
    img_paths: dict[str, list[str]] = {}
    total = 0
    for m in msgs:
        if total >= _FEISHU_IMG_MAX:
            break
        if m["msg_type"] == "image" and m.get("image_key") and m.get("message_id"):
            try:
                data = await download_im_resource(client, token, m["message_id"], m["image_key"])
                if len(data) <= _FEISHU_IMG_SIZE_MAX:
                    img_paths[m["message_id"]] = [await _save_feishu_image(
                        ctx, m.get("chat_id") or chat_id, m["message_id"], data)]
                    total += 1
            except Exception:
                pass
        elif m["msg_type"] == "post" and m.get("message_id"):
            saved: list[str] = []
            for i, key in enumerate((m.get("image_keys") or [])[:_FEISHU_POST_IMG_MAX], 1):
                if total >= _FEISHU_IMG_MAX:
                    break
                try:
                    data = await download_im_resource(client, token, m["message_id"], key)
                    if len(data) <= _FEISHU_IMG_SIZE_MAX:
                        saved.append(await _save_feishu_image(
                            ctx, m.get("chat_id") or chat_id, m["message_id"], data, suffix=f"_p{i}"))
                        total += 1
                except Exception:
                    pass
            if saved:
                img_paths[m["message_id"]] = saved
    return img_paths


def _feishu_format_msgs(msgs: list[dict], names: dict[str, str], img_paths: dict[str, list[str]],
                        reactions: dict[str, str] | None = None) -> list[str]:
    """消息 → 「时间 发送者：内容」行（图片带工作区路径标注，行尾附表情回复）；chat_history / daily 共用。"""
    lines = []
    for m in msgs:
        paths = img_paths.get(m.get("message_id", "")) or []
        if m["msg_type"] == "image":
            text = f"[图片 已存工作区 {paths[0]}，需要图中内容可用 run_code 识别]" if paths else "[图片]"
        else:
            text = _feishu_msg_text(m["msg_type"], m["text"], m.get("mentions")).strip()
            if not text:
                continue
            if m["msg_type"] == "post" and paths:  # M8c.1：帖附图落盘标注
                text += (f"\n[帖内图片 {len(paths)} 张已存工作区：{'、'.join(paths)}，"
                         "可用 run_code 识别图中文字并与正文合并理解]")
        ts = datetime.fromtimestamp(m["send_time"], tz=ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M")
        line = f"{ts} {names.get(m['sender'], m['sender'][:12])}：{text}"
        rx = (reactions or {}).get(m.get("message_id", ""))
        if rx:  # M32d：表情回复=已反馈，跟随消息行展示
            line += f" [表情回复: {rx}]"
        lines.append(line)
    return lines


async def _feishu_chat_history(ctx: "ToolContext", args: dict[str, Any]) -> str:
    chat = str(args.get("chat") or "").strip()
    if not chat:
        return "chat 不能为空（群名、对方人名或 chat_id）"
    days = args.get("days") or 1
    days = max(0.04, min(float(days), 30))
    max_messages = max(1, min(int(args.get("max_messages") or 200), 500))
    # M28.3 可选时间窗：高密度用户条数窗口只覆盖几小时，「昨晚8点」类提示换算成 start/end 才能定到几天前的私聊
    start_s, end_s = str(args.get("start") or "").strip(), str(args.get("end") or "").strip()
    start_ts = end_ts = 0
    if start_s:
        start_ts = _parse_feishu_dt(start_s) or 0
        if not start_ts:
            return f"start 格式不对：{start_s}（应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM，东八区）"
        if end_s:
            end_ts = _parse_feishu_dt(end_s, end_of_day=True) or 0
            if not end_ts:
                return f"end 格式不对：{end_s}（应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM，东八区）"
        elif len(start_s.replace("/", "-")) <= 10:  # 只给日期 → 当天全天
            end_ts = _parse_feishu_dt(start_s, end_of_day=True) or 0
    token_str = await get_feishu_user_token(ctx.db, ctx.user)
    if not token_str:
        return _FEISHU_AUTH_GUIDE
    now = time.time()
    end_ts = min(end_ts, int(now)) if end_ts else 0
    fetch_start = start_ts or int(now - days * 86400)
    fetch_end = end_ts or int(now)
    self_id = ctx.user.feishu_open_id or ctx.user.username
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resolved = await _resolve_feishu_chat(client, token_str, chat, self_id, start_ts=start_ts, end_ts=end_ts,
                                                  db=ctx.db, user_id=ctx.user.id)
            if isinstance(resolved, str):
                return resolved
            chat_id, chat_name = resolved
            msgs = await chat_messages(client, token_str, chat_id, fetch_start, fetch_end, max_messages)
            names = await _feishu_sender_names(client, token_str, msgs)
            img_paths = await _feishu_download_images(ctx, client, token_str, msgs, chat_id)
            reactions = await _feishu_reactions_map(client, token_str, msgs)
    except Exception as e:
        return _feishu_err(e)
    span_desc = (f"{datetime.fromtimestamp(fetch_start, tz=ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M')}"
                 f"~{datetime.fromtimestamp(fetch_end, tz=ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M')}") if start_ts else f"近 {days:g} 天"
    _audit_tool(ctx, "feishu.chat_history", {"chat_id": chat_id, "days": days, "window": bool(start_ts), "messages": len(msgs), "images_saved": len(img_paths)})
    if not msgs:
        return f"「{chat_name}」{span_desc}没有消息（或该时段消息不可见）"
    lines = _feishu_format_msgs(msgs, names, img_paths, reactions)
    out = f"「{chat_name}」{span_desc}共 {len(lines)} 条消息：\n" + "\n".join(lines)
    if len(out) > _FEISHU_TEXT_MAX:
        out = out[:_FEISHU_TEXT_MAX] + "\n…[消息过多已截断，先总结这部分；要更多可缩小天数或增大 max_messages 再拉]"
    return out


async def _feishu_chat_daily(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """时间窗全量扫用户飞书消息（M8 日报的原料工具）：search/v2/message 空 query 按时间窗拿 message_id，
    逐条 get_message 取正文，按会话分组。群聊+私聊均可读（私聊需 im:message.p2p_msg:get_as_user，
    M8b 2026-08-28 云上实测解锁）；个别仍失败的跳过并如实计数。"""
    days = max(0.04, min(float(args.get("days") or 1), 3))
    max_chats = max(1, min(int(args.get("max_chats") or 10), 20))
    person = str(args.get("person") or "").strip().lower()
    token_str = await get_feishu_user_token(ctx.db, ctx.user)
    if not token_str:
        return _FEISHU_AUTH_GUIDE
    now = time.time()
    start_ts = int(now - days * 86400)
    self_id = ctx.user.feishu_open_id or ctx.user.username
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            ids = await search_messages(client, token_str, start_ts, int(now), "", _FEISHU_DAILY_IDS_MAX)
            if not ids:
                return f"近 {days:g} 天你的飞书没有消息（休息日？）"
            # 逐条取正文；个别消息失败（权限边界/已撤回等）跳过计数，不影响整体
            by_chat: dict[str, list[dict]] = {}
            unreadable = 0
            for mid in ids[:_FEISHU_DAILY_RESOLVE_MAX]:
                try:
                    m = await get_message(client, token_str, mid)
                except FeishuApiError:
                    unreadable += 1
                    continue
                if m.get("chat_id"):
                    by_chat.setdefault(m["chat_id"], []).append(m)
            if not by_chat:
                return (f"近 {days:g} 天搜到你 {len(ids)} 条飞书消息，但正文全部读取失败"
                        "（可能授权缺少 im:message.p2p_msg:get_as_user，请走 /login?upgrade=1 重授权）")
            # 会话显示名：先查会话列表缓存，不在列表里的单独 get_chat_info
            chats = await list_chats(client, token_str)
            peers = await _feishu_p2p_peers(client, token_str, self_id, chats)
            meta = {c["chat_id"]: c for c in chats}
            labels: dict[str, str] = {}
            discovered: dict[str, str] = {}  # M28.4：日报顺手给私聊名册保鲜
            for cid in by_chat:
                if cid in meta:
                    labels[cid] = _feishu_chat_label(meta[cid], peers)
                else:
                    try:
                        info = await get_chat_info(client, token_str, cid)
                        if info["chat_mode"] == "p2p":
                            # 私聊不在 /im/v1/chats 列表，单独解析对方姓名（机器人会话解析不出则「私聊」）
                            peer = await _feishu_p2p_peer_name(client, token_str, cid, self_id)
                            labels[cid] = f"私聊·{peer}" if peer else "私聊"
                            if peer:
                                discovered[cid] = peer
                        else:
                            labels[cid] = info["name"] or cid[-6:]
                    except Exception:
                        labels[cid] = f"会话 {cid[-6:]}"
            if discovered:
                await _feishu_roster_upsert(ctx.db, ctx.user.id, discovered)
            if person:
                by_chat = {cid: ms for cid, ms in by_chat.items() if person in labels.get(cid, "").lower()}
                if not by_chat:
                    return (f"近 {days:g} 天没有和「{person}」相关的会话（本工具按时间窗扫最近消息，窗口有限；"
                            f"可用 feishu_chat_history 直接传对方人名，它会定向深扫近 30 天；或按 chat_id 指定会话再试）")
            # 排版：发送者姓名全程一次 batch；图片共享下载上限；表情回复取最新 20 条消息（M32d）
            all_msgs = [m for ms in by_chat.values() for m in ms]
            names = await _feishu_sender_names(client, token_str, all_msgs)
            img_paths = await _feishu_download_images(ctx, client, token_str, all_msgs)
            reactions = await _feishu_reactions_map(
                client, token_str, sorted(all_msgs, key=lambda m: m["send_time"]))
            sections = []
            total = 0
            for cid, ms in sorted(by_chat.items(), key=lambda kv: -len(kv[1]))[:max_chats]:
                ms.sort(key=lambda m: m["send_time"])
                lines = _feishu_format_msgs(ms, names, img_paths, reactions)
                if lines:
                    total += len(lines)
                    sections.append(f"## {labels[cid]}（{len(lines)} 条）\n" + "\n".join(lines))
    except Exception as e:
        return _feishu_err(e)
    _audit_tool(ctx, "feishu.chat_daily", {
        "days": days, "chats": len(sections), "messages": total,
        "unreadable_p2p": unreadable, "images_saved": len(img_paths),
    })
    if not sections:
        return f"近 {days:g} 天没有可读的会话消息"
    out = (f"近 {days:g} 天共 {len(sections)} 个会话 {total} 条可读消息，素材如下，"
           "请提炼重要事项/待办/被分享的内容：\n\n" + "\n\n".join(sections))
    if unreadable:
        out += f"\n\n（另有 {unreadable} 条消息正文读取失败已跳过，可能是已撤回或权限边界）"
    if len(out) > _FEISHU_TEXT_MAX:
        out = out[:_FEISHU_TEXT_MAX] + "\n…[素材过多已截断，可缩小 max_chats 或对重点会话用 feishu_chat_history 细看]"
    return out


def _approval_ts(v: Any) -> str:
    """审批时间戳（秒或毫秒字符串皆有）→ MM-DD HH:mm；解析不出返回空串。"""
    try:
        t = int(v)
    except (TypeError, ValueError):
        return ""
    if t > 10**12:
        t //= 1000
    return datetime.fromtimestamp(t, tz=ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M")


def _approval_line(item: dict, with_status: bool) -> str:
    """嵌套 item（{approval, group, instance, task?}）→ 一行摘要。字段缺失时降级为只有名称。
    行尾带 instance code——用户追问某条明细时，模型拿它调 feishu_approval_detail（M9a.2）。"""
    inst = item.get("instance") or {}
    task = item.get("task") or {}
    name = (item.get("approval") or {}).get("name") or inst.get("name") or "（未命名审批）"
    parts = []
    if with_status:
        status = task.get("status") or inst.get("status") or ""
        if status:
            parts.append(str(status))
    when = _approval_ts(inst.get("start_time") or task.get("start_time"))
    if when:
        parts.append(when)
    code = inst.get("code") or item.get("instance_code") or ""
    if code:
        parts.append(str(code))
    return f"- {name}" + (f"（{'｜'.join(parts)}）" if parts else "")


async def _feishu_approval_daily(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """飞书审批三段动态（M9a）：待我审批/我发起的/抄送我的。
    tenant token + 按当前用户 open_id 过滤（审批查询接口只支持应用身份，99991668 云上实测）；
    需要应用身份档 scope approval:approval.list:readonly。只读，审计只记计数。"""
    days = max(1, min(float(args.get("days") or 30), 90))
    open_id = getattr(ctx.user, "feishu_open_id", None)
    if not open_id:
        return ("审批查询按你的飞书身份过滤：请先用「飞书登录」进过一次平台（自动绑定飞书身份），"
                "再重试；还没绑定的账号查不了。")
    now = int(time.time())
    start_ts = now - int(days * 86400)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            app_token = await app_access_token(client)
            results: list[tuple[str, str, list[dict] | None, str]] = []  # (key, 标题, items, 错误)
            for key, title, fetch in (
                ("pending", "待我审批",
                 lambda: approval_pending_tasks(client, app_token, open_id)),
                ("mine", f"我发起的（近 {days:g} 天）",
                 lambda: approval_my_instances(client, app_token, open_id, start_ts, now)),
                ("cc", f"抄送我的（近 {days:g} 天）",
                 lambda: approval_cc_instances(client, app_token, open_id, start_ts, now)),
            ):
                try:
                    results.append((key, title, await fetch(), ""))
                except Exception as e:
                    results.append((key, title, None, str(e)))
    except Exception as e:
        return _feishu_err(e)
    counts = {k: (len(items) if items is not None else -1) for k, _, items, _ in results}
    _audit_tool(ctx, "feishu.approval_daily", {"days": days, **counts})
    sections = []
    for key, title, items, err in results:
        if items is None:
            sections.append(f"## {title}\n读取失败：{err}")
        elif not items:
            sections.append(f"## {title}\n（无）")
        else:
            lines = [_approval_line(it, with_status=(key != "pending")) for it in items[:15]]
            body = "\n".join(lines)
            if len(items) > 15:
                body += f"\n…（共 {len(items)} 条，只列前 15）"
            sections.append(f"## {title}（{len(items)} 条）\n{body}")
    out = ("你的飞书审批动态如下，请提炼：需要你尽快处理的、被驳回/撤回的、长期卡住的：\n\n"
           + "\n\n".join(sections))
    if len(out) > _FEISHU_TEXT_MAX:
        out = out[:_FEISHU_TEXT_MAX] + "\n…[内容过多已截断，可缩小 days 重拉]"
    return out


_APPROVAL_DETAIL_FIELD_MAX = 200  # 单个表单字段值截断
_APPROVAL_DETAIL_ROW_MAX = 8      # 明细控件（fieldList）最多渲染几行
_APPROVAL_TASK_STATUS = {
    "PENDING": "待处理", "APPROVED": "已同意", "REJECTED": "已拒绝",
    "CANCELED": "已取消", "DELETED": "已删除",
}


def _approval_widget_value(w: dict) -> str:
    """表单控件 → 值文本（2026-08-31 云上实测控件形态）：
    radio/input/textarea=字符串；checkbox=字符串数组；fieldList 明细=多行子控件数组。"""
    v = w.get("value")
    if v is None or v == "":
        return ""
    if isinstance(v, str):
        return v[:_APPROVAL_DETAIL_FIELD_MAX]
    if isinstance(v, list):
        if v and all(isinstance(r, list) for r in v):  # fieldList：[[{子控件}, …], …]
            rows = []
            for row in v[:_APPROVAL_DETAIL_ROW_MAX]:
                cells = "、".join(
                    f"{c.get('name')}={str(c.get('value'))[:50]}"
                    for c in row if isinstance(c, dict) and c.get("value") not in (None, ""))
                rows.append(cells or "（空行）")
            more = f"；…共 {len(v)} 行" if len(v) > _APPROVAL_DETAIL_ROW_MAX else ""
            return "；".join(rows) + more
        return "、".join(str(x)[:50] for x in v)[:_APPROVAL_DETAIL_FIELD_MAX]
    return str(v)[:_APPROVAL_DETAIL_FIELD_MAX]


async def _feishu_approval_detail(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """审批实例详情（M9a.2）：表单明细 + 审批进度。
    tenant token（此端点不收 user token，99991668 实测；应用身份档 scope
    approval:approval:readonly，和列表用的 approval.list:readonly 是两把钥匙）。
    审批人姓名复用用户 token 的 contact 权限解析，缺了静默降级 open_id。审计只记 code/状态。
    入参支持业务单号：纯数字流水号（serial_id，如 202608280029）不是实例 code（GUID 带横杠），
    自动在当前用户三段列表里匹配出真实 code 再查——用户天然只会报单号（云上实测踩坑）。"""
    code = str(args.get("instance_code") or "").strip()
    if not code:
        return "请提供 instance_code（先调 feishu_approval_daily 拉列表，每行末尾括号里的最后一段就是）；业务单号（纯数字流水号）也可以，我会自动匹配。"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            app_token = await app_access_token(client)
            data = None
            if "-" in code:  # 实例 code 是带横杠的 GUID；纯单号直接跳过必然失败的调用
                try:
                    data = await approval_instance_detail(client, app_token, code)
                except FeishuApiError:
                    data = None  # 落入下方单号/列表匹配兜底
            if data is None:
                open_id = getattr(ctx.user, "feishu_open_id", None)
                if open_id:
                    now = int(time.time())
                    for fetch in (
                        lambda: approval_pending_tasks(client, app_token, open_id),
                        lambda: approval_my_instances(client, app_token, open_id, now - 90 * 86400, now),
                        lambda: approval_cc_instances(client, app_token, open_id, now - 90 * 86400, now),
                    ):
                        try:
                            items = await fetch()
                        except Exception:
                            continue
                        for it in items:
                            inst = it.get("instance") or {}
                            if code in (str(inst.get("serial_id") or ""), str(inst.get("code") or "")):
                                data = await approval_instance_detail(
                                    client, app_token, str(inst["code"]))
                                code = str(inst["code"])
                                break
                        if data is not None:
                            break
                if data is None:
                    return (f"没找到「{code}」对应的审批实例。实例编号是带横杠的 GUID；"
                            f"业务单号已在你的待办/发起/抄送（近 90 天）里匹配过也没命中——"
                            f"可能不在你的可见范围或超出时间窗，请用户核对单号。")
            user_token = await get_feishu_user_token(ctx.db, ctx.user)
            names: dict[str, str] = {}
            if user_token:
                try:
                    names = await contact_user_names(
                        client, user_token,
                        [t.get("open_id", "") for t in data.get("task_list") or []])
                except Exception:
                    names = {}
    except Exception as e:
        return _feishu_err(e)
    _audit_tool(ctx, "feishu.approval_detail",
                {"instance_code": code, "status": data.get("status")})

    lines = [f"## {data.get('approval_name') or '（未命名审批）'}"]
    meta = [f"状态：{data.get('status') or '未知'}"]
    if data.get("serial_number"):
        meta.append(f"单号：{data['serial_number']}")
    start = _approval_ts(data.get("start_time"))
    raw_end = str(data.get("end_time") or "0")
    end = _approval_ts(raw_end) if raw_end not in ("", "0") else ""  # 未完结时 end_time="0"
    if start:
        meta.append(f"发起：{start}" + (f"｜完结：{end}" if end else ""))
    lines.append("｜".join(meta))

    try:
        widgets = json.loads(data.get("form") or "[]")
    except (TypeError, ValueError):
        widgets = []
    field_lines = []
    for w in widgets:
        if not isinstance(w, dict) or w.get("type") == "text":  # text 控件是静态说明文字，无用户输入
            continue
        val = _approval_widget_value(w)
        if val:
            field_lines.append(f"- {w.get('name') or '（未命名字段）'}：{val}")
    lines.append("### 表单内容\n" + ("\n".join(field_lines) if field_lines else "（无表单字段或未返回）"))

    tasks = sorted(
        (t for t in data.get("task_list") or [] if isinstance(t, dict)),
        key=lambda t: int(t.get("start_time") or 0))
    prog = []
    for t in tasks:
        who = names.get(t.get("open_id") or "") or (t.get("open_id") or "?")[:12]
        st = _APPROVAL_TASK_STATUS.get(str(t.get("status") or ""), str(t.get("status") or ""))
        raw_t_end = str(t.get("end_time") or "0")
        when = _approval_ts(raw_t_end) if raw_t_end != "0" else ""
        prog.append(f"- {t.get('node_name') or '审批'}·{who}：{st}" + (f"（{when}）" if when else ""))
    if prog:
        lines.append("### 审批进度\n" + "\n".join(prog))
    out = "\n".join(lines)
    if len(out) > _FEISHU_TEXT_MAX:
        out = out[:_FEISHU_TEXT_MAX] + "\n…[内容过多已截断]"
    return out


_MAIL_PREVIEW_MAX = 120  # 邮件正文预览单行截断
_MAIL_LABEL_CN = {"UNREAD": "未读", "IMPORTANT": "重要"}


def _mail_line(meta: dict) -> str:
    """邮件元数据 → 一行：- [未读][重要] 主题（发件人/收件人｜MM-DD HH:mm｜正文预览…） 📎附件。"""
    flags = "".join(f"[{_MAIL_LABEL_CN[l]}]" for l in meta.get("labels") or [] if l in _MAIL_LABEL_CN)
    subject = meta.get("subject") or "（无主题）"
    parts = []
    if meta.get("kind") == "sent":
        to = meta.get("recipients") or []
        if to:
            label = "收件人：" + "、".join(to)
            cc = meta.get("cc") or []
            if cc:
                label += "，抄送：" + "、".join(cc)
            parts.append(label)
    elif meta.get("sender"):
        parts.append(f"发件人：{meta['sender']}")
    when = _approval_ts(meta.get("date_ms")) if meta.get("date_ms") else ""  # 0/缺失不渲染 1970
    if when:
        parts.append(when)
    preview = " ".join(str(meta.get("preview") or "").split())[:_MAIL_PREVIEW_MAX]
    if preview:
        parts.append(preview)
    line = f"- {flags}{subject}" + (f"（{'｜'.join(parts)}）" if parts else "")
    atts = meta.get("attachments") or []
    if atts:
        line += f" 📎{len(atts)}个附件：{'、'.join(atts[:5])}" + ("…" if len(atts) > 5 else "")
    return line


async def _scan_mail_folder(
    client: httpx.AsyncClient, token: str, folder_id: str, max_messages: int, cutoff_ms: int, kind: str,
) -> tuple[list[dict], int, int]:
    """扫一个邮件文件夹：列表（时间倒序）→ 逐条 metadata → internal_date 撞窗即 break。
    返回 (metas, older_skipped, failed)；kind 决定 _mail_line 显示发件人还是收件人。"""
    ids = await mail_message_ids(client, token, folder_id, max_ids=max_messages)
    metas: list[dict] = []
    older_skipped = 0
    failed = 0
    for i, mid in enumerate(ids):
        try:
            meta = await mail_message_meta(client, token, mid)
        except Exception:
            failed += 1
            continue
        if meta["date_ms"] and meta["date_ms"] < cutoff_ms:
            older_skipped = len(ids) - i  # 倒序：从这条起全部超出时间窗
            break
        meta["kind"] = kind
        metas.append(meta)
    return metas, older_skipped, failed


def _mail_section(fid: str, metas: list[dict], older: int, failed: int, days: float, unread: int | None) -> str:
    """单个文件夹 → 自洽的日报段落（summary 行 + 逐封素材行）。"""
    name = "发件箱" if fid == "SENT" else "收件箱"
    header = f"{name}近 {days:g} 天共 {len(metas)} 封邮件"
    if unread is not None:
        header += f"（{name}当前总未读 {unread} 封）"
    notes = []
    if older:
        notes.append(f"另有 {older} 封更早的未列出")
    if failed:
        notes.append(f"{failed} 封读取失败已跳过")
    if notes:
        header += "（" + "；".join(notes) + "）"
    if not metas:
        return header + "。时间窗内没有邮件。"
    return (header + "，素材如下，请提炼：需要回复/跟进的、重要通知、带附件的交付物：\n\n"
            + "\n".join(_mail_line(m) for m in metas))


async def _feishu_mail_daily(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """邮件日报（M9b/M9b.2）：用户本人邮箱收件箱+发件箱近 N 天（主题/收发件人/预览/附件/未读/重要）。
    per-user token（邮箱是用户身份档字段级 scope）。列表接口只给 message_id（时间倒序、无时间字段），
    时间窗截停靠逐条 metadata 的 internal_date 判断——撞到窗口边缘即停止（倒序保证后面全更早）。
    只读；审计只记计数，不落主题/正文/收发件人。"""
    days = max(1, min(float(args.get("days") or 1), 7))
    max_messages = max(1, min(int(args.get("max_messages") or 30), 50))
    folder = str(args.get("folder") or "both").lower()
    if folder not in ("inbox", "sent", "both"):
        folder = "both"
    # M9b 修复：先查 scope 再动（普通登录会把内容 scope 清成最小权限，此时拿最小 token 调邮箱
    # 报的是含糊的 field validation failed，不如直接给授权引导）。
    scopes = getattr(ctx.user, "feishu_scopes", "") or ""
    if "mail:user_mailbox.message:readonly" not in scopes:
        return _FEISHU_AUTH_GUIDE
    token = await get_feishu_user_token(ctx.db, ctx.user)
    if not token:
        return ("邮箱读取需要你的飞书授权：请打开 /login?upgrade=1 完成一次增量授权"
                "（勾选邮箱相关权限）后重试；如果授权页没有邮箱权限，说明应用侧没开通，找管理员。")
    cutoff_ms = int(time.time() * 1000) - int(days * 86400 * 1000)
    targets = {"inbox": ["INBOX"], "sent": ["SENT"], "both": ["INBOX", "SENT"]}[folder]
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            unread: dict[str, int | None] = {"INBOX": None, "SENT": None}
            try:
                for f in await mail_folders(client, token):
                    unread[f["folder_id"]] = f["unread_count"]
            except Exception:
                pass  # 文件夹列表失败不阻塞（只少总未读数）
            sections = []
            for fid in targets:
                metas, older, failed = await _scan_mail_folder(
                    client, token, fid, max_messages, cutoff_ms,
                    "sent" if fid == "SENT" else "inbox",
                )
                sections.append((fid, metas, older, failed))
    except FeishuApiError as e:
        # 邮箱专属文案：覆盖「邮箱未开通」与「授权过期」两支，不再让 field validation failed 裸奔误导模型
        return (f"邮箱读取失败：{e}。可能邮箱未开通，或授权过期——请打开 "
                f"[飞书数据授权](/login?upgrade=1) 重新授权后重试。")
    except Exception as e:
        return _feishu_err(e)
    total = sum(len(m) for _, m, _, _ in sections)
    unread_in_window = sum(1 for _, m, _, _ in sections for m in m if "UNREAD" in (m.get("labels") or []))
    _audit_tool(ctx, "feishu.mail_daily",
                {"days": days, "folder": folder, "messages": total, "unread": unread_in_window})
    out = "\n\n".join(_mail_section(fid, m, o, f, days, unread.get(fid)) for fid, m, o, f in sections)
    if len(out) > _FEISHU_TEXT_MAX:
        out = out[:_FEISHU_TEXT_MAX] + "\n…[邮件过多已截断，可缩小 days 重拉]"
    return out


async def _feishu_mail_search(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """按关键词搜用户本人邮箱（M9b.3）。搜索接口（POST /user_mailboxes/me/search）返回命中
    message_id，再逐条取 metadata 复用 _mail_line 渲染（收件箱显发件人、发件箱显收件人）。
    只读；审计只记计数，不落正文/收发件人。"""
    query = str(args.get("query") or "").strip()
    if not query:
        return "请提供要搜索的关键词（如「合同」），我才能去邮箱里找。"
    max_results = max(1, min(int(args.get("max_results") or 20), 50))
    scopes = getattr(ctx.user, "feishu_scopes", "") or ""
    if "mail:user_mailbox.message:readonly" not in scopes:
        return _FEISHU_AUTH_GUIDE
    token = await get_feishu_user_token(ctx.db, ctx.user)
    if not token:
        return ("邮箱读取需要你的飞书授权：请打开 /login?upgrade=1 完成一次增量授权"
                "（勾选邮箱相关权限）后重试；如果授权页没有邮箱权限，说明应用侧没开通，找管理员。")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            ids = await mail_search(client, token, query, max_ids=max_results)
            metas: list[dict] = []
            failed = 0
            for mid in ids:
                try:
                    meta = await mail_message_meta(client, token, mid)
                except Exception:
                    failed += 1
                    continue
                meta["kind"] = "sent" if meta.get("folder_id") == "SENT" else "inbox"
                metas.append(meta)
    except FeishuApiError as e:
        return (f"邮箱搜索失败：{e}。可能邮箱未开通，或授权过期——请打开 "
                f"[飞书数据授权](/login?upgrade=1) 重新授权后重试。")
    except Exception as e:
        return _feishu_err(e)
    _audit_tool(ctx, "feishu.mail_search", {"query": query, "results": len(metas)})
    if not metas:
        head = f"邮箱搜索「{query}」没找到匹配的邮件"
        if failed:
            head += f"（{failed} 封读取失败已跳过）"
        return head + "。换个关键词试试，或告诉我更具体的内容。"
    header = f"邮箱搜索「{query}」命中 {len(metas)} 封"
    if failed:
        header += f"（另有 {failed} 封读取失败已跳过）"
    out = header + "，按时间列出如下，请提炼要点：\n\n" + "\n".join(_mail_line(m) for m in metas)
    if len(out) > _FEISHU_TEXT_MAX:
        out = out[:_FEISHU_TEXT_MAX] + "\n…[结果过多已截断，可缩小 max_results 或换更精确的关键词]"
    return out


# ---------- 智书合同工具（M9c；公司级 tenant token；按 users.zhishu_user_id 过滤本人可见合同） ----------
# 契约（2026-09-01 云上探针 v6/v7 实测）：
#   - 智书 user_id_type=user_id 用的就是飞书「用户ID」（user_id 字段，如 944f4998），
#     登录时从飞书自动抓取存 users.zhishu_user_id（非飞书用户才在管理端手填兜底）
#   - 搜索必须带 combine_condition（不带返回 0 条）；user_id_type 只有 user_id 真正过滤
#     （create_user_name 等姓名字段被静默忽略）；permission=1 我的可见 / 2 我申请的
_ZHISHU_STATUS_CN = {
    0: "编辑中", 1: "已取消", 2: "已撤销", 3: "审批中", 4: "已驳回", 5: "已通过",
    6: "签订中", 7: "已签约", 8: "归档中", 9: "已归档", 10: "变更中", 11: "已变更",
    12: "我方已签约", 13: "对方已签约",
}
_ZHISHU_PERM_CN = {1: "我可见", 2: "我申请的"}


def _zhishu_line(item: dict) -> str:
    """合同搜索 item → 一行：- [审批中] 合同名（编号｜对方｜金额｜发起人｜状态）。"""
    status_code = item.get("contract_status_code")
    status = item.get("contract_status_name") or _ZHISHU_STATUS_CN.get(status_code, str(status_code or ""))
    name = item.get("contract_name") or "（无名称）"
    number = item.get("contract_number") or ""
    cps = "、".join(c.get("counter_party_name", "") for c in item.get("counter_party_list") or []) or "—"
    amount = item.get("amount")
    currency = item.get("currency_name") or ""
    amt = f"{amount} {currency}".strip() if amount not in (None, "") else ""
    creator = item.get("submitter_user_name") or item.get("create_user_name") or ""
    parts = [p for p in [f"编号{number}" if number else "", f"对方{cps}", amt, f"发起人{creator}" if creator else ""] if p]
    return f"- [{status}] {name}" + (f"（{'｜'.join(parts)}）" if parts else "")


async def _zhishu_contract_mine(ctx: "ToolContext", args: dict[str, Any]) -> str:
    """「与我相关」的智书合同（M9c）：当前用户本人可见/发起的合同列表。
    公司级 tenant token（无 per-user OAuth）；按 users.zhishu_user_id + permission 过滤。
    只读；审计只记动作计数，不落合同名/金额。"""
    zsid = getattr(ctx.user, "zhishu_user_id", "") or ""
    if not zsid:
        return ("智书合同读取需要你的智书 user_id：请联系管理员在管理端为你的账号填上"
                "（智书后台/你自己负责的合同上能反查到，形如 944f4998 的 8 位小写串），填好后重试。")
    permission = int(args.get("permission") or 1)
    if permission not in (1, 2):
        permission = 1
    # M9c.2：query → combine_condition.contract_name（子串模糊匹配，探针 v8 实测；与 user_id/permission 是 AND）
    query = str(args.get("query") or "").strip()
    # M9c.3：时间窗（days 或 start_date/end_date，后者优先）+ 状态过滤 + 自动翻页（5 页 × 50 = 250 条防爆量）
    max_items = max(1, min(int(args.get("max_items") or 20), 250))
    time_field = str(args.get("time_field") or "submit").strip()
    if time_field not in ("create", "submit", "update", "archive"):
        time_field = "submit"
    start_date = str(args.get("start_date") or "").strip()
    end_date = str(args.get("end_date") or "").strip()
    days = max(0, min(int(args.get("days") or 0), 365))
    time_start = time_end = time_desc = ""
    if start_date or end_date:
        if start_date:
            time_start = start_date[:10] + " 00:00:00"
        if end_date:
            time_end = end_date[:10] + " 23:59:59"
        time_desc = f"{start_date[:10] or '最早'} ~ {end_date[:10] or '至今'}"
    elif days > 0:
        now_cn = datetime.now(ZoneInfo("Asia/Shanghai"))
        start_cn = (now_cn - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        time_start = start_cn.strftime("%Y-%m-%d %H:%M:%S")
        time_end = now_cn.strftime("%Y-%m-%d %H:%M:%S")
        time_desc = f"近 {days} 天"
    status_in = re.sub(r"[^0-9,]", "", str(args.get("status_in") or "")).strip(",")
    _FIELD_CN = {"create": "创建", "submit": "提交审批", "update": "更新", "archive": "归档"}
    try:
        items: list[dict] = []
        page_token = ""
        pages = 0
        async with httpx.AsyncClient(timeout=20) as client:
            token = await zhishu_token(client)
            while pages < 5 and len(items) < max_items:
                data = await contract_search(
                    client, token, user_id=zsid, permission=permission,
                    contract_name=query,
                    time_field=time_field, time_start=time_start, time_end=time_end,
                    status_in=status_in,
                    page_size=min(50, max_items - len(items)), page_token=page_token,
                )
                pages += 1
                items.extend(data.get("items") or [])
                page_token = data.get("page_token") or ""
                if not data.get("has_more") or not page_token:
                    page_token = ""
                    break
        truncated = bool(page_token)  # 服务端说还有下一页（满 5 页或达到 max_items 但结果未完）
    except ZhishuApiError as e:
        return f"智书合同读取失败：{e}（稍后再试，或联系管理员确认凭证配置）"
    except Exception as e:
        return f"智书请求失败：{e!s}（稍后再试，不要无脑重试）"
    _audit_tool(ctx, "feishu.zhishu_contract_mine",
                {"permission": permission, "items": len(items), "has_query": bool(query),
                 "has_time": bool(time_start or time_end), "days": days, "pages": pages})
    label = _ZHISHU_PERM_CN.get(permission, str(permission))
    scope = f"名称含「{query}」" if query else ""
    time_part = f"（{time_desc}{_FIELD_CN[time_field]}）" if time_desc else ""
    header = f"你名下「{label}」{scope}的智书合同{time_part}共 {len(items)} 条"
    if truncated:
        header += "（已达单次上限，后面还有——缩小时间窗、加 query 关键词或用 status_in 过滤）"
    if not items:
        tips = []
        if query:
            tips.append("换个关键词试试（智书按合同名称子串匹配）")
        if time_desc:
            tips.append("放宽时间窗，或换 time_field（默认按提交审批时间，可切创建/更新/归档）")
        if not tips:
            return header + "。"
        return header + "。" + "；".join(tips) + "。"
    out = (header + "，素材如下，请提炼：需要签约/审批/付款跟进的、金额异常的、对方主体的：\n\n"
           + "\n".join(_zhishu_line(i) for i in items))
    if len(out) > _FEISHU_TEXT_MAX:
        out = out[:_FEISHU_TEXT_MAX] + "\n…[合同过多已截断，可缩小时间窗/加关键词重拉]"
    return out


# ---------- 定时任务工具（M3.10；无条件挂载；属主=当前用户 × 当前数字人，与界面"定时任务"tab 同表同源） ----------

SCHED_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "schedule_create",
            "description": "创建定时任务：到点系统自动以当前用户身份向你（当前数字人）发起一条指令并生成独立会话。用户要求「定时/每天/每周…做某事」时直接创建。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "任务名称，如「每日AI新闻简报」"},
                    "prompt": {"type": "string", "description": "到点要执行的完整指令（写具体，含输出要求）"},
                    "cron_expr": {
                        "type": "string",
                        "description": "5 段 cron（分 时 日 月 周）。例：每天13:30='30 13 * * *'；每周一9点='0 9 * * 1'；每2小时='0 */2 * * *'；每月1号8点='0 8 1 * *'",
                    },
                    "timezone": {"type": "string", "description": "IANA 时区，默认 Asia/Shanghai"},
                },
                "required": ["name", "prompt", "cron_expr"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_list",
            "description": "列出当前用户与你（当前数字人）名下的全部定时任务。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_update",
            "description": "修改定时任务：启用/停用或编辑名称、指令、cron、时区。先用 schedule_list 拿到任务 id。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "任务 id"},
                    "enabled": {"type": "boolean", "description": "true=启用，false=停用"},
                    "name": {"type": "string"},
                    "prompt": {"type": "string"},
                    "cron_expr": {"type": "string", "description": "5 段 cron（分 时 日 月 周）"},
                    "timezone": {"type": "string"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_delete",
            "description": "删除定时任务（不可恢复）。用户明确指示删除时直接执行。",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "integer", "description": "任务 id"}},
                "required": ["task_id"],
            },
        },
    },
]
SCHED_TOOL_NAMES = {s["function"]["name"] for s in SCHED_TOOL_SCHEMAS}

_SCHED_MAX_TASKS = 10  # 每 (用户, 数字人) 未删除任务上限（防 prompt 注入刷任务/刷额度）


def _fmt_local(dt: datetime | None, tz: str) -> str:
    if dt is None:
        return "—"
    return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")


async def _get_own_sched_task(ctx: "ToolContext", task_id: Any) -> ScheduledTask | str:
    """归属校验（当前用户 × 当前数字人）：查不到返回错误文案，不暴露他人物件存在性。"""
    if not isinstance(task_id, int) or isinstance(task_id, bool):
        return "task_id 必须是整数"
    t = (
        await ctx.db.execute(
            select(ScheduledTask).where(
                ScheduledTask.id == task_id,
                ScheduledTask.user_id == ctx.user.id,
                ScheduledTask.agent_id == ctx.agent.id,
                ScheduledTask.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    return t if t is not None else f"定时任务 #{task_id} 不存在"


async def _schedule_create(ctx: "ToolContext", args: dict[str, Any]) -> str:
    name = (args.get("name") or "").strip()
    prompt = (args.get("prompt") or "").strip()
    cron_expr = (args.get("cron_expr") or "").strip()
    tz = (args.get("timezone") or "Asia/Shanghai").strip()
    if not name or not prompt:
        return "name 和 prompt 不能为空"
    if len(name) > 128:
        return "任务名称过长（≤128 字符）"
    if not is_valid_cron(cron_expr):
        return f"cron 表达式无效：{cron_expr}（需要 5 段：分 时 日 月 周，如 30 13 * * * 表示每天13:30）"
    if not is_valid_tz(tz):
        return f"时区无效：{tz}"
    if getattr(ctx.agent, "default_model_id", None) is None:
        return "该数字人未配置默认模型，定时任务到点无法执行。请先在管理端为它设置默认模型（或在「定时任务」tab 手动建任务时指定模型）后重试"
    count = (
        await ctx.db.execute(
            select(func.count(ScheduledTask.id)).where(
                ScheduledTask.user_id == ctx.user.id,
                ScheduledTask.agent_id == ctx.agent.id,
                ScheduledTask.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    if count >= _SCHED_MAX_TASKS:
        return f"定时任务已达上限（{_SCHED_MAX_TASKS} 个），请先 schedule_list 查看并删除不需要的任务"

    t = ScheduledTask(
        user_id=ctx.user.id,
        agent_id=ctx.agent.id,
        name=name,
        prompt=prompt,
        cron_expr=cron_expr,
        timezone=tz,
        enabled=True,
        next_run_at=compute_next(cron_expr, tz, datetime.now(UTC)),
    )
    ctx.db.add(t)
    _audit_tool(ctx, "schedule.create", {"name": name, "cron_expr": cron_expr, "timezone": tz})
    await ctx.db.commit()  # tick 循环是独立 session，必须落库才会被调度（_run_code 同款 mid-turn commit）
    return (
        f"定时任务已创建：#{t.id}「{name}」，规则 {cron_expr}（{tz}），"
        f"下次触发 {_fmt_local(t.next_run_at, tz)}。用户可在对话页右侧「定时任务」tab 查看和管理。"
    )


async def _schedule_list(ctx: "ToolContext") -> str:
    rows = (
        (
            await ctx.db.execute(
                select(ScheduledTask)
                .where(
                    ScheduledTask.user_id == ctx.user.id,
                    ScheduledTask.agent_id == ctx.agent.id,
                    ScheduledTask.deleted_at.is_(None),
                )
                .order_by(ScheduledTask.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return "（当前没有定时任务）"
    status_map = {"running": "运行中", "ok": "成功", "error": "失败"}
    lines = []
    for t in rows:
        last = status_map.get(t.last_status or "", "未运行")
        if t.last_status == "error" and t.last_error:
            last += f"（{t.last_error[:80]}）"
        lines.append(
            f"#{t.id} {t.name} | {t.cron_expr}（{t.timezone}）| {'启用' if t.enabled else '停用'}"
            f" | 下次 {_fmt_local(t.next_run_at, t.timezone)} | 最近 {last} | 已跑 {t.run_count} 次"
        )
    return "\n".join(lines)


async def _schedule_update(ctx: "ToolContext", args: dict[str, Any]) -> str:
    t = await _get_own_sched_task(ctx, args.get("task_id"))
    if isinstance(t, str):
        return t

    # validate-then-swap：全部校验通过前不动 ORM 对象（scheduler/router.py update_task 同款语义）
    new_cron = (args["cron_expr"]).strip() if args.get("cron_expr") is not None else t.cron_expr
    new_tz = (args["timezone"]).strip() if args.get("timezone") is not None else t.timezone
    if not is_valid_cron(new_cron):
        return f"cron 表达式无效：{new_cron}（需要 5 段：分 时 日 月 周）"
    if not is_valid_tz(new_tz):
        return f"时区无效：{new_tz}"
    new_name = (args["name"]).strip() if args.get("name") is not None else t.name
    if not new_name or len(new_name) > 128:
        return "任务名称不能为空且 ≤128 字符"

    schedule_changed = new_cron != t.cron_expr or new_tz != t.timezone
    t.name = new_name
    if args.get("prompt") is not None:
        if not args["prompt"].strip():
            return "prompt 不能为空"
        t.prompt = args["prompt"]
    t.cron_expr = new_cron
    t.timezone = new_tz
    if args.get("enabled") is not None:
        t.enabled = bool(args["enabled"])
    # 调度变了，或从停用翻启用：从现在重算下次触发；停用则清空
    if schedule_changed or args.get("enabled") is True:
        t.next_run_at = compute_next(t.cron_expr, t.timezone, datetime.now(UTC)) if t.enabled else None
    elif args.get("enabled") is False:
        t.next_run_at = None
    _audit_tool(ctx, "schedule.update", {"task_id": t.id, "changes": {k: v for k, v in args.items() if k != "task_id"}})
    await ctx.db.commit()
    state = "启用" if t.enabled else "停用"
    return f"任务 #{t.id}「{t.name}」已更新（{state}，下次触发 {_fmt_local(t.next_run_at, t.timezone)}）"


async def _schedule_delete(ctx: "ToolContext", args: dict[str, Any]) -> str:
    t = await _get_own_sched_task(ctx, args.get("task_id"))
    if isinstance(t, str):
        return t
    t.deleted_at = utcnow()
    _audit_tool(ctx, "schedule.delete", {"task_id": t.id, "name": t.name})
    await ctx.db.commit()
    return f"定时任务 #{t.id}「{t.name}」已删除"


@dataclass
class ToolContext:
    db: AsyncSession
    user: User
    agent: Agent
    is_admin_master: bool = False  # agent.is_master 且 user 是 admin
    allow_code_exec: bool = False  # agent.allow_code_exec：挂载 run_code（M3 沙箱）
    allow_web_search: bool = False  # agent.allow_web_search：挂载 web_search/web_fetch（M3.9）
    allow_api_request: bool = False  # agent.allow_api_request：挂载 api_request（M4d 白名单外部 API）
    allow_feishu: bool = False  # agent.allow_feishu：挂载飞书内容读取（M7，user_access_token=用户本人视野）
    allow_zhishu: bool = False  # agent.allow_zhishu：挂载智书合同读取（M9c，tenant token + zhishu_user_id 过滤）
    allow_image_generate: bool = False  # agent.allow_image_generate：挂载 image_generate（M13a 文生图）
    # 本轮工具落盘并要挂到最终 assistant 消息 attachments 的文件 [{name, path, ws_path, mime}]（M13a；M20 补 ws_path）
    created_files: list[dict] = field(default_factory=list)


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": "在与当前用户的个人记忆中语义检索（跨会话的长期记忆、历史笔记）。回答依赖用户偏好/过往背景时先检索。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "检索查询"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_write",
            "description": "把值得跨会话记住的信息（用户偏好、重要事实、关键结论）写入今日笔记。不要记录闲聊。",
            "parameters": {
                "type": "object",
                "properties": {"content": {"type": "string", "description": "要记住的内容"}},
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_update_curated",
            "description": "整体重写长期策展记忆 MEMORY.md（长期有效的核心偏好/背景）。只在需要固化重要长期信息时使用。",
            "parameters": {
                "type": "object",
                "properties": {"content": {"type": "string", "description": "新的策展记忆全文"}},
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_append_curated",
            "description": (
                "往 MEMORY.md 的「用户偏好与约定」区增量追加一条长期生效的偏好/约定（增量追加，不影响其他内容）。"
                "触发时机：用户明确说「记住/以后都/一直要/别再做」这类长期指令时——这是固化通道，比 memory_write 的今日笔记更持久、每轮可见。"
                "纪律：一条一事、一两句话；记录前先看 MEMORY.md 是否已有同类条目，有则不重复记（要修改/废止旧约定时用 memory_update_curated 整体重写）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"content": {"type": "string", "description": "要固化的长期偏好/约定（一两句话）"}},
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_add_lesson",
            "description": (
                "往 MEMORY.md 的「经验教训」区追加一条从错误中提炼的教训（增量追加，不影响其他内容）。"
                "触发时机：用户纠正了你，或同一工具反复出错后找到了正确做法。"
                "纪律：只记可复现、可避免、未来还会遇到的教训；一次性的环境问题（网络抖动、外部系统故障）不记；"
                "记录前先看 MEMORY.md 里是否已有同类条目，有则不重复记。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scene": {"type": "string", "description": "什么任务/操作场景（如：BOM 规格换算、工作区文件读取）"},
                    "mistake": {"type": "string", "description": "做错了什么"},
                    "correction": {"type": "string", "description": "正确做法"},
                    "source": {"type": "string", "description": "可选：来源（用户纠正 / 工具报错 / 自己发现）"},
                },
                "required": ["scene", "mistake", "correction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill",
            "description": "读取某个 skill 的完整指令（SKILL.md）及其附件清单；带 path 参数可读取具体附件（如 scripts/x.py）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "skill 名称"},
                    "path": {"type": "string", "description": "可选：要读取的附件路径"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_create_draft",
            "description": (
                "把刚完成的做法沉淀为可复用的技能：保存为当前用户的个人技能（SKILL.md 形态），默认同时绑定到本数字人（下一轮对话起生效）。"
                "触发时机：用户说「沉淀成技能/以后都这么干/把这次的流程固化下来」，或刚完成一个有复用价值的流程且用户满意。"
                "内容纪律：写可复用的方法/步骤/口径/注意事项，不写一次性数据（具体数字、某次文件的内容）；"
                "name 冲突时换一个更具体的名称重试。只建个人技能，平台技能由管理员收录。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能名：小写字母/数字/中划线，字母或数字开头，≤64（如 weekly-report-draft）"},
                    "description": {"type": "string", "description": "一句话说明什么时候该用这个技能（会出现在技能名册里供意图匹配）"},
                    "content": {"type": "string", "description": "SKILL.md 正文：适用场景 + 操作步骤 + 注意事项/口径"},
                    "bind_to_self": {"type": "boolean", "description": "可选，默认 true：同时绑定到本数字人"},
                },
                "required": ["name", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_list",
            "description": "列出当前用户 workspace 中的文件。加 agent 参数可列出你名下其他数字人的 workspace 文件（只读，用于复用其他数字人的产出）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "description": "可选：你名下其他数字人的名字或 id；不传 = 本数字人"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_read",
            "description": "读取 workspace 中某个文件的内容（文本类文件）。加 agent 参数可读取你名下其他数字人的文件（只读）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名（可含子文件夹路径，如 reports/a.md）"},
                    "agent": {"type": "string", "description": "可选：你名下其他数字人的名字或 id；不传 = 本数字人"},
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_write",
            "description": "把文本内容写入 workspace 文件；路径可含子文件夹（自动创建），已存在则覆盖。用于保存你生成的产出（报告、脚本、整理后的数据等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径，如 reports/周报.md"},
                    "content": {"type": "string", "description": "文件全文"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_delete",
            "description": "删除 workspace 中的文件或整个文件夹（含其中所有文件）。删除不可恢复：用户已明确指示删除对象时视为已确认、直接执行并在完成后汇报；仅删除范围含糊时才先向用户确认范围。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件或文件夹的相对路径，如 reports 或 reports/a.md"},
                },
                "required": ["path"],
            },
        },
    },
]

# ---------- 主 agent 只读管控工具 ----------
MASTER_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_all_agents",
            "description": "列出平台全部数字人：模板与员工领用的实例（含属主、状态、是否主 agent）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_agent_file",
            "description": "读取任意数字人的 .md 人格文件（SOUL/AGENTS/MEMORY）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "integer", "description": "数字人 ID"},
                    "file_key": {"type": "string", "description": "SOUL / AGENTS / MEMORY"},
                },
                "required": ["agent_id", "file_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_agent_skills",
            "description": "查看任意数字人绑定的技能清单。",
            "parameters": {
                "type": "object",
                "properties": {"agent_id": {"type": "integer", "description": "数字人 ID"}},
                "required": ["agent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "usage_summary",
            "description": "用量统计：按用户或按模型聚合最近 N 天的 token 与费用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "统计最近几天，默认 7"},
                    "group_by": {"type": "string", "description": "user 或 model，默认 user"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_users",
            "description": "列出平台用户（id、姓名、角色、部门）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
MASTER_TOOL_NAMES = {s["function"]["name"] for s in MASTER_TOOL_SCHEMAS}


async def _read_skill(ctx: ToolContext, args: dict) -> str:
    db = ctx.db
    skills = await get_available_skills(db, ctx.agent, ctx.user)
    skill = next((s for s in skills if s.name == args["name"]), None)
    if skill is None:
        return f"未找到名为 {args['name']} 的 skill（可用列表见系统提示）"
    files = (
        (
            await db.execute(
                select(SkillFile).where(SkillFile.skill_id == skill.id, SkillFile.deleted_at.is_(None)).order_by(SkillFile.path)
            )
        )
        .scalars()
        .all()
    )
    path = args.get("path")
    if path:
        f = next((x for x in files if x.path == path), None)
        if f is None:
            return f"技能 {skill.name} 下没有附件 {path}（可用：{[x.path for x in files]}）"
        return f.content[:20000] or "（空文件）"
    main = next((x for x in files if x.path == "SKILL.md"), None)
    body = (main.content if main else skill.instructions) or "（该 skill 无指令内容）"
    attachments = [x.path for x in files if x.path != "SKILL.md" and not x.path.endswith("/.keep") and x.path != ".keep"]
    if attachments:
        body += "\n\n## 附件（用 read_skill 带 path 参数读取）\n" + "\n".join(f"- {p}" for p in attachments)
    return body


# ---------- M16 技能沉淀：对话内创建个人技能草稿 ----------

_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")  # 与 SkillIn 的 name 校验同规则
_SKILL_MD_MAX = 20000  # SKILL.md 上限：技能是指令不是数据仓库，超长说明把一次性内容写进去了


async def _skill_create_draft(ctx: ToolContext, args: dict) -> str:
    """数字人把做法沉淀为当前用户的个人技能（owner_user_id=当前用户），可选绑定到本数字人。

    权限边界：只能建「自己的个人技能」，归属锁死 ctx.user.id——与「我的技能」界面创建等价；
    绑定目标只能是当前对话的数字人本人（ctx.agent.id），不能碰他人的技能/数字人。
    """
    name = str(args.get("name") or "").strip().lower()
    content = str(args.get("content") or "").strip()
    description = str(args.get("description") or "").strip()[:512]
    if not name or len(name) > 64 or not _SKILL_NAME_RE.match(name):
        return "技能名不合法：需小写字母/数字/中划线、字母或数字开头、≤64 字符（如 weekly-report-draft），换一个名称重试"
    if not content:
        return "content 不能为空（SKILL.md 正文：适用场景 + 操作步骤 + 注意事项/口径）"
    if err := mem.scan_instructional_write(content, max_chars=_SKILL_MD_MAX):
        return f"未创建：{err}"
    dup = (await ctx.db.execute(select(Skill).where(Skill.name == name))).scalar_one_or_none()
    if dup is not None:  # 全局唯一（含平台技能与软删除占用），撞名让模型换名重试，不自动加后缀（名称即语义）
        return f"技能名「{name}」已被占用，请换一个更具体的名称重新调用"
    bind = args.get("bind_to_self", True)
    s = Skill(
        name=name, description=description, instructions=content, enabled=True,
        owner_user_id=ctx.user.id, created_by=ctx.user.id,
    )
    ctx.db.add(s)
    try:
        await ctx.db.flush()
        ctx.db.add(SkillFile(skill_id=s.id, path="SKILL.md", content=content))
        if bind:
            ctx.db.add(AgentSkill(agent_id=ctx.agent.id, skill_id=s.id))
        _audit_tool(ctx, "skill.create_draft", {"skill_id": s.id, "name": name, "bound": bool(bind)})
        await ctx.db.commit()
    except IntegrityError:  # 并发同名等极端情况兜底，不裸抛 500（同 create_my_skill）
        await ctx.db.rollback()
        return f"技能名「{name}」已被占用，请换一个名称重新调用"
    msg = f"已保存为个人技能「{name}」"
    msg += "，并绑定到本数字人（下一轮对话起生效）" if bind else "（未绑定到数字人）"
    return msg + "。告知用户：可在「技能库 → 我的技能」查看/修改；若有通用价值，可找管理员收录为平台技能。"


# ---------- 主 agent 工具实现（全部只读） ----------

async def _master_list_all_agents(db: AsyncSession) -> str:
    rows = (
        (
            await db.execute(
                select(Agent, User.name)
                .outerjoin(User, User.id == Agent.owner_user_id)
                .where(Agent.deleted_at.is_(None))
                .order_by(Agent.is_template.desc(), Agent.id.asc())
            )
        )
        .all()
    )
    if not rows:
        return "（平台上还没有数字人）"
    lines = []
    for a, owner_name in rows:
        kind = "模板" if a.is_template else f"实例（属主：{owner_name or a.owner_user_id}）"
        master = "，主 agent" if a.is_master else ""
        lines.append(f"- #{a.id} {a.name}｜{kind}{master}｜状态 {a.status}｜{a.description or '无描述'}")
    return "\n".join(lines)


async def _master_read_agent_file(db: AsyncSession, args: dict) -> str:
    if args.get("file_key") not in ("SOUL", "AGENTS", "MEMORY"):
        return "file_key 必须是 SOUL / AGENTS / MEMORY 之一"
    f = (
        await db.execute(
            select(AgentFile).where(
                AgentFile.agent_id == args["agent_id"], AgentFile.file_key == args["file_key"], AgentFile.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if f is None:
        return "该数字人或文件不存在"
    return f.content or "（空文件）"


async def _master_get_agent_skills(db: AsyncSession, args: dict) -> str:
    rows = (
        (
            await db.execute(
                select(Skill.name, Skill.description, Skill.category)
                .join(AgentSkill, AgentSkill.skill_id == Skill.id)
                .where(AgentSkill.agent_id == args["agent_id"], AgentSkill.deleted_at.is_(None), Skill.deleted_at.is_(None))
            )
        )
        .all()
    )
    if not rows:
        return "（未绑定任何技能）"
    return "\n".join(f"- {n}（{c or '未分类'}）: {d}" for n, d, c in rows)


async def _master_usage_summary(db: AsyncSession, args: dict) -> str:
    days = min(int(args.get("days") or 7), 90)
    since = datetime.now(UTC) - timedelta(days=days)
    if args.get("group_by") == "model":
        label_col = Model.display_name
        join = (Model, Model.id == UsageRecord.model_id)
    else:
        label_col = User.name
        join = (User, User.id == UsageRecord.user_id)
    rows = (
        (
            await db.execute(
                select(
                    label_col,
                    func.count(UsageRecord.id),
                    func.sum(UsageRecord.input_tokens),
                    func.sum(UsageRecord.output_tokens),
                    func.sum(UsageRecord.cost),
                )
                .join(*join)
                .where(UsageRecord.created_at >= since)
                .group_by(label_col)
                .order_by(func.sum(UsageRecord.cost).desc())
                .limit(50)
            )
        )
        .all()
    )
    if not rows:
        return f"（最近 {days} 天无用量记录）"
    lines = [f"最近 {days} 天用量（按{'模型' if args.get('group_by') == 'model' else '用户'}）："]
    for name, cnt, tin, tout, cost in rows:
        lines.append(f"- {name}：{cnt} 次调用，输入 {tin or 0} / 输出 {tout or 0} tokens，费用 ¥{float(cost or 0):.4f}")
    return "\n".join(lines)


async def _master_list_users(db: AsyncSession) -> str:
    rows = (
        (await db.execute(select(User).where(User.deleted_at.is_(None)).order_by(User.id.asc()))).scalars().all()
    )
    return "\n".join(
        f"- #{u.id} {u.name or u.username}（{u.role}{'，部门 #' + str(u.department_id) if u.department_id else ''}）" for u in rows
    ) or "（无用户）"


async def _resolve_peer_agent(ctx: "ToolContext", raw: Any) -> tuple[Any, str | None]:
    """解析 workspace_list/read 的可选 agent 参数（M5 跨数字人只读共享）。

    空 = 本数字人；纯数字 = 按 id；其余按名字精确匹配。只认本用户名下的 active 实例。
    返回 (agent, None) 或 (None, 报错文案)。
    """
    if raw is None or str(raw).strip() == "":
        return ctx.agent, None
    s = str(raw).strip()
    rows = (
        (
            await ctx.db.execute(
                select(Agent).where(
                    Agent.owner_user_id == ctx.user.id,
                    Agent.deleted_at.is_(None),
                    Agent.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    if s.isdigit():
        hit = next((a for a in rows if a.id == int(s)), None)
        if hit is None:
            return None, f"你名下没有 id={s} 的可用数字人（只能读你自己的数字人的 workspace）"
        return hit, None
    hits = [a for a in rows if a.name == s]
    if not hits:
        names = "、".join(f"{a.name}（id {a.id}）" for a in rows) or "（无）"
        return None, f"找不到名为「{s}」的数字人。你名下可用的数字人：{names}"
    if len(hits) > 1:
        ids = "、".join(str(a.id) for a in hits)
        return None, f"有多个同名数字人「{s}」（id：{ids}），请改用 id 指定"
    return hits[0], None


async def dispatch_tool(ctx: ToolContext, name: str, args: dict) -> str:
    db = ctx.db
    # 代码执行工具：双保险（schema 层不挂载 + 这里再校验）
    if name in CODE_TOOL_NAMES:
        if not ctx.allow_code_exec:
            return "该数字人未开通代码执行能力（可联系管理员在数字人设置中开启）"
        if name == "run_code":
            return await _run_code(ctx, args)
    # 联网工具：双保险（schema 层不挂载 + 这里再校验）
    if name in WEB_TOOL_NAMES:
        if not ctx.allow_web_search:
            return "该数字人未开通联网搜索能力（可联系管理员在数字人设置中开启）"
        if name == "web_search":
            return await _web_search(ctx, args)
        if name == "web_fetch":
            return await _web_fetch(ctx, args)
    # 外部 API 工具：双保险（schema 层不挂载 + 这里再校验）
    if name in API_TOOL_NAMES:
        if not ctx.allow_api_request:
            return "该数字人未开通外部 API 调用能力（可联系管理员在数字人设置中开启）"
        if name == "api_request":
            return await _api_request(ctx, args)
    # 飞书内容工具：双保险（schema 层不挂载 + 这里再校验）
    if name in FEISHU_TOOL_NAMES:
        if not ctx.allow_feishu:
            return "该数字人未开通飞书内容读取能力（可联系管理员在数字人设置中开启）"
        if name == "feishu_doc_read":
            return await _feishu_doc_read(ctx, args)
        if name == "feishu_folder_list":
            return await _feishu_folder_list(ctx, args)
        if name == "feishu_file_upload":
            return await _feishu_file_upload(ctx, args)
        if name == "feishu_doc_append":
            return await _feishu_doc_append(ctx, args)
        if name == "feishu_doc_create":
            return await _feishu_doc_create(ctx, args)
        if name == "feishu_sheet_create":
            return await _feishu_sheet_create(ctx, args)
        if name == "feishu_bitable_create":
            return await _feishu_bitable_create(ctx, args)
        if name == "feishu_doc_delete_tail":
            return await _feishu_doc_delete_tail(ctx, args)
        if name == "feishu_doc_edit":
            return await _feishu_doc_edit(ctx, args)
        if name == "feishu_sheet_write":
            return await _feishu_sheet_write(ctx, args)
        if name == "feishu_bitable_write":
            return await _feishu_bitable_write(ctx, args)
        if name == "feishu_bitable_field":
            return await _feishu_bitable_field(ctx, args)
        if name == "feishu_doc_search":
            return await _feishu_doc_search(ctx, args)
        if name == "feishu_chat_search":
            return await _feishu_chat_search(ctx, args)
        if name == "feishu_chat_history":
            return await _feishu_chat_history(ctx, args)
        if name == "feishu_chat_daily":
            return await _feishu_chat_daily(ctx, args)
        if name == "feishu_approval_daily":
            return await _feishu_approval_daily(ctx, args)
        if name == "feishu_approval_detail":
            return await _feishu_approval_detail(ctx, args)
        if name == "feishu_mail_daily":
            return await _feishu_mail_daily(ctx, args)
        if name == "feishu_mail_search":
            return await _feishu_mail_search(ctx, args)
    # 智书合同工具：双保险（schema 层不挂载 + 这里再校验）
    if name in ZHISHU_TOOL_NAMES:
        if not ctx.allow_zhishu:
            return "该数字人未开通智书合同读取能力（可联系管理员在数字人设置中开启）"
        if name == "zhishu_contract_mine":
            return await _zhishu_contract_mine(ctx, args)
    # 文生图工具（M13a）：双保险（schema 层不挂载 + 这里再校验）
    if name in IMG_TOOL_NAMES:
        if not ctx.allow_image_generate:
            return "该数字人未开通文生图能力（可联系管理员在数字人设置中开启）"
        if name == "image_generate":
            return await _image_generate(ctx, args)
    # 定时任务工具：无条件挂载，归属锁死 当前用户 × 当前数字人
    if name == "schedule_create":
        return await _schedule_create(ctx, args)
    if name == "schedule_list":
        return await _schedule_list(ctx)
    if name == "schedule_update":
        return await _schedule_update(ctx, args)
    if name == "schedule_delete":
        return await _schedule_delete(ctx, args)
    # 主 agent 只读工具：双保险（schema 层不挂载 + 这里再校验）
    if name in MASTER_TOOL_NAMES:
        if not ctx.is_admin_master:
            return "该工具仅平台管理员可用"
        if name == "list_all_agents":
            return await _master_list_all_agents(db)
        if name == "read_agent_file":
            return await _master_read_agent_file(db, args)
        if name == "get_agent_skills":
            return await _master_get_agent_skills(db, args)
        if name == "usage_summary":
            return await _master_usage_summary(db, args)
        if name == "list_users":
            return await _master_list_users(db)

    if name == "memory_search":
        hits = await mem.memory_search(db, agent_id=ctx.agent.id, user_id=ctx.user.id, query=args["query"])
        return "\n---\n".join(hits) if hits else "（未检索到相关记忆）"
    if name == "memory_write":
        return await mem.memory_write(db, agent_id=ctx.agent.id, user_id=ctx.user.id, content=args["content"])
    if name == "memory_update_curated":
        return await mem.memory_update_curated(db, agent_id=ctx.agent.id, user_id=ctx.user.id, content=args["content"])
    if name == "memory_append_curated":
        return await mem.memory_append_curated(db, agent_id=ctx.agent.id, content=args["content"])
    if name == "memory_add_lesson":
        return await mem.memory_add_lesson(
            db,
            agent_id=ctx.agent.id,
            scene=args["scene"],
            mistake=args["mistake"],
            correction=args["correction"],
            source=args.get("source", ""),
        )
    if name == "read_skill":
        return await _read_skill(ctx, args)
    if name == "skill_create_draft":
        return await _skill_create_draft(ctx, args)
    if name == "workspace_list":
        target, err = await _resolve_peer_agent(ctx, args.get("agent"))
        if err:
            return err
        files = (
            (
                await db.execute(
                    select(WorkspaceFile.filename, WorkspaceFile.size).where(
                        WorkspaceFile.agent_id == target.id,
                        WorkspaceFile.user_id == ctx.user.id,
                        WorkspaceFile.deleted_at.is_(None),
                    )
                )
            )
            .all()
        )
        header = "" if target.id == ctx.agent.id else f"（这是「{target.name}」的 workspace，只读）\n"
        return header + ("\n".join(f"- {f}（{s} 字节）" for f, s in files) if files else "（workspace 为空）")
    if name == "workspace_read":
        target, err = await _resolve_peer_agent(ctx, args.get("agent"))
        if err:
            return err
        f = (
            await db.execute(
                select(WorkspaceFile).where(
                    WorkspaceFile.agent_id == target.id,
                    WorkspaceFile.user_id == ctx.user.id,
                    WorkspaceFile.filename == args["filename"],
                    WorkspaceFile.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        scope = "" if target.id == ctx.agent.id else f"（在「{target.name}」的 workspace 中）"
        if f is None:
            return f"文件不存在{scope}"
        if not f.content_text:
            return "该文件不是文本类文件，暂不支持读取内容（二进制文件请到对话页下载，或让对应数字人先导出为文本/表格摘要）"
        if target.id != ctx.agent.id:
            _audit_tool(ctx, "workspace.read_peer", {"from_agent_id": target.id, "from_agent_name": target.name, "filename": f.filename})
        return f.content_text[:20000]
    if name == "workspace_write":
        return await _workspace_write(ctx, args)
    if name == "workspace_delete":
        return await _workspace_delete(ctx, args)
    return f"未知工具: {name}"
