"""M4b：飞书群机器人 webhook 推送 — 纯函数 + 组包正确性（httpx 全 mock，不出网）。"""
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.scheduler.feishu_push as fp


def _mock_client(payload: dict):
    resp = MagicMock()
    resp.json = lambda: payload
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


class TestGenSign:
    def test_stable_known_vector(self):
        # 官方算法：Base64(HMAC-SHA256(key="ts\nsecret", msg=空)) — 固定输入必须恒定输出
        s1 = fp.gen_sign("secret-abc", 1700000000)
        s2 = fp.gen_sign("secret-abc", 1700000000)
        assert s1 == s2 and isinstance(s1, str) and len(s1) == 44  # sha256 → base64 44 字符

    def test_secret_or_ts_changes_sign(self):
        base = fp.gen_sign("s", 100)
        assert fp.gen_sign("s2", 100) != base
        assert fp.gen_sign("s", 101) != base


class TestValidateWebhook:
    GOOD = fp.WEBHOOK_PREFIX + "abcd-1234"

    def test_good(self):
        assert fp.validate_webhook(self.GOOD) is None

    def test_wrong_prefix(self):
        assert fp.validate_webhook("https://example.com/hook/abc") is not None
        assert fp.validate_webhook("http://open.feishu.cn/open-apis/bot/v2/hook/abc") is not None  # 必须 https

    def test_missing_hook_id(self):
        assert fp.validate_webhook(fp.WEBHOOK_PREFIX) is not None


class TestTruncate:
    def test_short_untouched(self):
        assert fp.truncate_push("abc") == "abc"

    def test_boundary_exact_limit(self):
        text = "x" * fp.MAX_PUSH_CHARS
        assert fp.truncate_push(text) == text

    def test_over_limit_appends_marker(self):
        out = fp.truncate_push("x" * (fp.MAX_PUSH_CHARS + 100))
        assert out.startswith("x" * fp.MAX_PUSH_CHARS)
        assert "已截断" in out


class TestPushFeishuText:
    @pytest.mark.asyncio
    async def test_ok_payload_no_secret(self, monkeypatch):
        client = _mock_client({"code": 0, "msg": "success"})
        monkeypatch.setattr(fp.httpx, "AsyncClient", lambda **kw: client)

        err = await fp.push_feishu_text(fp.WEBHOOK_PREFIX + "h1", "你好")
        assert err is None
        body = client.post.call_args.kwargs["json"]
        assert body == {"msg_type": "text", "content": {"text": "你好"}}
        assert "sign" not in body and "timestamp" not in body

    @pytest.mark.asyncio
    async def test_ok_payload_with_secret(self, monkeypatch):
        client = _mock_client({"code": 0, "msg": "success"})
        monkeypatch.setattr(fp.httpx, "AsyncClient", lambda **kw: client)

        err = await fp.push_feishu_text(fp.WEBHOOK_PREFIX + "h1", "hi", secret="s3cret")
        assert err is None
        body = client.post.call_args.kwargs["json"]
        assert body["sign"] == fp.gen_sign("s3cret", int(body["timestamp"]))

    @pytest.mark.asyncio
    async def test_feishu_error_returned_not_raised(self, monkeypatch):
        client = _mock_client({"code": 19021, "msg": "sign match fail"})
        monkeypatch.setattr(fp.httpx, "AsyncClient", lambda **kw: client)

        err = await fp.push_feishu_text(fp.WEBHOOK_PREFIX + "h1", "hi", secret="bad")
        assert "19021" in err and "sign match fail" in err

    @pytest.mark.asyncio
    async def test_network_error_returned_not_raised(self, monkeypatch):
        client = MagicMock()
        client.post = AsyncMock(side_effect=TimeoutError("boom"))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr(fp.httpx, "AsyncClient", lambda **kw: client)

        err = await fp.push_feishu_text(fp.WEBHOOK_PREFIX + "h1", "hi")
        assert "推送请求失败" in err

    @pytest.mark.asyncio
    async def test_long_text_truncated_in_payload(self, monkeypatch):
        client = _mock_client({"code": 0, "msg": "success"})
        monkeypatch.setattr(fp.httpx, "AsyncClient", lambda **kw: client)

        await fp.push_feishu_text(fp.WEBHOOK_PREFIX + "h1", "x" * 5000)
        text = client.post.call_args.kwargs["json"]["content"]["text"]
        assert len(text) < 5000 and "已截断" in text


class TestSplitForCards:
    def test_short_single_chunk(self):
        assert fp.split_for_cards("abc\ndef") == ["abc\ndef"]

    def test_empty_returns_placeholder(self):
        assert fp.split_for_cards("") == ["(无内容)"]

    def test_packing_respects_budget_and_lossless(self):
        lines = [f"行{i}" + "字" * 100 for i in range(50)]  # 每行 ~104 字符
        text = "\n".join(lines)
        chunks = fp.split_for_cards(text, per_card=500)
        assert all(len(c) <= 500 for c in chunks)
        assert "\n".join(chunks) == text  # 拼接无损
        assert len(chunks) > 1

    def test_single_overlong_line_hard_cut(self):
        chunks = fp.split_for_cards("x" * 1200, per_card=500)
        assert chunks == ["x" * 500, "x" * 500, "x" * 200]

    def test_exact_boundary(self):
        assert fp.split_for_cards("x" * 500, per_card=500) == ["x" * 500]


class TestBuildCard:
    def test_structure_ok(self):
        card = fp.build_card("标题", "正文", ok=True)
        assert card["header"]["template"] == "blue"
        assert card["header"]["title"] == {"tag": "plain_text", "content": "标题"}
        assert card["elements"] == [{"tag": "div", "text": {"tag": "lark_md", "content": "正文"}}]

    def test_error_red(self):
        assert fp.build_card("t", "c", ok=False)["header"]["template"] == "red"


class TestPushFeishuCard:
    @pytest.mark.asyncio
    async def test_single_card_payload(self, monkeypatch):
        client = _mock_client({"StatusCode": 0, "StatusMessage": "success"})
        monkeypatch.setattr(fp.httpx, "AsyncClient", lambda **kw: client)

        err = await fp.push_feishu_card(fp.WEBHOOK_PREFIX + "h1", "【日报】运行成功", "正文")
        assert err is None
        body = client.post.call_args.kwargs["json"]
        assert body["msg_type"] == "interactive"
        assert body["card"]["header"]["title"]["content"] == "【日报】运行成功"
        assert client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_long_content_split_into_numbered_cards(self, monkeypatch):
        client = _mock_client({"StatusCode": 0, "StatusMessage": "success"})
        monkeypatch.setattr(fp.httpx, "AsyncClient", lambda **kw: client)
        monkeypatch.setattr(fp, "CARD_BODY_CHARS", 100)  # 缩小预算好构造

        content = "\n".join("行" + "字" * 60 for _ in range(5))
        err = await fp.push_feishu_card(fp.WEBHOOK_PREFIX + "h1", "标题", content)
        assert err is None
        assert client.post.call_count > 1
        titles = [c.kwargs["json"]["card"]["header"]["title"]["content"] for c in client.post.call_args_list]
        assert titles[0].startswith("标题 (1/") and titles[-1].startswith(f"标题 ({len(titles)}/")

    @pytest.mark.asyncio
    async def test_secret_signs_every_card(self, monkeypatch):
        client = _mock_client({"StatusCode": 0})
        monkeypatch.setattr(fp.httpx, "AsyncClient", lambda **kw: client)
        monkeypatch.setattr(fp, "CARD_BODY_CHARS", 10)

        err = await fp.push_feishu_card(fp.WEBHOOK_PREFIX + "h1", "t", "x" * 25, secret="s3")
        assert err is None
        assert client.post.call_count == 3
        for c in client.post.call_args_list:
            body = c.kwargs["json"]
            assert body["sign"] == fp.gen_sign("s3", int(body["timestamp"]))

    @pytest.mark.asyncio
    async def test_feishu_error_stops_and_returns(self, monkeypatch):
        client = _mock_client({"StatusCode": 19021, "StatusMessage": "sign match fail"})
        monkeypatch.setattr(fp.httpx, "AsyncClient", lambda **kw: client)
        monkeypatch.setattr(fp, "CARD_BODY_CHARS", 10)

        err = await fp.push_feishu_card(fp.WEBHOOK_PREFIX + "h1", "t", "x" * 25, secret="bad")
        assert "19021" in err and "sign match fail" in err
        assert client.post.call_count == 1  # 首片失败即停，不继续发
