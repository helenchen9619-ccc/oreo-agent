"""M21 逻辑测试：工作区文件名字符集放宽（白名单字符正则 → 按段黑名单）。

场景：ERP 每次导出「物品导出清单-总部 (16).xlsx」（空格+半角括号），
上传能进工作区、run_code 带入却被旧白名单拦死——文件进得来用不了。
改为 app.workspace.paths.valid_rel 单一实现，tools/router/shares 共用。
"""
import base64
from types import SimpleNamespace

import pytest

import app.runtime.tools as tools
import app.workspace.router as ws_router
from app.runtime.tools import ToolContext, dispatch_tool
from app.workspace.paths import sanitize_name, valid_rel

ERP_NAME = "物品导出清单-总部 (16).xlsx"


# ---------- valid_rel 正反矩阵 ----------

def test_valid_rel_accepts_real_world_names():
    ok = [
        ERP_NAME,
        "reports/2026/周报.md",
        "【月结】2026、Q1.xlsx",
        "a & b (final) v2+.csv",
        ".keep",                      # 存量点前缀文件不受影响
        "销售数据（含退换）.parquet",  # 全角括号
    ]
    for p in ok:
        assert valid_rel(p) is True, p


def test_valid_rel_still_rejects_dangerous():
    bad = [
        "", "../evil.txt", "a/../../evil", "a//b", "/abs.txt",
        "a\\b.txt",            # 反斜杠（Windows 分层歧义）
        "a:b.txt", 'a"b.txt', "a<b.txt", "a>b.txt", "a|b.txt", "a?.txt", "a*.txt",
        "a\x01b.txt",          # 控制字符
        "a.", "a ",            # 尾点/尾空格（Windows 落盘兼容）
        "CON.xlsx", "com1.txt",  # Windows 保留设备名
    ]
    for p in bad:
        assert valid_rel(p) is False, p


def test_router_alias_points_to_same_impl():
    """shares.py/旧测试按 _valid_rel 引用——别名必须就是 paths.valid_rel。"""
    assert ws_router._valid_rel is valid_rel


# ---------- sanitize_name（飞书落盘清洗） ----------

def test_sanitize_name_keeps_readable_chars():
    assert sanitize_name("报告 2026(1).pdf") == "报告 2026(1).pdf"  # 空格/括号保留原名
    assert sanitize_name("报告*2026?.pdf") == "报告_2026_.pdf"       # 黑名单字符仍清洗
    assert sanitize_name("a/b.txt") == "a_b.txt"                     # 剥掉路径分层
    assert sanitize_name("   ") == ""                                # 洗不干净→调用方兜底
    assert sanitize_name("CON") == ""


# ---------- run_code 带入 ERP 文件名端到端 ----------

class _FakeResult:
    def scalars(self):
        return self

    def all(self):
        return []

    def scalar_one_or_none(self):
        return None


class _FakeDB:
    def __init__(self):
        self.added = []

    async def execute(self, *a, **k):
        return _FakeResult()

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        pass


def _ctx(**over):
    base = dict(db=_FakeDB(), user=SimpleNamespace(id=1), agent=SimpleNamespace(id=2))
    base.update(over)
    return ToolContext(**base)


@pytest.mark.asyncio
async def test_run_code_accepts_erp_filename(monkeypatch, tmp_path):
    """M21 核心场景：ERP 导出件（空格+括号）从工作区带进沙箱，不再被路径白名单拦死。"""
    from app.runtime import sandbox_client

    captured = {}

    async def _fake(code, files, timeout_s=None):
        captured["files"] = files
        return {"ok": True, "exit_code": 0, "stdout": "ok", "stderr": "", "duration_ms": 1, "outputs": []}

    monkeypatch.setattr(sandbox_client, "exec_code", _fake)
    monkeypatch.setattr(tools, "_workspace_root", lambda u, a: tmp_path)
    monkeypatch.setattr(tools, "_trace_root", lambda u, a: tmp_path / "traces")
    (tmp_path / ERP_NAME).write_bytes(b"PK-fake-xlsx")

    ctx = _ctx(allow_code_exec=True)
    r = await dispatch_tool(ctx, "run_code", {"code": "print(1)", "files": [ERP_NAME]})
    assert "不合法" not in r, r
    assert captured["files"] == [
        {"path": ERP_NAME, "content_b64": base64.b64encode(b"PK-fake-xlsx").decode()}
    ]


@pytest.mark.asyncio
async def test_run_code_still_rejects_traversal(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "_workspace_root", lambda u, a: tmp_path)
    ctx = _ctx(allow_code_exec=True)
    r = await dispatch_tool(ctx, "run_code", {"code": "print(1)", "files": ["../机密.xlsx"]})
    assert "带入文件路径不合法" in r


@pytest.mark.asyncio
async def test_workspace_write_accepts_parens_and_space(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "_workspace_root", lambda u, a: tmp_path)
    ctx = _ctx()
    r = await dispatch_tool(ctx, "workspace_write", {"path": "报表/汇总 (终版).md", "content": "x"})
    assert "已写入" in r
    assert (tmp_path / "报表" / "汇总 (终版).md").is_file()


# ---------- 上传端点：文件名也过校验（缺口补上） ----------

class _FakeUpload:
    def __init__(self, name: str, data: bytes = b"x"):
        self.filename = name
        self.content_type = "application/octet-stream"
        self._data = data

    async def read(self):
        return self._data


@pytest.mark.asyncio
async def test_upload_validates_filename(monkeypatch, tmp_path):
    monkeypatch.setattr(ws_router, "DATA_DIR", tmp_path)
    user = SimpleNamespace(id=1)

    rec = await ws_router.upload_file(2, _FakeUpload(ERP_NAME), "", user, _FakeDB())
    assert rec["filename"] == ERP_NAME
    assert (tmp_path / "workspaces" / "1" / "2" / ERP_NAME).is_file()

    # 注：反斜杠在 Windows 下会被 Path().name 剥掉分层，用 Windows 非法字符 ? 验证拦截
    with pytest.raises(Exception) as e:
        await ws_router.upload_file(2, _FakeUpload("a?.txt"), "", user, _FakeDB())
    assert "路径不合法" in str(e.value)
