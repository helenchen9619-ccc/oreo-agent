"""M3.12 组织与账号管理逻辑测试：部门防环（creates_cycle）+ 管理员防锁死（check_admin_lockout）+ 智书 user_id 校验。"""
import pytest
from fastapi import HTTPException

from app.auth.org_router import check_admin_lockout, creates_cycle, validate_zhishu_user_id
from app.models.user import User

# 部门树：1(根) → 2 → 3；4 是另一根
PARENTS = {1: None, 2: 1, 3: 2, 4: None}


class TestCreatesCycle:
    def test_move_root_under_own_descendant(self):
        assert creates_cycle(1, 3, PARENTS) is True  # 1 挂到 3 下：3→2→1 成环

    def test_move_to_self(self):
        assert creates_cycle(2, 2, PARENTS) is True

    def test_move_to_direct_child(self):
        assert creates_cycle(2, 3, PARENTS) is True

    def test_move_to_other_tree_ok(self):
        assert creates_cycle(2, 4, PARENTS) is False

    def test_move_to_top_level_ok(self):
        assert creates_cycle(3, None, PARENTS) is False

    def test_move_leaf_anywhere_ok(self):
        assert creates_cycle(3, 4, PARENTS) is False

    def test_broken_chain_terminates(self):
        # 父链指向不存在的部门（脏数据）时不死循环、不误判
        assert creates_cycle(1, 9, {9: 8, 8: None}) is False


def _user(uid, role="member", status="active"):
    u = User()
    u.id = uid
    u.role = role
    u.status = status
    return u


class TestAdminLockout:
    def test_self_disable_rejected(self):
        with pytest.raises(HTTPException):
            check_admin_lockout(1, _user(1, "admin"), "admin", "disabled", other_active_admins=5)

    def test_self_demote_rejected(self):
        with pytest.raises(HTTPException):
            check_admin_lockout(1, _user(1, "admin"), "member", "active", other_active_admins=5)

    def test_self_rename_untouched_ok(self):
        check_admin_lockout(1, _user(1, "admin"), "admin", "active", other_active_admins=0)  # 不抛

    def test_demote_last_admin_rejected(self):
        with pytest.raises(HTTPException) as e:
            check_admin_lockout(9, _user(1, "admin"), "member", "active", other_active_admins=0)
        assert "至少" in e.value.detail

    def test_disable_last_admin_rejected(self):
        with pytest.raises(HTTPException):
            check_admin_lockout(9, _user(1, "admin"), "admin", "disabled", other_active_admins=0)

    def test_demote_admin_ok_when_others_exist(self):
        check_admin_lockout(9, _user(1, "admin"), "member", "active", other_active_admins=1)

    def test_disable_member_always_ok(self):
        check_admin_lockout(1, _user(2, "member"), "member", "disabled", other_active_admins=0)

    def test_promote_member_ok(self):
        check_admin_lockout(1, _user(2, "member"), "admin", "active", other_active_admins=0)

    def test_disabled_admin_not_counted_as_lockout_risk(self):
        # 目标本是 disabled admin：再改角色不触发"最后管理员"判断（他已不可用）
        check_admin_lockout(9, _user(1, "admin", "disabled"), "member", "disabled", other_active_admins=0)


class TestZhishuUserId:
    def test_valid_lowercase_alnum(self):
        assert validate_zhishu_user_id("944f4998") == "944f4998"

    def test_trim_and_normalize(self):
        assert validate_zhishu_user_id("  6f9c5539  ") == "6f9c5539"

    def test_empty_means_clear(self):
        assert validate_zhishu_user_id("") is None
        assert validate_zhishu_user_id(None) is None
        assert validate_zhishu_user_id("   ") is None

    def test_uppercase_or_symbols_rejected(self):
        with pytest.raises(HTTPException):
            validate_zhishu_user_id("944F4998")
        with pytest.raises(HTTPException):
            validate_zhishu_user_id("944f-4998")
