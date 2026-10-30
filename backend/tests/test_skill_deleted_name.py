"""软删除改名释放 name 唯一约束：_deleted_name 截断规则 + 查重拒绝已删除占名。"""
import pytest
from fastapi import HTTPException

from app.skills.router import _check_name_free, _deleted_name


def test_deleted_name_frees_unique_name():
    assert _deleted_name("bi-api", 42) == "bi-api--del-42"
    # 长名截断到 64：suffix = '--del-42'（8 字符），原名留 56
    long_name = "a" * 64
    renamed = _deleted_name(long_name, 42)
    assert len(renamed) == 64
    assert renamed.endswith("--del-42")
    # 不同 id 后缀不同 → 同名删除两次也不互撞
    assert _deleted_name("bi-api", 1) != _deleted_name("bi-api", 2)


class _FakeSkill:
    def __init__(self, deleted):
        self.deleted_at = deleted


def test_check_name_free():
    _check_name_free("x", None)  # 无冲突：不抛
    with pytest.raises(HTTPException) as e1:
        _check_name_free("x", _FakeSkill(deleted=None))  # 活技能占名
    assert "已存在" in e1.value.detail
    with pytest.raises(HTTPException) as e2:
        _check_name_free("x", _FakeSkill(deleted="2026-08-31"))  # 软删除占名
    assert "已删除技能占用" in e2.value.detail
