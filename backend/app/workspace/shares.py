"""工作区内容跨用户只读共享（M5/M5.1）。

属主侧：/api/agents/{agent_id}/workspace/shares（增删查自己的共享）
接收侧：/api/shared（我可见的共享 → 文件列表 → 预览/下载/另存）

粒度（M5.1）：paths 为路径数组，元素可为文件或文件夹——精确命中=共享该文件，
前缀命中=共享该文件夹内容；空数组 = 整个工作区（覆盖未来新增文件，逐个全选文件不覆盖）。

语义：实时共享（接收方看最新内容）、只读；另存=复制进接收方自己的工作区。
审计：share.create / share.delete / share.read(preview|raw) / share.save。
"""
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.agent import Agent, WorkspaceFile, WorkspaceShare
from app.models.base import utcnow
from app.models.user import AuditLog, Department, User
from app.workspace.router import DATA_DIR, _extract_text, _valid_rel, copy_file_to_agent, guess_mime

owner_router = APIRouter(prefix="/api/agents/{agent_id}/workspace/shares", tags=["workspace-shares"])
router = APIRouter(prefix="/api/shared", tags=["workspace-shared"])

GRANTEE_TYPES = {"user", "department", "all"}
MAX_PATHS = 50  # 单次共享的路径数上限


# ---------- 纯函数（可测） ----------

def share_visible(share: WorkspaceShare, user: User) -> bool:
    """该共享对 user 是否可见（all / 指定用户 / 指定部门）。"""
    if share.grantee_type == "all":
        return True
    if share.grantee_type == "user":
        return share.grantee_id == user.id
    if share.grantee_type == "department":
        return user.department_id is not None and share.grantee_id == user.department_id
    return False


def paths_cover(paths: list[str], filename: str) -> bool:
    """共享路径集是否覆盖该文件：[]=整个工作区；元素精确命中=文件本身，前缀命中=文件夹内容
    （边界：报表 ≠ 报表2）。"""
    if not paths:
        return True
    for p in paths:
        p = p.rstrip("/")
        if filename == p or filename.startswith(p + "/"):
            return True
    return False


def normalize_paths(paths: list[str]) -> list[str]:
    """规范化共享路径：去空白/去末尾斜杠/去重/剔除嵌套冗余（已含"报表"则"报表/x"冗余）/排序。"""
    cleaned = {p.strip().strip("/") for p in paths if p and p.strip().strip("/")}
    out: list[str] = []
    for p in sorted(cleaned):
        if any(p == q or p.startswith(q + "/") for q in out):
            continue  # 已被更短的上级路径覆盖
        out.append(p)
    return out


def _paths_of(s: WorkspaceShare) -> list[str]:
    try:
        v = json.loads(s.paths or "[]")
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return []


def _audit(db: AsyncSession, actor: User, action: str, target_id: object, detail: dict) -> None:
    db.add(AuditLog(actor_user_id=actor.id, action=action, target_type="workspace_share", target_id=str(target_id), detail=json.dumps(detail, ensure_ascii=False)[:4000]))


async def _my_visible_shares(db: AsyncSession, user: User) -> list[WorkspaceShare]:
    rows = (await db.execute(select(WorkspaceShare).where(WorkspaceShare.deleted_at.is_(None)))).scalars().all()
    return [s for s in rows if share_visible(s, user)]


async def _files_of_share(db: AsyncSession, s: WorkspaceShare) -> list[WorkspaceFile]:
    """该共享覆盖的文件（paths=[] 直接全量；否则拉该工作区未删文件后按 paths_cover 过滤——
    共享是低频操作，单工作区文件量级可控）。"""
    rows = (
        (
            await db.execute(
                select(WorkspaceFile).where(
                    WorkspaceFile.user_id == s.owner_user_id,
                    WorkspaceFile.agent_id == s.agent_id,
                    WorkspaceFile.deleted_at.is_(None),
                ).order_by(WorkspaceFile.filename.asc())
            )
        )
        .scalars()
        .all()
    )
    paths = _paths_of(s)
    if not paths:
        return list(rows)
    return [f for f in rows if paths_cover(paths, f.filename)]


async def _get_visible_file(db: AsyncSession, user: User, file_id: int) -> WorkspaceFile:
    """按 file_id 取文件 + 校验被我可见的任一共享覆盖；否则 404（不暴露存在性）。"""
    f = (
        await db.execute(select(WorkspaceFile).where(WorkspaceFile.id == file_id, WorkspaceFile.deleted_at.is_(None)))
    ).scalar_one_or_none()
    if f is None:
        raise HTTPException(404, "文件不存在")
    shares = await _my_visible_shares(db, user)
    if any(s.agent_id == f.agent_id and s.owner_user_id == f.user_id and paths_cover(_paths_of(s), f.filename) for s in shares):
        return f
    raise HTTPException(404, "文件不存在")


# ---------- 属主侧：管理共享 ----------

class ShareIn(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=MAX_PATHS)  # [] = 整个工作区
    grantee_type: str = "user"
    grantee_id: int | None = None


async def _grantee_label(db: AsyncSession, s: WorkspaceShare) -> str:
    if s.grantee_type == "all":
        return "全员"
    if s.grantee_type == "department":
        name = (await db.execute(select(Department.name).where(Department.id == s.grantee_id))).scalar_one_or_none()
        return f"部门：{name or '#' + str(s.grantee_id)}"
    name = (await db.execute(select(User.name).where(User.id == s.grantee_id))).scalar_one_or_none()
    return f"用户：{name or '#' + str(s.grantee_id)}"


def _share_out(s: WorkspaceShare, label: str) -> dict:
    return {"id": s.id, "paths": _paths_of(s), "grantee_type": s.grantee_type, "grantee_id": s.grantee_id, "grantee_label": label, "created_at": s.created_at}


@owner_router.get("")
async def list_my_shares(agent_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (
        (
            await db.execute(
                select(WorkspaceShare).where(
                    WorkspaceShare.owner_user_id == user.id, WorkspaceShare.agent_id == agent_id, WorkspaceShare.deleted_at.is_(None)
                ).order_by(WorkspaceShare.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_share_out(s, await _grantee_label(db, s)) for s in rows]


@owner_router.post("", status_code=201)
async def create_share(agent_id: int, body: ShareIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    paths = normalize_paths(body.paths)
    if len(paths) > MAX_PATHS:
        raise HTTPException(400, f"一次最多共享 {MAX_PATHS} 个路径")
    # 每个路径必须真实存在：DB 里有该文件，或磁盘上有该文件夹
    if paths:
        rows = (
            (
                await db.execute(
                    select(WorkspaceFile.filename).where(
                        WorkspaceFile.user_id == user.id, WorkspaceFile.agent_id == agent_id, WorkspaceFile.deleted_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        existing_files = set(rows)
        root = DATA_DIR / "workspaces" / str(user.id) / str(agent_id)
        for p in paths:
            if not _valid_rel(p):
                raise HTTPException(400, f"路径不合法：{p}")
            if p not in existing_files and not (root / p).is_dir():
                raise HTTPException(400, f"文件或文件夹不存在：{p}")
    if body.grantee_type not in GRANTEE_TYPES:
        raise HTTPException(400, f"grantee_type 必须是 {'/'.join(sorted(GRANTEE_TYPES))} 之一")
    if body.grantee_type == "all":
        grantee_id = None
    else:
        if body.grantee_id is None:
            raise HTTPException(400, "请选择共享对象")
        grantee_id = body.grantee_id
        if body.grantee_type == "user":
            if grantee_id == user.id:
                raise HTTPException(400, "不需要共享给自己")
            hit = (await db.execute(select(User.id).where(User.id == grantee_id, User.deleted_at.is_(None), User.status == "active"))).scalar_one_or_none()
            if hit is None:
                raise HTTPException(400, "目标用户不存在或已禁用")
        else:
            hit = (await db.execute(select(Department.id).where(Department.id == grantee_id, Department.deleted_at.is_(None)))).scalar_one_or_none()
            if hit is None:
                raise HTTPException(400, "目标部门不存在")
    paths_json = json.dumps(paths, ensure_ascii=False)
    dup = (
        await db.execute(
            select(WorkspaceShare.id).where(
                WorkspaceShare.owner_user_id == user.id,
                WorkspaceShare.agent_id == agent_id,
                WorkspaceShare.paths == paths_json,
                WorkspaceShare.grantee_type == body.grantee_type,
                WorkspaceShare.grantee_id == grantee_id if grantee_id is not None else WorkspaceShare.grantee_id.is_(None),
                WorkspaceShare.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(400, "相同内容已共享给同一对象")
    s = WorkspaceShare(owner_user_id=user.id, agent_id=agent_id, paths=paths_json, grantee_type=body.grantee_type, grantee_id=grantee_id)
    db.add(s)
    await db.flush()
    _audit(db, user, "share.create", s.id, {"agent_id": agent_id, "paths": paths, "grantee_type": s.grantee_type, "grantee_id": grantee_id})
    await db.commit()
    return _share_out(s, await _grantee_label(db, s))


@owner_router.delete("/{share_id}", status_code=204)
async def delete_share(agent_id: int, share_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    s = (
        await db.execute(
            select(WorkspaceShare).where(
                WorkspaceShare.id == share_id,
                WorkspaceShare.owner_user_id == user.id,
                WorkspaceShare.agent_id == agent_id,
                WorkspaceShare.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if s is None:
        raise HTTPException(404, "共享不存在")
    s.deleted_at = utcnow()
    _audit(db, user, "share.delete", s.id, {"agent_id": agent_id, "paths": _paths_of(s)})
    await db.commit()


# ---------- 接收侧：浏览 / 预览 / 下载 / 另存 ----------

@router.get("/options")
async def grantee_options(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """共享对象选择器的数据源（全员可见：只暴露 id+name/部门名，不涉敏感字段）。"""
    users = (
        (await db.execute(select(User.id, User.name).where(User.deleted_at.is_(None), User.status == "active", User.id != user.id).order_by(User.name.asc())))
        .all()
    )
    depts = (
        (await db.execute(select(Department.id, Department.name, Department.parent_id).where(Department.deleted_at.is_(None)).order_by(Department.id.asc())))
        .all()
    )
    return {"users": [{"id": u.id, "name": u.name} for u in users], "departments": [{"id": d.id, "name": d.name, "parent_id": d.parent_id} for d in depts]}


@router.get("")
async def shared_with_me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    shares = await _my_visible_shares(db, user)
    out = []
    for s in shares:
        agent = (await db.execute(select(Agent).where(Agent.id == s.agent_id, Agent.deleted_at.is_(None), Agent.status == "active"))).scalar_one_or_none()
        if agent is None:
            continue  # 源数字人已归档/删除的共享不再展示
        owner_name = (await db.execute(select(User.name).where(User.id == s.owner_user_id))).scalar_one_or_none()
        files = await _files_of_share(db, s)
        latest = max((f.updated_at for f in files), default=None)
        out.append({
            "id": s.id, "paths": _paths_of(s), "owner_name": owner_name or f"#{s.owner_user_id}",
            "agent_id": s.agent_id, "agent_name": agent.name,
            "file_count": len(files), "latest_at": latest,
        })
    return out


@router.get("/{share_id}/files")
async def shared_files(share_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    s = (await db.execute(select(WorkspaceShare).where(WorkspaceShare.id == share_id, WorkspaceShare.deleted_at.is_(None)))).scalar_one_or_none()
    if s is None or not share_visible(s, user):
        raise HTTPException(404, "共享不存在")
    files = await _files_of_share(db, s)
    return [{"id": f.id, "filename": f.filename, "size": f.size, "mime": f.mime, "updated_at": f.updated_at, "has_text": bool(f.content_text)} for f in files]


@router.get("/files/{file_id}")
async def shared_file_preview(file_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    f = await _get_visible_file(db, user, file_id)
    # 懒抽取（xlsx/docx/pptx 首次预览时补齐 content_text，与 workspace 预览端点一致）
    if f.content_text is None and Path(f.filename).suffix.lower() in (".xlsx", ".docx", ".pptx"):
        p = Path(f.storage_path)
        if p.exists():
            f.content_text = _extract_text(f.filename, p.read_bytes())
            await db.commit()
    _audit(db, user, "share.read", f.id, {"agent_id": f.agent_id, "owner_user_id": f.user_id, "filename": f.filename, "mode": "preview"})
    await db.commit()
    return {"id": f.id, "filename": f.filename, "size": f.size, "mime": f.mime, "content_text": f.content_text}


@router.get("/files/{file_id}/raw")
async def shared_file_raw(file_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> FileResponse:
    f = await _get_visible_file(db, user, file_id)
    p = Path(f.storage_path)
    if not p.exists():
        raise HTTPException(404, "磁盘文件缺失")
    _audit(db, user, "share.read", f.id, {"agent_id": f.agent_id, "owner_user_id": f.user_id, "filename": f.filename, "mode": "raw"})
    await db.commit()
    return FileResponse(p, filename=p.name, media_type=f.mime or "application/octet-stream")


@router.get("/files/{file_id}/export.docx")
async def shared_file_export_docx(file_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """共享 .md 也支持「导出为 Word」（预览组件三个来源端点同前缀约定）。"""
    from app.workspace.router import _export_md_as_docx

    f = await _get_visible_file(db, user, file_id)
    resp = _export_md_as_docx(f)
    _audit(db, user, "share.read", f.id, {"agent_id": f.agent_id, "owner_user_id": f.user_id, "filename": f.filename, "mode": "export_docx"})
    await db.commit()
    return resp


class SaveIn(BaseModel):
    agent_id: int  # 另存到我的哪个数字人工作区


@router.post("/files/{file_id}/save", status_code=201)
async def save_shared_file(file_id: int, body: SaveIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    f = await _get_visible_file(db, user, file_id)
    target_agent = (
        await db.execute(
            select(Agent).where(Agent.id == body.agent_id, Agent.owner_user_id == user.id, Agent.deleted_at.is_(None), Agent.status == "active")
        )
    ).scalar_one_or_none()
    if target_agent is None:
        raise HTTPException(400, "目标数字人不存在或不属于你")
    if not Path(f.storage_path).exists():
        raise HTTPException(404, "磁盘文件缺失")
    # 复制语义与「转交给数字人」共用同一实现（保留目录结构、重名加 _1 序号）
    rec = copy_file_to_agent(db, user=user, src=f, target_agent=target_agent)
    await db.flush()
    _audit(db, user, "share.save", f.id, {"from_agent_id": f.agent_id, "from_owner_user_id": f.user_id, "filename": f.filename, "to_agent_id": target_agent.id, "saved_as": rec.filename})
    await db.commit()
    return {"id": rec.id, "filename": rec.filename, "size": rec.size}
