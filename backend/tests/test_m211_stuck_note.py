"""M21.1 逻辑测试：当前任务注记（_append_current_task_note）+ 执行纪律强化契约。

会话 167 教训：用户换题三次（引用新文件问物料用量）+ 质问一次「你都没回答我」，
模型四轮都在复读上一话题（circle）——自我模仿 + 自写教训进 MEMORY 反噬。
修法：每轮在最后一条用户消息之后追加当前任务注记（离生成最近=最高显著性），
解析到引用文件逐个点名要求先读，检出元抱怨时升级措辞。
"""
from app.runtime.engine import _append_current_task_note
from app.runtime.prompt import PLATFORM_LAYER


def _last_user(msgs):
    return [m for m in msgs if m["role"] == "user"][-1]


# ---------- 注记注入 ----------

def test_plain_message_gets_base_note():
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "今天天气怎么样"}]
    out = _append_current_task_note(msgs)
    assert len(out) == 3
    assert out[-1]["role"] == "system" and "当前任务" in out[-1]["content"]
    assert "当前唯一任务" in out[-1]["content"]
    assert "workspace_read" not in out[-1]["content"]  # 无引用文件不点名


def test_refs_marker_names_files_and_forces_read():
    text = "【我引用了工作区文件：出入库汇总表A.xlsx、出入库汇总表B.xlsx，请用 workspace_read 读取后再回答】 这两天物料用量变化？"
    msgs = [{"role": "user", "content": text}]
    out = _append_current_task_note(msgs)
    note = out[-1]["content"]
    assert "出入库汇总表A.xlsx" in note and "出入库汇总表B.xlsx" in note
    assert "workspace_read" in note


def test_meta_complaint_escalates():
    for text in ("为什么你一直在上一个问题里？你都没回答我", "答非所问，重新看我问的", "你又跑题了"):
        out = _append_current_task_note([{"role": "user", "content": text}])
        assert "停止沿用上一轮" in out[-1]["content"], text


def test_last_message_not_user_no_note():
    msgs = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好", "tool_calls": []},
    ]
    out = _append_current_task_note(msgs)
    assert out == msgs  # 原样返回（turn 内 tool 循环不应重复注入）


def test_vision_content_list_still_parsed():
    """带图片附件时 content 是 parts 数组，文本部分里的引用标记也要能解析。"""
    content = [
        {"type": "text", "text": "【我引用了工作区文件：日报.xlsx，请用 workspace_read 读取后再回答】 分析一下"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}},
    ]
    out = _append_current_task_note([{"role": "user", "content": content}])
    assert "日报.xlsx" in out[-1]["content"]


# ---------- PLATFORM_LAYER 执行纪律强化契约 ----------

def test_platform_layer_topic_switch_wording():
    assert "当前唯一任务" in PLATFORM_LAYER
    assert "复读" in PLATFORM_LAYER


def test_platform_layer_rule_count_unchanged():
    """M6b 瘦身纪律：通用规则 7 条上限——执行纪律是改不是加。"""
    rules = [ln for ln in PLATFORM_LAYER.splitlines() if ln.startswith("- ")]
    assert len(rules) <= 7
