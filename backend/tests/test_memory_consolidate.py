"""M22c 记忆提炼纯逻辑测试：section_body / replace_section / build_consolidate_prompt / parse_consolidate_output。

覆盖：取区正文与截断边界、整区替换不伤邻区、无区文末新建、提示词组装与超长截断、
输出校验（围栏剥离 / 非条目行整体弃用 / 单条截断 / 条数上限）、与 append_pref 的区共享回归。
"""

from app.runtime.memory import (
    CONSOLIDATE_MAX_NOTE_CHARS,
    MAX_LESSON_CHARS,
    MAX_PREFS,
    PREF_SECTION,
    append_pref,
    build_consolidate_prompt,
    parse_consolidate_output,
    replace_section,
    section_body,
)

LESSON = "## 经验教训"


class TestSectionBody:
    def test_extracts_body(self):
        content = f"# 前言\n\n{PREF_SECTION}\n- [2026-09-01] 条目A\n- [2026-09-02] 条目B\n\n{LESSON}\n- 别动我\n"
        assert section_body(content, PREF_SECTION) == "- [2026-09-01] 条目A\n- [2026-09-02] 条目B"

    def test_missing_section_returns_empty(self):
        assert section_body("# 只有前言\n", PREF_SECTION) == ""
        assert section_body("", PREF_SECTION) == ""

    def test_stops_at_next_heading(self):
        content = f"{PREF_SECTION}\n- 条目A\n{LESSON}\n- 经验X\n"
        assert "经验X" not in section_body(content, PREF_SECTION)

    def test_section_at_eof(self):
        content = f"# 前言\n\n{PREF_SECTION}\n- 条目A\n"
        assert section_body(content, PREF_SECTION) == "- 条目A"


class TestReplaceSection:
    def test_replaces_body_preserves_others(self):
        content = f"# 前言\n\n{PREF_SECTION}\n- 旧条目\n\n{LESSON}\n- 经验X\n"
        out = replace_section(content, PREF_SECTION, "- 新条目A\n- 新条目B")
        assert "- 旧条目" not in out
        assert "- 新条目A" in out and "- 新条目B" in out
        assert "# 前言" in out and LESSON in out and "- 经验X" in out
        assert out.index(PREF_SECTION) < out.index("- 新条目A") < out.index(LESSON)

    def test_creates_section_at_eof_when_missing(self):
        out = replace_section("# 前言\n", PREF_SECTION, "- 条目A")
        assert out.index("# 前言") < out.index(PREF_SECTION) < out.index("- 条目A")

    def test_creates_in_empty_content(self):
        out = replace_section("", PREF_SECTION, "- 条目A")
        assert PREF_SECTION in out and "- 条目A" in out

    def test_replace_section_at_eof(self):
        content = f"{LESSON}\n- 经验X\n\n{PREF_SECTION}\n- 旧条目\n"
        out = replace_section(content, PREF_SECTION, "- 新条目")
        assert "- 旧条目" not in out
        assert out.index(LESSON) < out.index(PREF_SECTION)
        assert out.rstrip().endswith("- 新条目")

    def test_blank_lines_in_body_dropped(self):
        out = replace_section("", PREF_SECTION, "- A\n\n\n- B\n")
        body = section_body(out, PREF_SECTION)
        assert body == "- A\n- B"


class TestBuildConsolidatePrompt:
    def test_contains_existing_and_notes(self):
        msgs = build_consolidate_prompt("- [2026-09-01] 旧约定", [("2026-09-09", "笔记一"), ("2026-09-10", "笔记二")])
        assert msgs[0]["role"] == "system" and msgs[1]["role"] == "user"
        user = msgs[1]["content"]
        assert "【现有条目】" in user and "【原始笔记】" in user
        assert "- [2026-09-01] 旧约定" in user
        assert "- (2026-09-09) 笔记一" in user and "- (2026-09-10) 笔记二" in user
        # 输出纪律在 system 里：纯条目列表、日期格式、禁编造
        assert "- [YYYY-MM-DD]" in msgs[0]["content"]
        assert "不得编造" in msgs[0]["content"]

    def test_empty_existing_shows_placeholder(self):
        msgs = build_consolidate_prompt("", [("2026-09-10", "笔记")])
        assert "（空）" in msgs[1]["content"]

    def test_notes_truncated_to_max_chars(self):
        notes = [("2026-09-10", "x" * 2000) for _ in range(20)]  # 4 万字符
        msgs = build_consolidate_prompt("", notes)
        assert len(msgs[1]["content"]) < CONSOLIDATE_MAX_NOTE_CHARS + 200


class TestParseConsolidateOutput:
    def test_parses_plain_items(self):
        items = parse_consolidate_output("- [2026-09-10] 条目A\n- [2026-09-09] 条目B")
        assert items == ["- [2026-09-10] 条目A", "- [2026-09-09] 条目B"]

    def test_strips_code_fence(self):
        items = parse_consolidate_output("```\n- [2026-09-10] 条目A\n```")
        assert items == ["- [2026-09-10] 条目A"]
        items2 = parse_consolidate_output("```markdown\n- [2026-09-10] 条目A\n```\n")
        assert items2 == ["- [2026-09-10] 条目A"]

    def test_mixed_explanation_returns_none(self):
        assert parse_consolidate_output("好的，以下是条目：\n- [2026-09-10] 条目A") is None
        assert parse_consolidate_output("## 用户偏好与约定\n- [2026-09-10] 条目A") is None

    def test_empty_returns_empty_list(self):
        assert parse_consolidate_output("") == []
        assert parse_consolidate_output("```\n```") == []

    def test_item_truncated_to_max_chars(self):
        long_item = "- " + "长" * (MAX_LESSON_CHARS + 100)
        items = parse_consolidate_output(long_item)
        assert len(items) == 1 and len(items[0]) == MAX_LESSON_CHARS

    def test_capped_at_max_prefs(self):
        text = "\n".join(f"- [2026-09-10] 条目{i}" for i in range(MAX_PREFS + 10))
        items = parse_consolidate_output(text)
        assert len(items) == MAX_PREFS


class TestSharedSectionRegression:
    def test_append_pref_output_readable_by_section_body(self):
        """append_pref 追加的条目必须能被 section_body 读回（提炼循环的闭环前提）。"""
        content = append_pref("", "- [2026-09-10] 喜欢表格输出")
        assert section_body(content, PREF_SECTION) == "- [2026-09-10] 喜欢表格输出"

    def test_replace_then_append_roundtrip(self):
        """提炼整区替换后，append_pref 还能继续往后追加。"""
        content = replace_section("", PREF_SECTION, "- [2026-09-10] 提炼条目")
        out = append_pref(content, "- [2026-09-11] 新增约定")
        body = section_body(out, PREF_SECTION)
        assert "提炼条目" in body and "新增约定" in body
        assert body.index("提炼条目") < body.index("新增约定")
