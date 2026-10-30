"""M19 逻辑测试：同用户跨数字人文件转交（transfer / transfer-folder）。

_FakeDB 用查询队列（handler 内多次 execute 按序给行）；DATA_DIR monkeypatch 到 tmp_path。
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.workspace.router as ws_router
from app.runtime.prompt import PLATFORM_LAYER


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


def _src_file(tmp_path, rel="reports/周报.md", content="周报内容"):
    """在源数字人（user=1, agent=2）工作区落一个真文件，返回记录。"""
    p = tmp_path / "workspaces" / "1" / "2" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return SimpleNamespace(
        id=10, agent_id=2, user_id=1, filename=rel, mime="text/markdown",
        storage_path=str(p), content_text="抽取文本", deleted_at=None,
    )


_TARGET = SimpleNamespace(id=3, owner_user_id=1, status="active", deleted_at=None)


@pytest.mark.asyncio
async def test_transfer_file_success(monkeypatch, tmp_path):
    monkeypatch.setattr(ws_router, "DATA_DIR", tmp_path)
    f = _src_file(tmp_path)
    db = _FakeDB([f], [_TARGET])
    r = await ws_router.transfer_file(
        agent_id=2, file_id=10, body=ws_router.TransferIn(target_agent_id=3),
        user=SimpleNamespace(id=1), db=db,
    )
    # 目标工作区（user=1, agent=3）拿到字节一致的副本，目录结构保留
    copied = tmp_path / "workspaces" / "1" / "3" / "reports" / "周报.md"
    assert copied.read_text(encoding="utf-8") == "周报内容"
    assert r["filename"] == "reports/周报.md"
    rec = next(x for x in db.added if getattr(x, "filename", None) == "reports/周报.md")
    assert rec.agent_id == 3 and rec.user_id == 1 and rec.content_text == "抽取文本"
    audits = [x for x in db.added if getattr(x, "action", "") == "workspace.transfer"]
    assert audits and '"to_agent_id": 3' in audits[0].detail
    assert db.committed == 1


@pytest.mark.asyncio
async def test_transfer_dedupe_naming(monkeypatch, tmp_path):
    monkeypatch.setattr(ws_router, "DATA_DIR", tmp_path)
    f = _src_file(tmp_path)
    # 目标工作区已有同名文件 → 自动加 _1
    dup = tmp_path / "workspaces" / "1" / "3" / "reports" / "周报.md"
    dup.parent.mkdir(parents=True, exist_ok=True)
    dup.write_text("旧版", encoding="utf-8")
    db = _FakeDB([f], [_TARGET])
    r = await ws_router.transfer_file(
        agent_id=2, file_id=10, body=ws_router.TransferIn(target_agent_id=3),
        user=SimpleNamespace(id=1), db=db,
    )
    assert r["filename"] == "reports/周报_1.md"
    assert dup.read_text(encoding="utf-8") == "旧版"  # 原件不被动


@pytest.mark.asyncio
async def test_transfer_to_other_users_agent_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(ws_router, "DATA_DIR", tmp_path)
    f = _src_file(tmp_path)
    db = _FakeDB([f], [])  # 目标查询查不到（别人的/已删的）
    with pytest.raises(HTTPException) as exc:
        await ws_router.transfer_file(
            agent_id=2, file_id=10, body=ws_router.TransferIn(target_agent_id=999),
            user=SimpleNamespace(id=1), db=db,
        )
    assert exc.value.status_code == 400 and "不属于你" in exc.value.detail


@pytest.mark.asyncio
async def test_transfer_to_self_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(ws_router, "DATA_DIR", tmp_path)
    f = _src_file(tmp_path)
    db = _FakeDB([f])
    with pytest.raises(HTTPException) as exc:
        await ws_router.transfer_file(
            agent_id=2, file_id=10, body=ws_router.TransferIn(target_agent_id=2),
            user=SimpleNamespace(id=1), db=db,
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_transfer_source_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(ws_router, "DATA_DIR", tmp_path)
    db = _FakeDB([])
    with pytest.raises(HTTPException) as exc:
        await ws_router.transfer_file(
            agent_id=2, file_id=99, body=ws_router.TransferIn(target_agent_id=3),
            user=SimpleNamespace(id=1), db=db,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_transfer_disk_file_gone(monkeypatch, tmp_path):
    monkeypatch.setattr(ws_router, "DATA_DIR", tmp_path)
    f = _src_file(tmp_path)
    (tmp_path / "workspaces" / "1" / "2" / f.filename).unlink()
    db = _FakeDB([f], [_TARGET])
    with pytest.raises(HTTPException) as exc:
        await ws_router.transfer_file(
            agent_id=2, file_id=10, body=ws_router.TransferIn(target_agent_id=3),
            user=SimpleNamespace(id=1), db=db,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_transfer_folder_success(monkeypatch, tmp_path):
    monkeypatch.setattr(ws_router, "DATA_DIR", tmp_path)
    files = [
        _src_file(tmp_path, "docs/a.md", "A"),
        _src_file(tmp_path, "docs/b.md", "B"),
        _src_file(tmp_path, "docs/sub/c.md", "C"),
    ]
    db = _FakeDB(files, [_TARGET])
    r = await ws_router.transfer_folder(
        agent_id=2, body=ws_router.TransferFolderIn(path="docs", target_agent_id=3),
        user=SimpleNamespace(id=1), db=db,
    )
    assert r == {"transferred": 3}
    root = tmp_path / "workspaces" / "1" / "3" / "docs"
    assert (root / "a.md").read_text(encoding="utf-8") == "A"
    assert (root / "b.md").read_text(encoding="utf-8") == "B"
    assert (root / "sub" / "c.md").read_text(encoding="utf-8") == "C"  # 子目录结构保留


@pytest.mark.asyncio
async def test_transfer_folder_empty_404(monkeypatch, tmp_path):
    monkeypatch.setattr(ws_router, "DATA_DIR", tmp_path)
    db = _FakeDB([])
    with pytest.raises(HTTPException) as exc:
        await ws_router.transfer_folder(
            agent_id=2, body=ws_router.TransferFolderIn(path="nope", target_agent_id=3),
            user=SimpleNamespace(id=1), db=db,
        )
    assert exc.value.status_code == 404


def test_platform_layer_teaches_transfer():
    assert "转交给数字人" in PLATFORM_LAYER
    bullets = [ln for ln in PLATFORM_LAYER.splitlines() if ln.startswith("- ")]
    assert len(bullets) <= 7
