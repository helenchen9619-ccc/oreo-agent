"""M3.16 管理端数字人列表扩展字段测试：_admin_agent_item 纯函数组装逻辑。

覆盖：属主账号/部门名解析、无部门/无属主/无来源模板的 None 分支、created_at 序列化。
"""
from datetime import UTC, datetime

from app.agents.router import _admin_agent_item
from app.models.agent import Agent
from app.models.user import User


def _agent(**kw):
    a = Agent()
    a.id = kw.get("id", 1)
    a.name = kw.get("name", "实例")
    a.avatar = None
    a.description = kw.get("description", "")
    a.status = kw.get("status", "active")
    a.is_template = kw.get("is_template", False)
    a.template_id = kw.get("template_id")
    a.owner_user_id = kw.get("owner_user_id")
    a.is_master = kw.get("is_master", False)
    a.default_model_id = None
    a.model_allowlist = None
    a.allow_code_exec = False
    a.allow_web_search = False
    a.max_loop_rounds = 16
    a.created_at = kw.get("created_at", datetime(2026, 8, 17, 9, 30, tzinfo=UTC))
    return a


def _user(uid, name="张三", username="zhangsan", department_id=None):
    u = User()
    u.id = uid
    u.name = name
    u.username = username
    u.department_id = department_id
    return u


class TestAdminAgentItem:
    def test_owner_with_department(self):
        item = _admin_agent_item(
            _agent(owner_user_id=7, template_id=10),
            _user(7, department_id=3),
            {3: "市场部"},
            {10: "市场研究员"},
        )
        assert item["owner_name"] == "张三"
        assert item["owner_username"] == "zhangsan"
        assert item["owner_department_id"] == 3
        assert item["owner_department_name"] == "市场部"
        assert item["template_name"] == "市场研究员"
        assert item["created_at"] == "2026-08-17T09:30:00+00:00"

    def test_owner_without_department(self):
        item = _admin_agent_item(_agent(owner_user_id=7), _user(7), {}, {})
        assert item["owner_department_id"] is None
        assert item["owner_department_name"] is None

    def test_owner_department_missing_from_names(self):
        # 属主挂了已删除/不存在的部门：不抛错，部门名为 None
        item = _admin_agent_item(_agent(owner_user_id=7), _user(7, department_id=99), {}, {})
        assert item["owner_department_id"] == 99
        assert item["owner_department_name"] is None

    def test_no_owner(self):
        item = _admin_agent_item(_agent(), None, {}, {})
        assert item["owner_name"] is None
        assert item["owner_username"] is None
        assert item["owner_department_name"] is None

    def test_no_template(self):
        item = _admin_agent_item(_agent(owner_user_id=7), _user(7), {}, {})
        assert item["template_name"] is None

    def test_template_deleted_gives_none(self):
        # template_id 指向已删除模板（不在 tpl_names 里）→ None，不残留脏 id
        item = _admin_agent_item(_agent(template_id=42), None, {}, {})
        assert item["template_id"] == 42
        assert item["template_name"] is None

    def test_created_at_none_safe(self):
        item = _admin_agent_item(_agent(created_at=None), None, {}, {})
        assert item["created_at"] is None

    def test_base_fields_intact(self):
        # _agent_out 原有字段不被扩展破坏
        item = _admin_agent_item(_agent(name="alice的市场研究员", is_master=True), None, {}, {})
        assert item["name"] == "alice的市场研究员"
        assert item["is_master"] is True
        assert item["max_loop_rounds"] == 16
