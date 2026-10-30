"""M8d 逻辑测试：feishu_imgs/ 定时清扫（sweep_feishu_images）+ 落盘复活幂等。

契约：
- 清扫只碰 DB 查询命中的 feishu_imgs/ 且 created_at 超 _FEISHU_IMG_TTL_DAYS 的行（DB 是事实源，不扫磁盘）
- 每行软删 deleted_at + unlink 磁盘文件（missing_ok，异常不炸）；返回清理数；空则不 commit
- 落盘幂等：_save_feishu_bytes 对软删行复活（deleted_at=None），清理后重拉消息图片自动重建
"""
import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.base import utcnow
from app.runtime import tools
from app.runtime.tools import ToolContext, sweep_feishu_images


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _SweepDB:
    """execute 返回「查询已命中」的行集（SQL 过滤由 test_sweep_filter 用 inspect 断言真实源码）。"""

    def __init__(self, rows):
        self.rows = rows
        self.committed = 0

    async def execute(self, *a, **k):
        return _Rows(self.rows)

    async def commit(self):
        self.committed += 1


def _row(path, deleted_at=None):
    return SimpleNamespace(filename="feishu_imgs/" + path, storage_path=path, deleted_at=deleted_at)


# ---------- 清扫行为 ----------

@pytest.mark.asyncio
async def test_sweep_soft_deletes_and_unlinks(tmp_path):
    p = tmp_path / "feishu_imgs" / "a.png"
    p.parent.mkdir()
    p.write_bytes(b"x")
    row = _row(str(p))
    db = _SweepDB([row])
    n = await sweep_feishu_images(db)
    assert n == 1
    assert row.deleted_at is not None
    assert not p.exists()
    assert db.committed == 1


@pytest.mark.asyncio
async def test_sweep_no_rows_returns_zero_no_commit():
    db = _SweepDB([])
    n = await sweep_feishu_images(db)
    assert n == 0 and db.committed == 0


@pytest.mark.asyncio
async def test_sweep_missing_disk_file_tolerated(tmp_path):
    row = _row(str(tmp_path / "feishu_imgs" / "gone.png"))
    db = _SweepDB([row])
    n = await sweep_feishu_images(db)
    assert n == 1
    assert row.deleted_at is not None  # 磁盘没了也照常软删，不炸


# ---------- 过滤口径（源码级断言，与 test_m33 同款 inspect 手法） ----------

def test_sweep_filter_matches_feishu_imgs_and_ttl():
    src = inspect.getsource(tools.sweep_feishu_images)
    assert 'WorkspaceFile.filename.like("feishu_imgs/%")' in src
    assert "WorkspaceFile.deleted_at.is_(None)" in src
    assert "WorkspaceFile.created_at < cutoff" in src
    assert "_FEISHU_IMG_TTL_DAYS" in src
    # cutoff 必须锚定 TTL，而不是写死天数
    assert "timedelta(days=_FEISHU_IMG_TTL_DAYS)" in src


# ---------- 落盘复活幂等 ----------

@pytest.mark.asyncio
async def test_resave_resurrects_soft_deleted_row(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_workspace_root", lambda uid, aid: tmp_path)
    row = SimpleNamespace(deleted_at=utcnow())  # 软删态（已被清扫）

    class _Result:
        def scalar_one_or_none(self):
            return row

    class _DB:
        def __init__(self):
            self.committed = 0

        async def execute(self, *a, **k):
            return _Result()

        async def commit(self):
            self.committed += 1

    ctx = ToolContext(db=_DB(), user=SimpleNamespace(id=1), agent=SimpleNamespace(id=1))
    rel = await tools._save_feishu_bytes(ctx, "feishu_imgs/x.png", b"x", "image/png")
    assert rel == "feishu_imgs/x.png"
    assert row.deleted_at is None            # 复活
    assert (tmp_path / "feishu_imgs" / "x.png").exists()
