"""M2.6 纯逻辑单测：图片解码校验、workspace 路径防护、vision 消息组装（不依赖数据库）。"""
import base64
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.chat.router import MAX_IMAGES, ImageIn, _decode_images
from app.runtime.engine import _estimate_content, _row_to_llm_message
from app.runtime.tools import _workspace_write
from app.workspace.router import _valid_rel

# 1x1 PNG（标准 base64）
PNG_1PX = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def _img(data_url, name="a.png"):
    return ImageIn(name=name, data_url=data_url)


# ---------- 图片 data URL 解码 ----------

def test_decode_images_ok():
    out = _decode_images([_img(f"data:image/png;base64,{PNG_1PX}")])
    assert len(out) == 1
    name, mime, raw = out[0]
    assert name == "a.png" and mime == "image/png" and raw[:4] == b"\x89PNG"


def test_decode_images_rejects_non_image():
    with pytest.raises(HTTPException) as e:
        _decode_images([_img(f"data:text/html;base64,{PNG_1PX}")])
    assert e.value.status_code == 400


def test_decode_images_rejects_bad_base64():
    with pytest.raises(HTTPException) as e:
        _decode_images([_img("data:image/png;base64,!!!not-base64!!!")])
    assert e.value.status_code == 400


def test_decode_images_rejects_too_many():
    with pytest.raises(HTTPException) as e:
        _decode_images([_img(f"data:image/png;base64,{PNG_1PX}") for _ in range(MAX_IMAGES + 1)])
    assert e.value.status_code == 400


def test_decode_images_rejects_oversize():
    big = base64.b64encode(b"x" * (5 * 1024 * 1024 + 1)).decode()
    with pytest.raises(HTTPException) as e:
        _decode_images([_img(f"data:image/png;base64,{big}")])
    assert e.value.status_code == 400


# ---------- workspace 路径防护 ----------

def test_valid_rel_accepts_nested_chinese():
    assert _valid_rel("reports/2026/周报.md") is True
    assert _valid_rel("a.txt") is True
    # M21：真实世界文件名（空格/半角括号/全角括号/顿号）合法——ERP 导出件场景
    assert _valid_rel("物品导出清单-总部 (16).xlsx") is True
    assert _valid_rel("报表/【月结】2026、Q1.xlsx") is True


def test_valid_rel_rejects_traversal_and_bad_chars():
    assert _valid_rel("../evil.txt") is False
    assert _valid_rel("a/../../evil") is False
    assert _valid_rel("") is False
    assert _valid_rel("a//b") is False
    # M21：黑名单仍拦 Windows 非法字符/尾空格尾点/保留名
    assert _valid_rel("a\\b.txt") is False
    assert _valid_rel("a:b.txt") is False
    assert _valid_rel("a.") is False
    assert _valid_rel("a /b.txt") is False
    assert _valid_rel("CON.xlsx") is False


@pytest.mark.asyncio
async def test_workspace_write_blocks_traversal_without_db():
    """非法路径在进入 DB 操作前就被拒绝（ctx 给 None 也不应触发属性访问）。"""
    ctx = SimpleNamespace(user=None, agent=None, db=None)
    for bad in ("../evil.txt", "a/../../evil.txt", "C:/Windows/x.txt", "a//b", ""):
        result = await _workspace_write(ctx, {"path": bad, "content": "x"})
        assert "路径不合法" in result or "越界" in result, f"{bad} 未被拦截: {result}"


@pytest.mark.asyncio
async def test_workspace_write_blocks_oversize_content():
    ctx = SimpleNamespace(user=None, agent=None, db=None)
    result = await _workspace_write(ctx, {"path": "a.txt", "content": "x" * (200 * 1024 + 1)})
    assert "200KB" in result


# ---------- vision 消息组装 ----------

def _msg(role="user", content="你好", attachments=None, tool_calls=None):
    return SimpleNamespace(
        role=role, content=content, attachments=attachments,
        tool_calls=tool_calls, tool_call_id=None, name=None,
    )


def test_row_to_llm_message_plain_user():
    out = _row_to_llm_message(_msg())
    assert out == {"role": "user", "content": "你好"}


def test_row_to_llm_message_vision_assembles_parts(tmp_path):
    fp = tmp_path / "0.png"
    fp.write_bytes(base64.b64decode(PNG_1PX))
    atts = f'[{{"name":"a.png","path":"{str(fp).replace(chr(92), chr(92)*2)}","mime":"image/png"}}]'
    out = _row_to_llm_message(_msg(attachments=atts), vision_ok=True)
    assert out["role"] == "user"
    parts = out["content"]
    # M24.6：text 头部带内联锚定提示（防止模型去工作区翻找图片），用户原文保留在提示后
    assert parts[0]["type"] == "text"
    assert "1 张图片" in parts[0]["text"] and "不在工作区" in parts[0]["text"]
    assert parts[0]["text"].endswith("你好")
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_row_to_llm_message_missing_file_explicit_notice(tmp_path):
    """M24.6：附件文件读取失败时明示（不再静默只剩用户原文，防模型瞎猜）。"""
    atts = f'[{{"name":"gone.png","path":"{str(tmp_path / "gone.png").replace(chr(92), chr(92)*2)}","mime":"image/png"}}]'
    out = _row_to_llm_message(_msg(attachments=atts), vision_ok=True)
    # 不产生坏 image_url，只有 text part
    assert all(p["type"] == "text" for p in out["content"])
    assert "读取失败" in out["content"][0]["text"] and "重新发送" in out["content"][0]["text"]


def test_row_to_llm_message_degrades_without_vision(tmp_path):
    fp = tmp_path / "0.png"
    fp.write_bytes(base64.b64decode(PNG_1PX))
    atts = f'[{{"name":"截图.png","path":"{str(fp).replace(chr(92), chr(92)*2)}","mime":"image/png"}}]'
    out = _row_to_llm_message(_msg(attachments=atts), vision_ok=False)
    assert isinstance(out["content"], str)
    assert "截图.png" in out["content"]
    assert "不支持图片" in out["content"]


def test_estimate_content_counts_image_tokens():
    text_only = _estimate_content("你好")
    with_img = _estimate_content([{"type": "text", "text": "你好"}, {"type": "image_url", "image_url": {"url": "data:..."}}])
    assert with_img == text_only + 1000
