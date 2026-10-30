"""M5 逻辑测试：跨数字人只读（_resolve_peer_agent + workspace_list/read 的 agent 参数）+ 跨用户共享纯函数。"""
from types import SimpleNamespace

import pytest

from app.runtime.tools import _resolve_peer_agent, dispatch_tool
from app.runtime.tools import ToolContext
from app.workspace.shares import normalize_paths, paths_cover, share_visible


def _agent(aid, name, uid=1):
    return SimpleNamespace(id=aid, name=name, owner_user_id=uid, status="active", deleted_at=None)


class _FakeResult:
    def __init__(self, rows=(), one=None):
        self._rows = list(rows)
        self._one = one

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._one


class _PeerDB:
    """按查询目标表分流：agents 表→数字人清单；其余（workspace_files）→预置文件行。"""

    def __init__(self, agents=(), ws_row=None, ws_files=()):
        self.agents = list(agents)
        self.ws_row = ws_row
        self.ws_files = list(ws_files)
        self.added = []

    async def execute(self, stmt, *a, **k):
        if "from agents" in str(stmt).lower():
            return _FakeResult(rows=self.agents)
        return _FakeResult(rows=self.ws_files, one=self.ws_row)

    def add(self, row):
        self.added.append(row)


def _ctx(db, agent=None):
    return ToolContext(db=db, user=SimpleNamespace(id=1), agent=agent or _agent(1, "PPT助手"))


# ---------- _resolve_peer_agent ----------

@pytest.mark.asyncio
async def test_peer_resolve_empty_is_self():
    ctx = _ctx(_PeerDB())
    target, err = await _resolve_peer_agent(ctx, None)
    assert err is None and target.id == 1
    target, err = await _resolve_peer_agent(ctx, "  ")
    assert err is None and target.id == 1


@pytest.mark.asyncio
async def test_peer_resolve_by_name_and_id():
    db = _PeerDB(agents=[_agent(1, "PPT助手"), _agent(2, "数据分析")])
    target, err = await _resolve_peer_agent(_ctx(db), "数据分析")
    assert err is None and target.id == 2
    target, err = await _resolve_peer_agent(_ctx(db), "2")
    assert err is None and target.id == 2


@pytest.mark.asyncio
async def test_peer_resolve_name_miss_lists_available():
    db = _PeerDB(agents=[_agent(2, "数据分析")])
    target, err = await _resolve_peer_agent(_ctx(db), "不存在的人")
    assert target is None and "数据分析" in err  # 报错里列出可用数字人


@pytest.mark.asyncio
async def test_peer_resolve_id_not_owned_rejected():
    db = _PeerDB(agents=[_agent(2, "数据分析")])  # 库里只有 id=2（属于本用户）
    target, err = await _resolve_peer_agent(_ctx(db), "99")
    assert target is None and "99" in err


@pytest.mark.asyncio
async def test_peer_resolve_duplicate_names_asks_id():
    db = _PeerDB(agents=[_agent(2, "助手"), _agent(3, "助手")])
    target, err = await _resolve_peer_agent(_ctx(db), "助手")
    assert target is None and "2" in err and "3" in err  # 提示改用 id


# ---------- workspace_list / workspace_read 跨数字人 ----------

@pytest.mark.asyncio
async def test_workspace_list_peer_marks_readonly():
    db = _PeerDB(agents=[_agent(2, "数据分析")], ws_files=[("调研/竞品.md", 1234)])
    r = await dispatch_tool(_ctx(db), "workspace_list", {"agent": "数据分析"})
    assert "数据分析" in r and "只读" in r and "调研/竞品.md" in r


@pytest.mark.asyncio
async def test_workspace_read_peer_returns_content_and_audits():
    f = SimpleNamespace(id=9, filename="调研/竞品.md", content_text="竞品全文", agent_id=2, user_id=1)
    db = _PeerDB(agents=[_agent(2, "数据分析")], ws_row=f)
    ctx = _ctx(db)
    r = await dispatch_tool(ctx, "workspace_read", {"filename": "调研/竞品.md", "agent": "数据分析"})
    assert r == "竞品全文"
    audits = [x for x in db.added if getattr(x, "action", "") == "workspace.read_peer"]
    assert len(audits) == 1  # 跨数字人读留痕


@pytest.mark.asyncio
async def test_workspace_read_self_no_audit():
    f = SimpleNamespace(id=9, filename="a.md", content_text="自己的文件", agent_id=1, user_id=1)
    db = _PeerDB(ws_row=f)
    ctx = _ctx(db)
    r = await dispatch_tool(ctx, "workspace_read", {"filename": "a.md"})
    assert r == "自己的文件"
    assert not db.added  # 读本数字人：行为与现状一致（不审计）


@pytest.mark.asyncio
async def test_workspace_read_peer_binary_hint():
    f = SimpleNamespace(id=9, filename="data.parquet", content_text=None, agent_id=2, user_id=1)
    db = _PeerDB(agents=[_agent(2, "数据分析")], ws_row=f)
    r = await dispatch_tool(_ctx(db), "workspace_read", {"filename": "data.parquet", "agent": "数据分析"})
    assert "不是文本类文件" in r


# ---------- 跨用户共享纯函数（share_visible / folder_covers） ----------

def _share(grantee_type, grantee_id=None, folder=""):
    return SimpleNamespace(grantee_type=grantee_type, grantee_id=grantee_id, folder=folder)


def _user(uid, dept=None):
    return SimpleNamespace(id=uid, department_id=dept)


def test_share_visible_all():
    assert share_visible(_share("all"), _user(9)) is True


def test_share_visible_user_hit_and_miss():
    s = _share("user", grantee_id=7)
    assert share_visible(s, _user(7)) is True
    assert share_visible(s, _user(8)) is False


def test_share_visible_department():
    s = _share("department", grantee_id=3)
    assert share_visible(s, _user(9, dept=3)) is True
    assert share_visible(s, _user(9, dept=4)) is False
    assert share_visible(s, _user(9, dept=None)) is False  # 无部门用户不命中部门共享


def test_share_visible_unknown_type():
    assert share_visible(_share("weird", grantee_id=1), _user(1)) is False


def test_paths_cover_root_covers_all():
    assert paths_cover([], "报表/x.md") is True
    assert paths_cover([], "a.md") is True


def test_paths_cover_exact_file():
    # 单文件共享：精确命中才覆盖，同文件夹其他文件不覆盖
    assert paths_cover(["报表/月报.md"], "报表/月报.md") is True
    assert paths_cover(["报表/月报.md"], "报表/周报.md") is False
    assert paths_cover(["a.md"], "a.md") is True


def test_paths_cover_prefix_boundary():
    assert paths_cover(["报表"], "报表/x.md") is True
    assert paths_cover(["报表"], "报表/子/y.md") is True
    assert paths_cover(["报表"], "报表2/x.md") is False   # 前缀边界：不能误伤同名前缀文件夹
    assert paths_cover(["报表"], "报表.md") is False     # 文件夹路径不覆盖同名文件
    assert paths_cover(["报表/"], "报表/x.md") is True   # 末尾斜杠容错


def test_paths_cover_mixed_multi():
    # 多选混合：文件夹 + 单文件
    paths = ["调研", "notes.md"]
    assert paths_cover(paths, "调研/a.md") is True
    assert paths_cover(paths, "notes.md") is True
    assert paths_cover(paths, "其他/b.md") is False
    assert paths_cover(paths, "notes2.md") is False


def test_normalize_paths_dedup_and_sort():
    assert normalize_paths(["b", "a", "a", " b "]) == ["a", "b"]
    assert normalize_paths([]) == []
    assert normalize_paths(["", "  "]) == []


def test_normalize_paths_drops_nested():
    # 已含"报表"则"报表/x"冗余剔除；"报表2"不受影响
    assert normalize_paths(["报表/x", "报表", "报表2"]) == ["报表", "报表2"]
    assert normalize_paths(["a/b/c", "a/b", "a"]) == ["a"]
