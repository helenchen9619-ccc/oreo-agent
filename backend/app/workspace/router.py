"""workspace 文件：按 (user, agent) 隔离。拖文件进对话即上传到这里。

路径支持子文件夹：filename 存相对路径（如 reports/周会.md）。
二进制文件（xlsx/docx）在上传与预览时做文本抽取，供预览和 agent 读取。
"""
import io
import json
import mimetypes
import re
import shutil
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Form, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.agent import Agent, WorkspaceFile, WorkspaceFolderPref, WorkspaceShare
from app.models.base import utcnow
from app.models.user import AuditLog, User
from app.workspace.docx_export import DOCX_MIME, md_to_docx
from app.workspace.paths import INVALID_HINT, valid_rel

router = APIRouter(prefix="/api/agents/{agent_id}/workspace", tags=["workspace"])
settings = get_settings()

DATA_DIR = Path(getattr(settings, "DATA_DIR", "data"))
TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".jsx", ".tsx", ".xml", ".yaml", ".yml", ".log", ".html", ".css", ".sql", ".toml", ".ini", ".env"}
MAX_SIZE = 100 * 1024 * 1024    # 100MB（百万行 Excel 约 20-50MB）
MAX_TEXT_EXTRACT = 50_000         # 文本抽取上限

# M21：校验器唯一实现迁到 app.workspace.paths（白名单→按段黑名单）；别名供 shares.py/测试引用
_valid_rel = valid_rel


def guess_mime(filename: str, fallback: str | None = None) -> str:
    """按扩展名猜 mime；浏览器/multipart 没给或只给 octet-stream 时用（否则图片/PDF 等无法预览）。"""
    if fallback and fallback != "application/octet-stream":
        return fallback
    return mimetypes.guess_type(filename)[0] or fallback or "application/octet-stream"


def _extract_xlsx(raw: bytes) -> str | None:
    """xlsx → 每个 sheet 前 200 行的 TSV 文本（openpyxl 只读模式）。"""
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception:
        return None
    parts: list[str] = []
    total = 0
    for ws in wb.worksheets:
        lines = [f"# sheet: {ws.title}"]
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= 200:
                lines.append("…（仅前 200 行）")
                break
            lines.append("\t".join("" if c is None else str(c) for c in row))
        text = "\n".join(lines)
        parts.append(text)
        total += len(text)
        if total >= MAX_TEXT_EXTRACT:
            break
    wb.close()
    return "\n\n".join(parts)[:MAX_TEXT_EXTRACT]


def _extract_docx(raw: bytes) -> str | None:
    """docx → document.xml 去标签后的纯文本（docx 本质是 zip）。"""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    except Exception:
        return None
    xml = re.sub(r"</w:p>", "\n", xml)  # 段落换行
    text = re.sub(r"<[^>]+>", "", xml)  # 去标签
    return text.strip()[:MAX_TEXT_EXTRACT] or None


def _extract_pptx(raw: bytes) -> str | None:
    """pptx → 逐页 slide XML 去标签后的纯文本（pptx 本质是 zip；视觉渲染无轻量方案，先保内容可读）。"""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            slides = sorted(
                (n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[-1]).group()),
            )
            if not slides:
                return None
            parts: list[str] = []
            total = 0
            for i, name in enumerate(slides, 1):
                xml = z.read(name).decode("utf-8", errors="replace")
                text = re.sub(r"<[^>]+>", "\n", xml)  # 每个 run 换行，尽量保留阅读顺序
                text = re.sub(r"\n{2,}", "\n", text).strip()
                if not text:
                    continue
                part = f"# 第 {i} 页\n{text}"
                parts.append(part)
                total += len(part)
                if total >= MAX_TEXT_EXTRACT:
                    break
    except Exception:
        return None
    return "\n\n".join(parts)[:MAX_TEXT_EXTRACT] or None


def _extract_text(filename: str, raw: bytes) -> str | None:
    ext = Path(filename).suffix.lower()
    if ext == ".xlsx":
        return _extract_xlsx(raw)
    if ext == ".docx":
        return _extract_docx(raw)
    if ext == ".pptx":
        return _extract_pptx(raw)
    if ext not in TEXT_EXTS:
        return None
    try:
        # errors=replace 只修非法编码，不会移除 NUL；PostgreSQL text 禁止 0x00
        return raw.decode("utf-8", errors="replace").replace("\x00", "")[:MAX_TEXT_EXTRACT]
    except Exception:
        return None


@router.get("")
async def list_files(agent_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (
        (
            await db.execute(
                select(WorkspaceFile).where(
                    WorkspaceFile.agent_id == agent_id, WorkspaceFile.user_id == user.id, WorkspaceFile.deleted_at.is_(None)
                ).order_by(WorkspaceFile.pinned.desc(), WorkspaceFile.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return [{"id": f.id, "filename": f.filename, "size": f.size, "mime": f.mime, "pinned": f.pinned, "has_text": bool(f.content_text)} for f in rows]


@router.post("", status_code=201)
async def upload_file(agent_id: int, file: UploadFile, folder: str = Form(""), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    raw = await file.read()
    if len(raw) > MAX_SIZE:
        raise HTTPException(400, "文件超过 100MB 限制")
    folder = folder.strip().strip("/")
    if folder and not _valid_rel(folder):
        raise HTTPException(400, "目标文件夹路径不合法")
    name = Path(file.filename or "unnamed").name  # 防路径穿越
    # M21：文件名本身也校验（此前只验 folder，ERP 导出的「物品导出清单-总部 (16).xlsx」
    # 进得来却被工具层拦死——现在空格/括号合法，真正的坏名字（\ 控制字符 保留名）在上传瞬间说清楚）
    if not _valid_rel(name):
        raise HTTPException(400, f"文件{INVALID_HINT}")
    rel = f"{folder}/{name}" if folder else name
    folder_path = DATA_DIR / "workspaces" / str(user.id) / str(agent_id) / folder if folder else DATA_DIR / "workspaces" / str(user.id) / str(agent_id)
    folder_path.mkdir(parents=True, exist_ok=True)
    # 同名加序号
    target = folder_path / name
    i = 1
    while target.exists():
        target = folder_path / f"{Path(name).stem}_{i}{Path(name).suffix}"
        i += 1
    rel = f"{folder}/{target.name}" if folder else target.name
    target.write_bytes(raw)
    rec = WorkspaceFile(
        user_id=user.id,
        agent_id=agent_id,
        filename=rel,
        mime=guess_mime(target.name, file.content_type),
        size=len(raw),
        storage_path=str(target),
        content_text=_extract_text(target.name, raw),
    )
    db.add(rec)
    await db.commit()
    return {"id": rec.id, "filename": rec.filename, "size": rec.size, "has_text": bool(rec.content_text)}


class WriteFileIn(BaseModel):
    path: str = Field(min_length=1, max_length=256)
    content: str = ""


@router.put("/file")
async def write_text_file(agent_id: int, body: WriteFileIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """用户侧写文本文件（含新建子文件夹路径）；已存在则覆盖。"""
    path = body.path.strip().lstrip("/")
    if not _valid_rel(path):
        raise HTTPException(400, INVALID_HINT)
    root = (DATA_DIR / "workspaces" / str(user.id) / str(agent_id)).resolve()
    target = (root / path).resolve()
    if root not in target.parents:
        raise HTTPException(400, "路径越界")
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = body.content.encode("utf-8")
    if len(raw) > MAX_SIZE:
        raise HTTPException(400, "内容超过 100MB 限制")
    target.write_bytes(raw)
    rec = (
        await db.execute(
            select(WorkspaceFile).where(
                WorkspaceFile.user_id == user.id, WorkspaceFile.agent_id == agent_id, WorkspaceFile.filename == path
            )
        )
    ).scalar_one_or_none()
    if rec is None:
        rec = WorkspaceFile(
            user_id=user.id, agent_id=agent_id, filename=path,
            mime="text/plain", size=len(raw), storage_path=str(target),
            content_text=_extract_text(target.name, raw),
        )
        db.add(rec)
    else:
        rec.size = len(raw)
        rec.storage_path = str(target)
        rec.content_text = _extract_text(target.name, raw)
        rec.deleted_at = None
    await db.commit()
    return {"id": rec.id, "filename": rec.filename, "size": rec.size}


@router.delete("/folder", status_code=200)
async def delete_folder(agent_id: int, path: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """删除文件夹：软删其下全部文件记录 + 删磁盘目录。"""
    path = path.strip().strip("/")
    if not _valid_rel(path):
        raise HTTPException(400, "路径不合法")
    rows = (
        (
            await db.execute(
                select(WorkspaceFile).where(
                    WorkspaceFile.agent_id == agent_id,
                    WorkspaceFile.user_id == user.id,
                    WorkspaceFile.deleted_at.is_(None),
                    (WorkspaceFile.filename == path) | (WorkspaceFile.filename.startswith(path + "/")),
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        raise HTTPException(404, "文件夹不存在或为空")
    for f in rows:
        f.deleted_at = utcnow()
        try:
            Path(f.storage_path).unlink(missing_ok=True)
        except OSError:
            pass
    # M36：文件夹没了，其置顶/排序偏好一并软删（逐行改字段，不用 db.delete()）
    prefs = (
        (
            await db.execute(
                select(WorkspaceFolderPref).where(
                    WorkspaceFolderPref.agent_id == agent_id,
                    WorkspaceFolderPref.user_id == user.id,
                    WorkspaceFolderPref.deleted_at.is_(None),
                    (WorkspaceFolderPref.folder_path == path) | (WorkspaceFolderPref.folder_path.startswith(path + "/")),
                )
            )
        )
        .scalars()
        .all()
    )
    for r in prefs:
        r.deleted_at = utcnow()
    root = (DATA_DIR / "workspaces" / str(user.id) / str(agent_id)).resolve()
    target = (root / path).resolve()
    if root in target.parents:
        shutil.rmtree(target, ignore_errors=True)
    await db.commit()
    return {"deleted": len(rows)}


# ---------- M36：文件夹偏好（置顶/手动排序，服务端存跨设备同步）+ 文件夹重命名 ----------
# 纪律：prefs 清理/改写一律逐行改字段（软删置 deleted_at），不用 db.delete() 语句——
# uq_ws_folder_pref 唯一约束含软删行，且测试 _FakeDB 的 delete() 占坑。


def _all_folder_pref_rows(agent_id: int, user_id: int):
    """该 (user, agent) 全部 pref 行（含软删——唯一约束不管软删态，复活/冲突判定都要全量）。"""
    return select(WorkspaceFolderPref).where(
        WorkspaceFolderPref.agent_id == agent_id,
        WorkspaceFolderPref.user_id == user_id,
    )


@router.get("/folder-prefs")
async def list_folder_prefs(agent_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (
        (
            await db.execute(
                _all_folder_pref_rows(agent_id, user.id)
                .where(WorkspaceFolderPref.deleted_at.is_(None))
                .order_by(WorkspaceFolderPref.sort_order, WorkspaceFolderPref.id)
            )
        )
        .scalars()
        .all()
    )
    return [{"path": r.folder_path, "pinned": r.pinned, "sort_order": r.sort_order} for r in rows]


class FolderPrefItem(BaseModel):
    path: str = Field(min_length=1, max_length=256)
    pinned: bool | None = None
    sort_order: int | None = None


class FolderPrefsPut(BaseModel):
    """置顶切换（单项 {path, pinned}）与拖拽落盘（兄弟组全量 {path, sort_order}）共用一个 upsert 端点。"""
    items: list[FolderPrefItem] = Field(max_length=200)


@router.put("/folder-prefs")
async def put_folder_prefs(agent_id: int, body: FolderPrefsPut, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    cleaned: list[tuple[str, FolderPrefItem]] = []
    for it in body.items:
        p = it.path.strip().strip("/")
        if not p or not _valid_rel(p):
            raise HTTPException(400, "路径不合法")
        cleaned.append((p, it))
    by_path = {r.folder_path: r for r in (await db.execute(_all_folder_pref_rows(agent_id, user.id))).scalars().all()}
    for p, it in cleaned:
        row = by_path.get(p)
        if row is None:
            row = WorkspaceFolderPref(user_id=user.id, agent_id=agent_id, folder_path=p)
            db.add(row)
            by_path[p] = row
        else:
            row.deleted_at = None  # 软删复活（uq 约束含软删行，不能另行新增）
        if it.pinned is not None:
            row.pinned = it.pinned
        if it.sort_order is not None:
            row.sort_order = it.sort_order
    await db.commit()
    return {"saved": len(cleaned)}


class FolderPatch(BaseModel):
    """M36：文件夹重命名（只改末段名，不做跨层移动）。"""
    path: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=256)


@router.patch("/folder")
async def rename_folder(agent_id: int, body: FolderPatch, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """文件夹重命名：文件记录 filename/storage_path 前缀改写 + 磁盘目录先行移动 +
    WorkspaceShare.paths 与 folder_prefs 前缀同步改写（不改写则已发出的共享静默失效）。"""
    from app.workspace.shares import normalize_paths  # 延迟导入防循环（shares 依赖本模块）

    path = body.path.strip().strip("/")
    if not _valid_rel(path):
        raise HTTPException(400, "路径不合法")
    name = body.name.strip()
    if "/" in name or not _valid_rel(name):
        raise HTTPException(400, f"文件夹{INVALID_HINT}")
    parent, _, _ = path.rpartition("/")
    new_path = f"{parent}/{name}" if parent else name
    if new_path == path:
        return {"renamed": 0, "path": path}  # 幂等 no-op

    root = (DATA_DIR / "workspaces" / str(user.id) / str(agent_id)).resolve()
    src_dir = (root / path).resolve()
    dst_dir = (root / new_path).resolve()
    if root not in src_dir.parents or root not in dst_dir.parents:
        raise HTTPException(400, "路径越界")
    # 冲突 → 409（不做文件式的 _1 自动后缀：文件夹合并语义危险，明确报错让用户改名）
    clash = (
        (
            await db.execute(
                select(WorkspaceFile).where(
                    WorkspaceFile.agent_id == agent_id,
                    WorkspaceFile.user_id == user.id,
                    WorkspaceFile.deleted_at.is_(None),
                    (WorkspaceFile.filename == new_path) | (WorkspaceFile.filename.startswith(new_path + "/")),
                )
            )
        )
        .scalars()
        .all()
    )
    if dst_dir.exists() or clash:
        raise HTTPException(409, "已存在同名文件夹")
    rows = (
        (
            await db.execute(
                select(WorkspaceFile).where(
                    WorkspaceFile.agent_id == agent_id,
                    WorkspaceFile.user_id == user.id,
                    WorkspaceFile.deleted_at.is_(None),
                    (WorkspaceFile.filename == path) | (WorkspaceFile.filename.startswith(path + "/")),
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        raise HTTPException(404, "文件夹不存在或为空")

    disk_moved = False
    if src_dir.is_dir():
        src_dir.rename(dst_dir)  # 磁盘先行：失败时 DB 尚未脏
        disk_moved = True
    try:
        for f in rows:
            f.filename = new_path + f.filename[len(path):]
            f.storage_path = str((root / f.filename).resolve())  # 重新拼路径，杜绝字符串替换误伤前缀
        # 共享 paths 前缀改写（paths 是字面路径字符串、startswith 匹配，不改写会静默失效）
        shares = (
            (
                await db.execute(
                    select(WorkspaceShare).where(
                        WorkspaceShare.owner_user_id == user.id,
                        WorkspaceShare.agent_id == agent_id,
                        WorkspaceShare.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for s in shares:
            try:
                paths = json.loads(s.paths or "[]")
            except (TypeError, ValueError):
                continue
            changed = False
            new_paths: list[str] = []
            for p in paths:
                if p == path:
                    new_paths.append(new_path)
                    changed = True
                elif p.startswith(path + "/"):
                    new_paths.append(new_path + p[len(path):])
                    changed = True
                else:
                    new_paths.append(p)
            if changed:
                s.paths = json.dumps(normalize_paths(new_paths), ensure_ascii=False)
        # folder_prefs 前缀改写；目标路径已被别的 pref（含软删残留）占用时让位软删，避免撞 uq
        all_prefs = (await db.execute(_all_folder_pref_rows(agent_id, user.id))).scalars().all()
        occupied = {r.folder_path for r in all_prefs}
        for r in all_prefs:
            if r.deleted_at is not None:
                continue  # 软删行只参与占位判定，不改写
            if r.folder_path == path or r.folder_path.startswith(path + "/"):
                new_fp = new_path + r.folder_path[len(path):]
                occupied.discard(r.folder_path)
                if new_fp in occupied:
                    r.deleted_at = utcnow()  # 目标已有 pref，让位
                else:
                    r.folder_path = new_fp
                    occupied.add(new_fp)
        db.add(AuditLog(actor_user_id=user.id, action="workspace.folder_rename", target_type="workspace_folder",
                        target_id=path, detail=json.dumps({"path": path, "new_path": new_path, "files": len(rows)}, ensure_ascii=False)[:4000]))
        await db.commit()
    except Exception:
        if disk_moved:
            try:
                dst_dir.rename(src_dir)  # best-effort 回滚磁盘
            except OSError:
                pass
        raise
    return {"renamed": len(rows), "path": new_path}


@router.get("/{file_id}")
async def get_file(agent_id: int, file_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """单文件详情（含文本内容，供预览）。xlsx/docx 等二进制文件首次预览时懒抽取文本。"""
    f = (
        await db.execute(
            select(WorkspaceFile).where(
                WorkspaceFile.id == file_id, WorkspaceFile.agent_id == agent_id,
                WorkspaceFile.user_id == user.id, WorkspaceFile.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if f is None:
        raise HTTPException(404, "文件不存在")
    if not f.content_text and Path(f.filename).suffix.lower() in (".xlsx", ".docx", ".pptx"):
        try:
            raw = Path(f.storage_path).read_bytes()
        except OSError:
            raw = b""
        text = _extract_text(f.filename, raw) if raw else None
        if text:
            f.content_text = text
            await db.commit()
    return {"id": f.id, "filename": f.filename, "size": f.size, "mime": f.mime, "content_text": f.content_text}


@router.get("/{file_id}/raw")
async def get_file_raw(agent_id: int, file_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> FileResponse:
    """原始文件下载（前端 xlsx 渲染等需要真实字节）。"""
    f = (
        await db.execute(
            select(WorkspaceFile).where(
                WorkspaceFile.id == file_id, WorkspaceFile.agent_id == agent_id,
                WorkspaceFile.user_id == user.id, WorkspaceFile.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if f is None:
        raise HTTPException(404, "文件不存在")
    p = Path(f.storage_path)
    if not p.is_file():
        raise HTTPException(404, "文件已从磁盘移除")
    return FileResponse(p, filename=p.name, media_type=f.mime or "application/octet-stream")


def _export_md_as_docx(f: WorkspaceFile) -> Response:
    """.md → .docx 导出（「导出为 Word」按钮）。同事不认识 .md，Word 才是可交付格式。"""
    if Path(f.filename).suffix.lower() not in (".md", ".markdown"):
        raise HTTPException(400, "仅支持 Markdown 文件导出 Word")
    p = Path(f.storage_path)
    if not p.is_file():
        raise HTTPException(404, "文件已从磁盘移除")
    text = p.read_bytes().decode("utf-8", errors="replace")
    name = Path(f.filename).stem + ".docx"
    return Response(
        content=md_to_docx(text),
        media_type=DOCX_MIME,
        headers={"Content-Disposition": f"attachment; filename*=utf-8''{quote(name)}"},
    )


@router.get("/{file_id}/export.docx")
async def export_file_docx(agent_id: int, file_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Response:
    f = (
        await db.execute(
            select(WorkspaceFile).where(
                WorkspaceFile.id == file_id, WorkspaceFile.agent_id == agent_id,
                WorkspaceFile.user_id == user.id, WorkspaceFile.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if f is None:
        raise HTTPException(404, "文件不存在")
    return _export_md_as_docx(f)


# ---------- 转交给同用户的其他数字人（M19） ----------


def copy_file_to_agent(db: AsyncSession, *, user: User, src: WorkspaceFile, target_agent) -> WorkspaceFile:
    """把 src 文件复制进同一用户的另一个数字人工作区（转交 / 共享另存共用）。
    保留目录结构，重名自动加 _1 序号（与上传一致）；content_text 直接继承不重抽取。"""
    raw = Path(src.storage_path).read_bytes()
    rel = src.filename
    root = DATA_DIR / "workspaces" / str(user.id) / str(target_agent.id)
    parent = Path(rel).parent.as_posix()
    prefix = "" if parent == "." else parent + "/"
    stem, suffix = Path(rel).stem, Path(rel).suffix
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    i = 1
    while target.exists():
        target = root / f"{prefix}{stem}_{i}{suffix}"
        i += 1
    target.write_bytes(raw)
    rel_final = target.relative_to(root).as_posix()
    rec = WorkspaceFile(
        user_id=user.id,
        agent_id=target_agent.id,
        filename=rel_final,
        mime=guess_mime(target.name, src.mime),
        size=len(raw),
        storage_path=str(target),
        content_text=src.content_text,
    )
    db.add(rec)
    return rec


async def _resolve_transfer_target(db: AsyncSession, user: User, current_agent_id: int, target_agent_id: int):
    if target_agent_id == current_agent_id:
        raise HTTPException(400, "文件已在该数字人工作区")
    t = (
        await db.execute(
            select(Agent).where(
                Agent.id == target_agent_id, Agent.owner_user_id == user.id,
                Agent.deleted_at.is_(None), Agent.status == "active",
            )
        )
    ).scalar_one_or_none()
    if t is None:
        raise HTTPException(400, "目标数字人不存在或不属于你")
    return t


def _audit_transfer(db: AsyncSession, user: User, detail: dict) -> None:
    db.add(AuditLog(actor_user_id=user.id, action="workspace.transfer", target_type="workspace_file",
                    target_id=str(detail.get("file_id", "")), detail=json.dumps(detail, ensure_ascii=False)[:4000]))


class TransferIn(BaseModel):
    target_agent_id: int


@router.post("/{file_id}/transfer", status_code=201)
async def transfer_file(agent_id: int, file_id: int, body: TransferIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """把本工作区的文件转交（复制）给同用户的另一个数字人。"""
    f = (
        await db.execute(
            select(WorkspaceFile).where(
                WorkspaceFile.id == file_id, WorkspaceFile.agent_id == agent_id,
                WorkspaceFile.user_id == user.id, WorkspaceFile.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if f is None:
        raise HTTPException(404, "文件不存在")
    target_agent = await _resolve_transfer_target(db, user, agent_id, body.target_agent_id)
    if not Path(f.storage_path).is_file():
        raise HTTPException(404, "文件已从磁盘移除")
    rec = copy_file_to_agent(db, user=user, src=f, target_agent=target_agent)
    _audit_transfer(db, user, {"file_id": f.id, "from_agent_id": agent_id, "to_agent_id": target_agent.id,
                               "filename": f.filename, "saved_as": rec.filename})
    await db.commit()
    return {"id": rec.id, "filename": rec.filename, "size": rec.size}


class TransferFolderIn(BaseModel):
    path: str = Field(min_length=1, max_length=256)
    target_agent_id: int


@router.post("/transfer-folder", status_code=201)
async def transfer_folder(agent_id: int, body: TransferFolderIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """把本工作区的整个文件夹转交（逐文件复制）给同用户的另一个数字人，目录结构保留。"""
    path = body.path.strip().strip("/")
    if not _valid_rel(path):
        raise HTTPException(400, "路径不合法")
    rows = (
        (
            await db.execute(
                select(WorkspaceFile).where(
                    WorkspaceFile.agent_id == agent_id,
                    WorkspaceFile.user_id == user.id,
                    WorkspaceFile.deleted_at.is_(None),
                    (WorkspaceFile.filename == path) | (WorkspaceFile.filename.startswith(path + "/")),
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        raise HTTPException(404, "文件夹不存在或为空")
    target_agent = await _resolve_transfer_target(db, user, agent_id, body.target_agent_id)
    copied = 0
    for f in rows:
        if not Path(f.storage_path).is_file():
            continue  # 磁盘已缺失的跳过，不阻断整批
        copy_file_to_agent(db, user=user, src=f, target_agent=target_agent)
        copied += 1
    if copied == 0:
        raise HTTPException(404, "文件夹内文件均已从磁盘移除")
    _audit_transfer(db, user, {"from_agent_id": agent_id, "to_agent_id": target_agent.id,
                               "folder": path, "count": copied})
    await db.commit()
    return {"transferred": copied}


class FilePatch(BaseModel):
    """⋯ 菜单：重命名（同文件夹改名）/ 移动至（改文件夹）/ 置顶。"""
    filename: str | None = None  # 新文件名（不含路径）
    folder: str | None = None    # 目标文件夹（"" = 根目录）
    pinned: bool | None = None


@router.patch("/{file_id}")
async def update_file(agent_id: int, file_id: int, body: FilePatch, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    f = (
        await db.execute(
            select(WorkspaceFile).where(
                WorkspaceFile.id == file_id, WorkspaceFile.agent_id == agent_id,
                WorkspaceFile.user_id == user.id, WorkspaceFile.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if f is None:
        raise HTTPException(404, "文件不存在")

    if body.pinned is not None:
        f.pinned = body.pinned

    # 重命名 / 移动：拼新相对路径 → 校验 → 物理移动 → 更新记录
    if body.filename is not None or body.folder is not None:
        parts = f.filename.split("/")
        old_folder, old_name = ("/".join(parts[:-1]), parts[-1])
        new_name = (body.filename if body.filename is not None else old_name).strip()
        new_folder = (body.folder if body.folder is not None else old_folder).strip().strip("/")
        if "/" in new_name or not _valid_rel(new_name):
            raise HTTPException(400, f"文件{INVALID_HINT}")
        if new_folder and not _valid_rel(new_folder):
            raise HTTPException(400, "目标文件夹路径不合法")
        root = (DATA_DIR / "workspaces" / str(user.id) / str(agent_id)).resolve()
        dest_dir = (root / new_folder).resolve() if new_folder else root
        if root != dest_dir and root not in dest_dir.parents:
            raise HTTPException(400, "路径越界")
        src = Path(f.storage_path)
        if not src.is_file():
            raise HTTPException(404, "文件已从磁盘移除")
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / new_name
        # 目标已存在（且不是自己）→ 自动加序号，与上传行为一致
        if target.exists() and target.resolve() != src.resolve():
            i = 1
            while target.exists():
                target = dest_dir / f"{Path(new_name).stem}_{i}{Path(new_name).suffix}"
                i += 1
        if target.resolve() != src.resolve():
            src.rename(target)
        rel = f"{new_folder}/{target.name}" if new_folder else target.name
        f.filename = rel
        f.storage_path = str(target)
        f.mime = guess_mime(target.name, f.mime)
        # 扩展名变了 → 旧抽取文本可能失效
        if Path(rel).suffix.lower() != Path(old_name).suffix.lower():
            f.content_text = None

    await db.commit()
    return {"id": f.id, "filename": f.filename, "pinned": f.pinned, "mime": f.mime}


@router.delete("/{file_id}", status_code=204)
async def delete_file(agent_id: int, file_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    f = (
        await db.execute(
            select(WorkspaceFile).where(
                WorkspaceFile.id == file_id, WorkspaceFile.agent_id == agent_id,
                WorkspaceFile.user_id == user.id, WorkspaceFile.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if f is None:
        raise HTTPException(404, "文件不存在")
    f.deleted_at = utcnow()
    try:
        Path(f.storage_path).unlink(missing_ok=True)
    except OSError:
        pass
    await db.commit()
