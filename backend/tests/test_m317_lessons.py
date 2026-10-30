"""M3.17 自进化一期纯逻辑测试：append_lesson（经验教训区增量追加）。

覆盖：空 MEMORY 建区 / 已有区追加 / 区后标题不串位 / 区在文末 / 满员抛错 / 条目规整。
"""
import pytest

from app.runtime.memory import LESSON_SECTION, MAX_LESSONS, append_lesson

L1 = "- [2026-08-17] BOM 换算 | 错误：未确认规格直接换算 → 正确：先出换算映射表"
L2 = "- [2026-08-17] 工作区读取 | 错误：没 list 直接 read → 正确：先 workspace_list"


class TestAppendLesson:
    def test_empty_content_creates_section(self):
        out = append_lesson("", L1)
        assert LESSON_SECTION in out
        assert L1 in out
        assert out.index(LESSON_SECTION) < out.index(L1)

    def test_appends_to_existing_section(self):
        base = f"# 偏好\n\n{LESSON_SECTION}\n{L1}\n"
        out = append_lesson(base, L2)
        assert out.index(L1) < out.index(L2)

    def test_does_not_bleed_into_next_section(self):
        base = f"{LESSON_SECTION}\n{L1}\n\n## 其他区\n- 别动我\n"
        out = append_lesson(base, L2)
        # 新条目在「经验教训」区内，「其他区」内容原样且在 L2 之后
        assert out.index(L2) < out.index("## 其他区")
        assert "- 别动我" in out
        section_body = out[out.index(LESSON_SECTION) : out.index("## 其他区")]
        assert L2 in section_body
        assert "- 别动我" not in section_body

    def test_section_at_eof(self):
        base = f"前言\n\n{LESSON_SECTION}\n{L1}"  # 无尾换行
        out = append_lesson(base, L2)
        assert out.index(L1) < out.index(L2)
        assert "前言" in out

    def test_full_section_raises(self):
        body = "\n".join(f"- [2026-08-0{i % 9 + 1}] 条目{i}" for i in range(MAX_LESSONS))
        base = f"{LESSON_SECTION}\n{body}\n"
        with pytest.raises(ValueError, match="已满"):
            append_lesson(base, L1)

    def test_max_lessons_override(self):
        base = f"{LESSON_SECTION}\n{L1}\n"
        with pytest.raises(ValueError):
            append_lesson(base, L2, max_lessons=1)

    def test_bare_lesson_gets_dash_prefix(self):
        out = append_lesson("", "没写横线的条目")
        assert "\n- 没写横线的条目\n" in out

    def test_surrounding_content_preserved(self):
        base = "# 用户偏好\n- 喜欢表格\n"
        out = append_lesson(base, L1)
        assert out.startswith("# 用户偏好\n- 喜欢表格")
        assert LESSON_SECTION in out
