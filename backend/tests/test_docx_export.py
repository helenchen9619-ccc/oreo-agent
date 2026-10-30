"""M18 逻辑测试：md→docx 导出（转换器 + 导出端点）+ PLATFORM_LAYER 交付纪律。

转换器：python-docx 读回断言结构（标题/粗体/列表/引用/代码块/GFM 表格/中文）。
端点：_FakeDB + tmp_path 落真文件，直接调 handler。
"""
import io
from types import SimpleNamespace

import pytest
from docx import Document
from fastapi import HTTPException

from app.runtime.prompt import PLATFORM_LAYER
from app.workspace import router as ws_router
from app.workspace.docx_export import DOCX_MIME, md_to_docx


def _doc(text: str) -> Document:
    return Document(io.BytesIO(md_to_docx(text)))


def _paras(doc: Document):
    return [(p.style.name, p.text) for p in doc.paragraphs]


# ---------- md_to_docx 转换器 ----------


def test_headings_and_levels():
    doc = _doc("# 一级\n\n## 二级\n\n##### 五级\n\n正文")
    paras = _paras(doc)
    assert ("Heading 1", "一级") in paras
    assert ("Heading 2", "二级") in paras
    assert ("Heading 4", "五级") in paras  # 5/6 级并入 Heading 4
    assert ("Normal", "正文") in paras


def test_bold_inline():
    doc = _doc("本周回款 **120 万**，超额完成")
    para = doc.paragraphs[0]
    bold_runs = [r.text for r in para.runs if r.bold]
    assert bold_runs == ["120 万"]
    assert para.text == "本周回款 120 万，超额完成"


def test_lists_bullet_and_ordered():
    doc = _doc("- 苹果\n- 香蕉\n\n1. 第一步\n2. 第二步")
    paras = _paras(doc)
    assert ("List Bullet", "苹果") in paras and ("List Bullet", "香蕉") in paras
    assert ("List Number", "第一步") in paras and ("List Number", "第二步") in paras


def test_quote_is_italic_indented():
    doc = _doc("> 客户说要再想想")
    para = doc.paragraphs[0]
    assert para.text == "客户说要再想想"
    assert para.paragraph_format.left_indent is not None
    assert all(r.italic for r in para.runs)


def test_code_block_monospace_with_breaks():
    doc = _doc("```\nline1\nline2\n```")
    para = doc.paragraphs[0]
    assert "line1" in para.text and "line2" in para.text
    assert para.runs[0].font.name == "Consolas"


def test_unclosed_code_fence_still_renders():
    doc = _doc("前文\n\n```\n未闭合的代码")
    assert any("未闭合的代码" in p.text for p in doc.paragraphs)


def test_gfm_table():
    doc = _doc("| 点位 | 评分 |\n| --- | --- |\n| 万象城 | 4.5 |\n| 印象城 | 3.8 |")
    assert len(doc.tables) == 1
    table = doc.tables[0]
    assert len(table.rows) == 3 and len(table.columns) == 2
    assert table.rows[0].cells[0].text == "点位"
    assert table.rows[0].cells[0].paragraphs[0].runs[0].bold  # 表头加粗
    assert table.rows[2].cells[1].text == "3.8"


def test_cjk_font_set():
    doc = _doc("中文正文")
    normal = doc.styles["Normal"]
    assert normal.font.name == "等线"
    rfonts = normal.element.rPr.rFonts
    from docx.oxml.ns import qn

    assert rfonts.get(qn("w:eastAsia")) == "等线"


def test_divider_and_blank_lines_ignored():
    doc = _doc("上文\n\n---\n\n下文")
    texts = [p.text for p in doc.paragraphs]
    assert "---" not in texts and "上文" in texts and "下文" in texts


# ---------- PLATFORM_LAYER 交付纪律 ----------


def test_platform_layer_teaches_delivery_format():
    assert "导出为 Word" in PLATFORM_LAYER
    assert ".docx" in PLATFORM_LAYER
    bullets = [ln for ln in PLATFORM_LAYER.splitlines() if ln.startswith("- ")]
    assert len(bullets) <= 7  # test_m6_logic 的硬上限，workspace 条扩写不新增条目


# ---------- export.docx 端点 ----------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, rows=None):
        self.rows = rows or []

    async def execute(self, *a, **k):
        return _FakeResult(self.rows)


def _file_record(tmp_path, filename="reports/周报.md", content="# 周报\n\n本周一切顺利"):
    p = tmp_path / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return SimpleNamespace(
        id=1, agent_id=2, user_id=1, filename=filename, mime="text/markdown",
        storage_path=str(p), deleted_at=None,
    )


@pytest.mark.asyncio
async def test_export_success(tmp_path):
    f = _file_record(tmp_path)
    resp = await ws_router.export_file_docx(agent_id=2, file_id=1, user=SimpleNamespace(id=1), db=_FakeDB([f]))
    assert resp.media_type == DOCX_MIME
    assert "attachment" in resp.headers["content-disposition"]
    assert ".docx" in resp.headers["content-disposition"]
    doc = Document(io.BytesIO(resp.body))  # 字节流是合法 docx
    assert ("Heading 1", "周报") in _paras(doc)


@pytest.mark.asyncio
async def test_export_rejects_non_markdown(tmp_path):
    f = _file_record(tmp_path, filename="notes.txt")
    with pytest.raises(HTTPException) as exc:
        await ws_router.export_file_docx(agent_id=2, file_id=1, user=SimpleNamespace(id=1), db=_FakeDB([f]))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_export_404_when_not_found():
    with pytest.raises(HTTPException) as exc:
        await ws_router.export_file_docx(agent_id=2, file_id=99, user=SimpleNamespace(id=1), db=_FakeDB([]))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_export_404_when_disk_file_gone(tmp_path):
    f = _file_record(tmp_path)
    (tmp_path / f.filename).unlink()
    with pytest.raises(HTTPException) as exc:
        await ws_router.export_file_docx(agent_id=2, file_id=1, user=SimpleNamespace(id=1), db=_FakeDB([f]))
    assert exc.value.status_code == 404
