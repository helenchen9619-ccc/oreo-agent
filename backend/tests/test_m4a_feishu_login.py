# M4a 飞书免登：authorize-url 端点纯逻辑测试（直接调函数，不起服务）。
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.auth import router as auth_router


class TestAuthorizeUrl:
    @pytest.mark.asyncio
    async def test_unconfigured_400(self, monkeypatch):
        monkeypatch.setattr(auth_router, "settings", SimpleNamespace(FEISHU_APP_ID=""))
        with pytest.raises(HTTPException) as exc:
            await auth_router.feishu_authorize_url(redirect_uri="http://localhost:5173/login")
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_url_contains_redirect(self, monkeypatch):
        # router 与 feishu 模块各自持有 settings 引用，都要打桩
        monkeypatch.setattr(auth_router, "settings", SimpleNamespace(FEISHU_APP_ID="cli_test"))
        monkeypatch.setattr(auth_router.feishu, "settings", SimpleNamespace(FEISHU_APP_ID="cli_test"))
        out = await auth_router.feishu_authorize_url(redirect_uri="http://localhost:5173/login")
        assert "authen/v1/authorize" in out["url"]
        assert "app_id=cli_test" in out["url"]
        assert "redirect_uri=" in out["url"]
        assert "scope=" not in out["url"]  # 普通登录不带 scope（回归保护：避免报错打死登录）

    @pytest.mark.asyncio
    async def test_upgrade_url_carries_m7_scopes(self, monkeypatch):
        """M7：upgrade=1 显式申请数据 scope（空格分隔 %20 编码，含 offline_access 才有 refresh_token）。"""
        monkeypatch.setattr(auth_router, "settings", SimpleNamespace(FEISHU_APP_ID="cli_test"))
        monkeypatch.setattr(auth_router.feishu, "settings", SimpleNamespace(FEISHU_APP_ID="cli_test"))
        out = await auth_router.feishu_authorize_url(redirect_uri="http://localhost:5173/login", upgrade=True)
        url = out["url"]
        assert "scope=" in url
        for s in ("docx:document:readonly", "wiki:wiki:readonly", "search:docs:read",
                  "im:message", "im:chat:readonly", "offline_access", "contact:contact.base:readonly",
                  "contact:user.base:readonly", "contact:user.employee_id:readonly",
                  "search:message", "im:message.p2p_msg:get_as_user",
                  "mail:user_mailbox.message:readonly", "mail:user_mailbox.message.subject:read",
                  "mail:user_mailbox.message.body:read", "mail:user_mailbox.mail_contact:read",
                  "mail:user_mailbox.mail_contact.mail_address:read",
                  "mail:user_mailbox.folder:read"):
            assert s in url
        assert "%20" in url  # 空格分隔编码
