"""M36 逻辑测试：工作区文件夹五功能——重命名（PATCH /folder）+ 置顶/排序偏好（folder-prefs）+ 前端接线守护。

_FakeDB 用查询队列（handler 内多次 execute 按序给行），范式抄 test_m19_transfer.py；
rename_folder 的 execute 顺序：① 冲突检查（目标前缀）→ ② 源文件夹文件行 → ③ shares → ④ prefs。
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.workspace.router as ws_router
from app.models.agent import WorkspaceFolderPref


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    """execute 按调用顺序弹出预设结果；add 收集落库对象。"""

    def __init__(self, *queries_rows):
        self._queue = [list(r) for r in queries_rows]
        self.added = []
        self.committed = 0

    async def execute(self, *a, **k):
        return _FakeResult(self._queue.pop(0) if self._queue else [])

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        pass

    async def commit(self):
        self.committed += 1


def _src_file(tmp_path, rel="reports/a.md", content="内容"):
    """在（user=1, agent=2）工作区落一个真文件，返回记录。"""
    p = tmp_path / "workspaces" / "1" / "2" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return SimpleNamespace(
        id=10, agent_id=2, user_id=1, filename=rel, mime="text/markdown",
        storage_path=str(p), content_text="抽取文本", deleted_at=None,
    )


def _share(paths):
    return SimpleNamespace(owner_user_id=1, agent_id=2, paths=json.dumps(paths, ensure_ascii=False), deleted_at=None)


def _pref(folder_path, pinned=False, sort_order=0, deleted_at=None):
    return SimpleNamespace(user_id=1, agent_id=2, folder_path=folder_path, pinned=pinned, sort_order=sort_order, deleted_at=deleted_at)


_USER = SimpleNamespace(id=1)


# ---------- 文件夹重命名 ----------

@pytest.mark.asyncio
async def test_rename_folder_rewrites_files_disk_and_db(monkeypatch, tmp_path):
    monkeypatch.setattr(ws_router, "DATA_DIR", tmp_path)
    files = [_src_file(tmp_path, "reports/a.md"), _src_file(tmp_path, "reports/2026/b.md", "B")]
    db = _FakeDB([], files, [], [])  # 冲突=空 → 源行 → 无共享 → 无 pref
    r = await ws_router.rename_folder(
        agent_id=2, body=ws_router.FolderPatch(path="reports", name="renamed"), user=_USER, db=db,
    )
    assert r == {"renamed": 2, "path": "renamed"}
    assert [f.filename for f in files] == ["renamed/a.md", "renamed/2026/b.md"]
    root = tmp_path / "workspaces" / "1" / "2"
    assert files[0].storage_path == str((root / "renamed/a.md").resolve())  # 重新拼路径而非字符串替换
    assert not (root / "reports").exists() and (root / "renamed" / "2026" / "b.md").read_text(encoding="utf-8") == "B"
    audits = [x for x in db.added if getattr(x, "action", "") == "workspace.folder_rename"]
    assert audits and '"new_path": "renamed"' in audits[0].detail
    assert db.committed == 1


@pytest.mark.asyncio
async def test_rename_folder_rewrites_share_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(ws_router, "DATA_DIR", tmp_path)
    files = [_src_file(tmp_path)]
    share = _share(["reports", "reports/2026", "other"])
    db = _FakeDB([], files, [share], [])
    await ws_router.rename_folder(
        agent_id=2, body=ws_router.FolderPatch(path="reports", name="renamed"), user=_USER, db=db,
    )
    # normalize_paths 去嵌套冗余：renamed/2026 被 renamed 覆盖
    assert json.loads(share.paths) == ["other", "renamed"]
    assert "reports" not in share.paths


@pytest.mark.asyncio
async def test_rename_folder_rewrites_folder_prefs(monkeypatch, tmp_path):
    monkeypatch.setattr(ws_router, "DATA_DIR", tmp_path)
    files = [_src_file(tmp_path)]
    prefs = [_pref("reports", pinned=True), _pref("reports/2026", sort_order=3), _pref("other", sort_order=1)]
    db = _FakeDB([], files, [], prefs)
    await ws_router.rename_folder(
        agent_id=2, body=ws_router.FolderPatch(path="reports", name="renamed"), user=_USER, db=db,
    )
    assert [p.folder_path for p in prefs] == ["renamed", "renamed/2026", "other"]
    assert prefs[0].pinned is True and prefs[1].sort_order == 3  # 只改路径不动偏好
    assert all(p.deleted_at is None for p in prefs)


@pytest.mark.asyncio
async def test_rename_folder_pref_collision_yields(monkeypatch, tmp_path):
    """目标路径已被别的 pref（含软删残留）占用 → 被改写的让位软删，不撞 uq。"""
    monkeypatch.setattr(ws_router, "DATA_DIR", tmp_path)
    files = [_src_file(tmp_path)]
    stale = _pref("renamed", deleted_at="2026-01-01")  # 软删残留也占唯一键
    prefs = [_pref("reports", pinned=True), stale]
    db = _FakeDB([], files, [], prefs)
    await ws_router.rename_folder(
        agent_id=2, body=ws_router.FolderPatch(path="reports", name="renamed"), user=_USER, db=db,
    )
    assert prefs[0].deleted_at is not None and prefs[0].folder_path == "reports"  # 让位软删、路径不改
    assert stale.folder_path == "renamed"  # 占用者不动


@pytest.mark.asyncio
async def test_rename_folder_conflict_409(monkeypatch, tmp_path):
    monkeypatch.setattr(ws_router, "DATA_DIR", tmp_path)
    _src_file(tmp_path)
    (tmp_path / "workspaces" / "1" / "2" / "renamed").mkdir(parents=True)  # 磁盘目标已存在
    db = _FakeDB([])
    with pytest.raises(HTTPException) as exc:
        await ws_router.rename_folder(
            agent_id=2, body=ws_router.FolderPatch(path="reports", name="renamed"), user=_USER, db=db,
        )
    assert exc.value.status_code == 409 and "同名" in exc.value.detail
    assert db.committed == 0


@pytest.mark.asyncio
async def test_rename_folder_conflict_409_db_rows(monkeypatch, tmp_path):
    """磁盘没目录但 DB 有目标前缀文件 → 同样 409。"""
    monkeypatch.setattr(ws_router, "DATA_DIR", tmp_path)
    files = [_src_file(tmp_path)]
    clash = [SimpleNamespace(id=99, filename="renamed/x.md")]
    db = _FakeDB(clash, files)
    with pytest.raises(HTTPException) as exc:
        await ws_router.rename_folder(
            agent_id=2, body=ws_router.FolderPatch(path="reports", name="renamed"), user=_USER, db=db,
        )
    assert exc.value.status_code == 409
    assert db.committed == 0


@pytest.mark.asyncio
async def test_rename_folder_invalid_name_400(monkeypatch, tmp_path):
    monkeypatch.setattr(ws_router, "DATA_DIR", tmp_path)
    for bad in ("../x", "a/b", "a\\b", "CON"):  # 尾空格会被 strip 洗掉（与文件重命名同款行为），不算 400 用例
        db = _FakeDB()
        with pytest.raises(HTTPException) as exc:
            await ws_router.rename_folder(
                agent_id=2, body=ws_router.FolderPatch(path="reports", name=bad), user=_USER, db=db,
            )
        assert exc.value.status_code == 400, bad
        assert db.committed == 0


@pytest.mark.asyncio
async def test_rename_folder_not_found_404(monkeypatch, tmp_path):
    monkeypatch.setattr(ws_router, "DATA_DIR", tmp_path)
    db = _FakeDB([], [])  # 冲突空、源行也空
    with pytest.raises(HTTPException) as exc:
        await ws_router.rename_folder(
            agent_id=2, body=ws_router.FolderPatch(path="nope", name="x"), user=_USER, db=db,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_rename_folder_noop_same_name(monkeypatch, tmp_path):
    monkeypatch.setattr(ws_router, "DATA_DIR", tmp_path)
    db = _FakeDB()
    r = await ws_router.rename_folder(
        agent_id=2, body=ws_router.FolderPatch(path="reports", name="reports"), user=_USER, db=db,
    )
    assert r == {"renamed": 0, "path": "reports"}
    assert db.committed == 0


# ---------- folder-prefs 端点 ----------

@pytest.mark.asyncio
async def test_put_folder_prefs_upsert():
    db = _FakeDB([])  # 无既有行 → 新增
    r = await ws_router.put_folder_prefs(
        agent_id=2,
        body=ws_router.FolderPrefsPut(items=[
            ws_router.FolderPrefItem(path="reports", pinned=True),
            ws_router.FolderPrefItem(path="docs", sort_order=2),
        ]),
        user=_USER, db=db,
    )
    assert r == {"saved": 2}
    added = [x for x in db.added if isinstance(x, WorkspaceFolderPref)]
    assert len(added) == 2
    assert added[0].folder_path == "reports" and added[0].pinned is True
    assert added[1].folder_path == "docs" and added[1].sort_order == 2
    assert db.committed == 1

    # 再 PUT 同路径 → 原地更新不新增（含软删复活）
    existing = _pref("reports", pinned=False, sort_order=9, deleted_at="2026-01-01")
    db2 = _FakeDB([existing])
    await ws_router.put_folder_prefs(
        agent_id=2, body=ws_router.FolderPrefsPut(items=[ws_router.FolderPrefItem(path="reports", pinned=True)]),
        user=_USER, db=db2,
    )
    assert db2.added == []  # 没有新增行
    assert existing.pinned is True and existing.sort_order == 9  # 只更新非 None 字段
    assert existing.deleted_at is None  # 软删复活


@pytest.mark.asyncio
async def test_put_folder_prefs_rejects_invalid_path():
    db = _FakeDB()
    with pytest.raises(HTTPException) as exc:
        await ws_router.put_folder_prefs(
            agent_id=2, body=ws_router.FolderPrefsPut(items=[ws_router.FolderPrefItem(path="../x", pinned=True)]),
            user=_USER, db=db,
        )
    assert exc.value.status_code == 400
    assert db.committed == 0


@pytest.mark.asyncio
async def test_get_folder_prefs_shape():
    db = _FakeDB([_pref("a", pinned=True, sort_order=1), _pref("b")])
    rows = await ws_router.list_folder_prefs(agent_id=2, user=_USER, db=db)
    assert rows == [
        {"path": "a", "pinned": True, "sort_order": 1},
        {"path": "b", "pinned": False, "sort_order": 0},
    ]


@pytest.mark.asyncio
async def test_delete_folder_soft_deletes_prefs(monkeypatch, tmp_path):
    monkeypatch.setattr(ws_router, "DATA_DIR", tmp_path)
    f = _src_file(tmp_path)
    pref = _pref("reports")
    db = _FakeDB([f], [pref])  # 文件行 → prefs（队列版 FakeDB 不做 WHERE 过滤，队列内容即「命中行」）
    r = await ws_router.delete_folder(agent_id=2, path="reports", user=_USER, db=db)
    assert r == {"deleted": 1}
    assert f.deleted_at is not None
    assert pref.deleted_at is not None  # 命中前缀的 pref 一并软删


def test_m36_routes_registered():
    paths = {(r.path, tuple(sorted(r.methods))) for r in ws_router.router.routes}
    assert ("/api/agents/{agent_id}/workspace/folder", ("PATCH",)) in paths
    assert ("/api/agents/{agent_id}/workspace/folder-prefs", ("GET",)) in paths
    assert ("/api/agents/{agent_id}/workspace/folder-prefs", ("PUT",)) in paths


# ---------- 前端源码守护 ----------

def test_frontend_m36_wiring():
    root = Path(__file__).resolve().parents[2]
    chat_page = (root / "frontend/src/pages/ChatPage.jsx").read_text(encoding="utf-8")
    for s in ("ea_ws_expanded_", "folder-prefs", "renameFolder", "@dnd-kit/sortable", "SortableFolderRow", "搜索文件或文件夹"):
        assert s in chat_page, s
    pkg = json.loads((root / "frontend/package.json").read_text(encoding="utf-8"))
    for dep in ("@dnd-kit/core", "@dnd-kit/sortable", "@dnd-kit/utilities"):
        assert dep in pkg["dependencies"], dep
