"""M3.1.3 逻辑测试：工作区 mime 兜底（图片/PDF 预览的前提）+ pptx 文本抽取。"""
import io
import zipfile

from app.workspace.router import _extract_pptx, guess_mime


def test_guess_mime_prefers_real_content_type():
    assert guess_mime("a.png", "image/png") == "image/png"
    assert guess_mime("a.txt", "text/plain") == "text/plain"


def test_guess_mime_fills_octet_stream_by_ext():
    # 浏览器/沙箱只给 octet-stream 时按扩展名猜（图表 PNG 无法预览的病灶）
    assert guess_mime("chart.png", "application/octet-stream") == "image/png"
    assert guess_mime("slides.pdf", "application/octet-stream") == "application/pdf"
    assert guess_mime("page.html", "application/octet-stream") == "text/html"


def test_guess_mime_unknown_ext_keeps_fallback():
    # 必须用任何平台 mimetypes 库都不认识的扩展名（.parquet 在 py3.12+ Linux 已入库，会使断言炸）
    assert guess_mime("data.oreo_unknown_ext", "application/octet-stream") == "application/octet-stream"
    assert guess_mime("data.oreo_unknown_ext", None) == "application/octet-stream"


def _make_pptx(slides: list[str]) -> bytes:
    """构造最小 pptx：ppt/slides/slideN.xml 含若干 a:t 文本 run。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i, text in enumerate(slides, 1):
            xml = f'<p:sld xmlns:a="a"><p:cSld><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:cSld></p:sld>'
            z.writestr(f"ppt/slides/slide{i}.xml", xml)
    return buf.getvalue()


def test_extract_pptx_reads_slides_in_order():
    raw = _make_pptx(["年度总结", "营收增长 20%"])
    text = _extract_pptx(raw)
    assert text is not None
    assert "# 第 1 页" in text and "年度总结" in text
    assert "# 第 2 页" in text and "营收增长 20%" in text
    assert text.index("年度总结") < text.index("营收增长 20%")


def test_extract_pptx_bad_bytes_returns_none():
    assert _extract_pptx(b"not a zip") is None
