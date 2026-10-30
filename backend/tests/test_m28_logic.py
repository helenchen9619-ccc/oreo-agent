"""M2.8 逻辑测试：xlsx/docx 文本抽取、技能绑定复活（500 修复）、导入工具函数。"""
import io
import zipfile

from app.workspace.router import _extract_docx, _extract_text, _extract_xlsx, _valid_rel


def _make_xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "销售"
    ws.append(["产品", "数量"])
    ws.append(["苹果", 3])
    ws.append(["香蕉", 5])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_docx(text: str) -> bytes:
    """最小可解析 docx：zip 里只有一个 word/document.xml。"""
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


# ---------- xlsx 抽取 ----------

def test_extract_xlsx_reads_sheets_and_cells():
    text = _extract_xlsx(_make_xlsx())
    assert text is not None
    assert "销售" in text          # sheet 名
    assert "产品" in text and "苹果" in text and "香蕉" in text


def test_extract_xlsx_bad_bytes_returns_none():
    assert _extract_xlsx(b"not a zip at all") is None


# ---------- docx 抽取 ----------

def test_extract_docx_reads_text():
    text = _extract_docx(_make_docx("季度总结报告"))
    assert text == "季度总结报告"


def test_extract_docx_bad_bytes_returns_none():
    assert _extract_docx(b"\x00\x01\x02") is None


# ---------- _extract_text 分派 ----------

def test_extract_text_dispatch_by_extension():
    assert _extract_text("a.xlsx", _make_xlsx()) is not None
    assert _extract_text("a.docx", _make_docx("hi")) == "hi"
    assert _extract_text("a.txt", "你好".encode()) == "你好"
    assert _extract_text("a.png", b"\x89PNG") is None  # 未支持的二进制不抽取


def test_extract_text_strips_nul():
    """errors=replace 不处理 NUL，但 PostgreSQL text 禁止 0x00；登记前必须清掉。"""
    assert _extract_text("a.txt", b"a\x00b") == "ab"


# ---------- 删文件夹路径校验（复用 _valid_rel） ----------

def test_delete_folder_path_validation():
    assert _valid_rel("reports")
    assert _valid_rel("reports/2026")
    assert _valid_rel("周报")  # \w 含中文
    assert _valid_rel("导出清单 (16)")  # M21：空格/括号文件夹名合法
    assert not _valid_rel("")
    assert not _valid_rel("../etc")
    assert not _valid_rel("a/../../b")
    assert not _valid_rel("a//b")
    assert not _valid_rel("a\\b")   # M21：反斜杠非法
    assert not _valid_rel("a?")     # M21：Windows 非法字符
    assert not _valid_rel("a.")     # M21：尾点非法（Windows 兼容）


# ---------- 导入工具函数（前端同名逻辑的镜像校验） ----------

def test_sanitize_skill_name_logic():
    import re

    def sanitize(name: str) -> str:
        s = re.sub(r"-{2,}", "-", re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9-]+", "-", name.lower())))
        return s or "imported-skill"

    assert sanitize("My Weekly Report") == "my-weekly-report"
    assert sanitize("周报技能！") == "imported-skill"  # 中文被剥光后回退默认名
    assert sanitize("--a--b--") == "a-b"
