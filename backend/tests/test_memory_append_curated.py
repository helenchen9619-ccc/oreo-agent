"""「记住」quick fix 纯逻辑测试：append_pref（用户偏好与约定区增量追加）+ 工具接线。

覆盖：空 MEMORY 建区 / 已有区追加 / 区后标题不串位 / 满员抛错 / 条目规整 /
工具 schema 注册 / PLATFORM_LAYER 路由规则（显式记住 → memory_append_curated）。
"""

from app.runtime.memory import MAX_PREFS, PREF_SECTION, append_pref
from app.runtime.prompt import PLATFORM_LAYER
from app.runtime.tools import TOOL_SCHEMAS

P1 = "- [2026-09-10] 喜欢表格输出"
P2 = "- [2026-09-10] 报告标题统一带日期"


class TestAppendPref:
    def test_empty_content_creates_section(self):
        out = append_pref("", P1)
        assert PREF_SECTION in out
        assert P1 in out
        assert out.index(PREF_SECTION) < out.index(P1)

    def test_appends_to_existing_section(self):
        base = f"# 前言\n\n{PREF_SECTION}\n{P1}\n"
        out = append_pref(base, P2)
        assert out.index(P1) < out.index(P2)

    def test_does_not_bleed_into_next_section(self):
        base = f"{PREF_SECTION}\n{P1}\n\n## 经验教训\n- 别动我\n"
        out = append_pref(base, P2)
        assert out.index(P2) < out.index("## 经验教训")
        section_body = out[out.index(PREF_SECTION) : out.index("## 经验教训")]
        assert P2 in section_body
        assert "- 别动我" not in section_body

    def test_coexists_with_lesson_section(self):
        """偏好区与经验教训区互不干擾（同一 MEMORY.md 内两个追加区）。"""
        from app.runtime.memory import LESSON_SECTION, append_lesson

        base = append_pref("", P1)
        out = append_lesson(base, "- [2026-09-10] 场景 | 错误→正确")
        assert P1 in out and LESSON_SECTION in out
        assert out.index(P1) < out.index(LESSON_SECTION)

    def test_full_section_raises(self):
        body = "\n".join(f"- [2026-09-{i % 28 + 1:02d}] 条目{i}" for i in range(MAX_PREFS))
        base = f"{PREF_SECTION}\n{body}\n"
        try:
            append_pref(base, P1)
            raise AssertionError("应当抛 ValueError")
        except ValueError as e:
            assert "已满" in str(e)

    def test_bare_entry_gets_dash_prefix(self):
        out = append_pref("", "没写横线的约定")
        assert "\n- 没写横线的约定\n" in out

    def test_surrounding_content_preserved(self):
        base = "# 策展记忆\n- 核心背景\n"
        out = append_pref(base, P1)
        assert out.startswith("# 策展记忆\n- 核心背景")
        assert PREF_SECTION in out


class TestWiring:
    def test_tool_schema_registered(self):
        names = {s["function"]["name"] for s in TOOL_SCHEMAS}
        assert "memory_append_curated" in names
        schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == "memory_append_curated")
        assert schema["function"]["parameters"]["required"] == ["content"]

    def test_platform_layer_routes_explicit_remember(self):
        """PLATFORM_LAYER 记忆条款必须指明：显式「记住」走 memory_append_curated 而非只写笔记。"""
        assert "memory_append_curated" in PLATFORM_LAYER
        assert "记住" in PLATFORM_LAYER
        bullets = [ln for ln in PLATFORM_LAYER.splitlines() if ln.startswith("- ")]
        assert len(bullets) <= 7, f"PLATFORM_LAYER 现有 {len(bullets)} 条（M6b 上限 7）"
