"""轻量 Markdown → .docx 转换器（工作区「导出为 Word」按钮的后端支撑）。

面向 agent 产出的常见结构：# 标题、**粗体**、- / 1. 列表、> 引用、``` 代码块、
GFM 表格、--- 分割线。不做：嵌套列表缩进（拍平）、图片内嵌、复杂混排——
这些超出"让同事能打开看"的目标。
"""
import io
import re

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_ORDERED_RE = re.compile(r"^\d+[.、)]\s+(.*)$")
_BOLD_RE = re.compile(r"(\*\*.+?\*\*)")
# GFM 表格分隔行：| --- | :---: | --- |（允许冒号对齐标记，只允许 - : 空格与 |）
_TABLE_SEP_RE = re.compile(r"^\|?[\s\-:]+\|[\s\-:|]*$")


def _setup_styles(doc: Document) -> None:
    """中文排版：正文等线、标题黑体系；不设 eastAsia 时 Word 对中文会回落到默认西文字体。"""
    normal = doc.styles["Normal"]
    normal.font.name = "等线"
    normal.font.size = Pt(11)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "等线")
    for level in range(1, 5):
        st = doc.styles[f"Heading {level}"]
        st.font.name = "微软雅黑"
        st.font.color.rgb = RGBColor(0, 0, 0)
        st.element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")


def _add_runs(para, text: str) -> None:
    """行内解析：**粗体** → bold run，其余纯文本。"""
    for part in _BOLD_RE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            para.add_run(part[2:-2]).bold = True
        else:
            para.add_run(part)


def _split_row(line: str) -> list[str]:
    """| a | b | → [a, b]；单元格内的 \\| 转义不处理（agent 产出极少用）。"""
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _add_code_block(doc: Document, code_lines: list[str]) -> None:
    para = doc.add_paragraph()
    para.paragraph_format.left_indent = Pt(12)
    for i, ln in enumerate(code_lines):
        run = para.add_run(ln)
        run.font.name = "Consolas"
        run.element.rPr.rFonts.set(qn("w:eastAsia"), "等线")
        run.font.size = Pt(9)
        if i < len(code_lines) - 1:
            run.add_break()


def _add_table(doc: Document, header: list[str], rows: list[list[str]]) -> None:
    cols = len(header)
    table = doc.add_table(rows=1 + len(rows), cols=cols)
    table.style = "Table Grid"
    for j, cell in enumerate(header):
        para = table.rows[0].cells[j].paragraphs[0]
        _add_runs(para, cell)
        for run in para.runs:
            run.bold = True
    for i, row in enumerate(rows, start=1):
        for j in range(cols):
            cell = row[j] if j < len(row) else ""
            _add_runs(table.rows[i].cells[j].paragraphs[0], cell)


def md_to_docx(text: str) -> bytes:
    """markdown 文本 → .docx 字节流。"""
    doc = Document()
    _setup_styles(doc)
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        # ``` 代码围栏
        if s.startswith("```"):
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # 跳过收尾围栏（未闭合则自然到文末）
            _add_code_block(doc, code_lines or [""])
            continue
        # GFM 表格：当前行含 | 且下一行是分隔行
        if "|" in s and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1].strip()):
            header = _split_row(s)
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_split_row(lines[i]))
                i += 1
            _add_table(doc, header, rows)
            continue
        if not s:
            i += 1
            continue
        m = _HEADING_RE.match(s)
        if m:
            level = min(len(m.group(1)), 4)  # Heading 5/6 并入 4
            _add_runs(doc.add_heading("", level=level), m.group(2).strip())
            i += 1
            continue
        if s in ("---", "***", "___"):
            i += 1
            continue
        if s.startswith(">"):
            para = doc.add_paragraph()
            para.paragraph_format.left_indent = Pt(18)
            _add_runs(para, s.lstrip("> ").strip())
            for run in para.runs:
                run.italic = True
                run.font.color.rgb = RGBColor(0x59, 0x59, 0x59)
            i += 1
            continue
        if s.startswith(("- ", "* ")):
            _add_runs(doc.add_paragraph(style="List Bullet"), s[2:])
            i += 1
            continue
        m = _ORDERED_RE.match(s)
        if m:
            _add_runs(doc.add_paragraph(style="List Number"), m.group(1))
            i += 1
            continue
        _add_runs(doc.add_paragraph(), s)
        i += 1
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
