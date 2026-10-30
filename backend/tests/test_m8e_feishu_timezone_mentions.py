"""M8e 逻辑测试：飞书时间显示北京时间 + 群聊 @提及 还原真实姓名。

契约：
- 消息/审批时间戳格式化必须显式带 Asia/Shanghai（云上容器是 UTC，naive fromtimestamp 会差 8 小时）
- text 消息正文里的 @_user_N 是匿名占位符，真实姓名在 body.mentions（key→name），渲染时还原成 @姓名
- client._normalize_msg 必须把 body.mentions 透传（之前被丢弃，导致无处还原）
"""
import inspect

from app.feishu.client import _normalize_msg
from app.runtime import tools


# ---------- 问题 1：时区（北京时间） ----------

def test_format_msgs_uses_beijing_time():
    # send_time=0（epoch 0 = 1970-01-01 08:00 北京时间）——断言与测试机本地时区无关，确定性
    lines = tools._feishu_format_msgs(
        [{"message_id": "1", "msg_type": "text", "text": '{"text":"hi"}', "send_time": 0, "sender": "ou_x"}],
        {"ou_x": "张三"},
        {},
    )
    assert lines == ["01-01 08:00 张三：hi"]


def test_approval_ts_uses_beijing_time():
    assert tools._approval_ts(0) == "01-01 08:00"


def test_timezone_source_uses_zoneinfo():
    assert 'ZoneInfo("Asia/Shanghai")' in inspect.getsource(tools._feishu_format_msgs)
    assert 'ZoneInfo("Asia/Shanghai")' in inspect.getsource(tools._approval_ts)


# ---------- 问题 2：@提及 还原真实姓名 ----------

def test_normalize_msg_keeps_mentions():
    m = _normalize_msg({
        "msg_type": "text",
        "body": {"content": '{"text":"hi @_user_1"}', "mentions": [{"key": "@_user_1", "name": "苏晴"}]},
        "create_time": "1700000000000",
        "sender": {"id": "ou_s"},
        "message_id": "m1",
        "chat_id": "c1",
    })
    assert m["mentions"] == [{"key": "@_user_1", "name": "苏晴"}]


def test_normalize_msg_mentions_empty_when_absent():
    m = _normalize_msg({
        "msg_type": "text",
        "body": {"content": '{"text":"hi"}'},
        "create_time": "1700000000000",
        "sender": {"id": "ou_s"},
        "message_id": "m1",
        "chat_id": "c1",
    })
    assert m["mentions"] == []


def test_text_mention_resolved():
    out = tools._feishu_msg_text("text", '{"text":"hi @_user_1"}', [{"key": "@_user_1", "name": "苏晴"}])
    assert out == "hi @苏晴"


def test_text_no_mentions_unchanged():
    assert tools._feishu_msg_text("text", '{"text":"hi @_user_1"}') == "hi @_user_1"
