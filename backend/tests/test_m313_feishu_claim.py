"""M3.13 飞书鉴权认领决策测试：decide_feishu_login 纯函数。

策略（用户拍板）：open_id 为唯一锚点；未绑定时按姓名认领预建账号（恰好 1 个候选才认）；
默认拒绝自动建档，仅 ADMIN_FEISHU_OPEN_IDS 白名单豁免（冷启动兜底）。
"""
from app.auth.router import _has_content_scope, decide_feishu_login
from app.models.user import User


def _user(uid, name="张三", status="active", open_id=None, role="member"):
    u = User()
    u.id = uid
    u.name = name
    u.status = status
    u.feishu_open_id = open_id
    u.role = role
    return u


class TestBoundUser:
    def test_bound_active_login(self):
        assert decide_feishu_login(_user(1, open_id="ou_x"), [], is_admin_openid=False) == "login"

    def test_bound_disabled_rejected(self):
        # 已绑定但账号被禁用 → 直接 403，不发"全 401"的废 token
        assert decide_feishu_login(_user(1, status="disabled", open_id="ou_x"), [], is_admin_openid=False) == "reject_disabled"

    def test_bound_ignores_candidates(self):
        # 已绑定时候选列表不应参与决策（调用方也不会查，双保险）
        assert decide_feishu_login(_user(1, open_id="ou_x"), [_user(2)], is_admin_openid=False) == "login"


class TestClaim:
    def test_single_candidate_claims(self):
        assert decide_feishu_login(None, [_user(2)], is_admin_openid=False) == "claim"

    def test_single_candidate_admin_openid_still_claims(self):
        # 白名单 open_id 认领预建账号：认领 + 路由内提 admin，不是另建档
        assert decide_feishu_login(None, [_user(2)], is_admin_openid=True) == "claim"

    def test_ambiguous_rejected(self):
        assert decide_feishu_login(None, [_user(2), _user(3)], is_admin_openid=False) == "reject_ambiguous"

    def test_ambiguous_even_for_admin_openid(self):
        # 白名单不豁免歧义——认领错人比进不来更糟
        assert decide_feishu_login(None, [_user(2), _user(3)], is_admin_openid=True) == "reject_ambiguous"


class TestNoCandidate:
    def test_admin_openid_creates_admin(self):
        # 冷启动兜底：第一个管理员必须先能进来建号
        assert decide_feishu_login(None, [], is_admin_openid=True) == "create_admin"

    def test_non_whitelist_rejected(self):
        # 默认拒绝：不再自动建档 member（一人两号根源）
        assert decide_feishu_login(None, [], is_admin_openid=False) == "reject_unauthorized"


class TestContentScopeGuard:
    """M9b 修复：普通登录（最小 scope）不得覆盖升级授权的内容 token。"""

    def test_content_scope_detected(self):
        assert _has_content_scope("mail:user_mailbox.message:readonly") is True
        assert _has_content_scope("im:message contact:contact.base:readonly") is True
        assert _has_content_scope("approval:approval.list:readonly docx:document:readonly") is True

    def test_minimal_scope_not_content(self):
        # 普通登录不带 scope → 飞书只发最小 auth:user.id:read
        assert _has_content_scope("auth:user.id:read") is False
        assert _has_content_scope("") is False
        assert _has_content_scope(None) is False
