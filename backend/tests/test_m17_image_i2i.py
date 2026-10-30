"""M17 逻辑测试：image_generate 参考图扩展（i2i）。

覆盖：schema 含 images、IMAGE_GEN_LAYER 教学、参考图读取与 data URL 构造、
路径穿越/不存在/非图片/超限/超数量拒绝、无 images 回归（请求体不带 image 键）。
外部 HTTP 全 fake（照 test_m13a 模式）。
"""
from types import SimpleNamespace

import pytest

import app.runtime.tools as tools
from app.runtime.prompt import IMAGE_GEN_LAYER



class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.added = []
        self.committed = 0

    async def execute(self, *a, **k):
        return _FakeResult(self.rows)

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        pass

    async def commit(self):
        self.committed += 1


class _FakeResp:
    def __init__(self, status_code=200, payload=None, headers=None, content=b"", text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.content = content
        self.text = text

    def json(self):
        return self._payload


class _FakeArkClient:
    posts = []

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        type(self).posts.append({"url": url, "json": json})
        return _FakeResp(payload={"data": [{"url": "https://cdn.example.com/x.png"}]})

    async def get(self, url, timeout=None):
        return _FakeResp(headers={"content-type": "image/png"}, content=b"\x89PNG-fake-bytes")


def _settings(tmp_path):
    return SimpleNamespace(VOLC_ARK_API_KEY="k", VOLC_ARK_IMAGE_MODEL="test-model", DATA_DIR=str(tmp_path))


def _ctx(db=None, **over):
    base = dict(db=db or _FakeDB(), user=SimpleNamespace(id=1), agent=SimpleNamespace(id=2))
    base.update(over)
    return tools.ToolContext(**base)


def _ws_file(tmp_path, rel="product.jpg", content=b"\xff\xd8\xff-fake-jpg"):
    """在工作区 root（user=1, agent=2）下落一张参考图。"""
    p = tmp_path / "workspaces" / "1" / "2" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return rel


# ---------- schema 与提示词教学 ----------

def test_schema_has_images():
    props = tools.IMG_TOOL_SCHEMAS[0]["function"]["parameters"]["properties"]
    assert "images" in props and props["images"]["type"] == "array"


def test_prompt_layer_teaches_i2i():
    assert "参考图" in IMAGE_GEN_LAYER and "images" in IMAGE_GEN_LAYER


def test_prompt_layer_forbids_pixel_preserving_redraw():
    """M24.7：「不动原图只加字/排版/贴标」类需求禁用 image_generate（扩散整图重绘保不住像素），
    引导走 run_code PIL 叠加；图只在对话里时请用户传工作区。"""
    assert "硬性边界" in IMAGE_GEN_LAYER and "一像素不动" in IMAGE_GEN_LAYER
    assert "run_code" in IMAGE_GEN_LAYER and "fc-list" in IMAGE_GEN_LAYER
    assert "上传到工作区" in IMAGE_GEN_LAYER


# ---------- 参考图成功路径 ----------


@pytest.mark.asyncio
async def test_i2i_success_sends_data_urls(monkeypatch, tmp_path):
    _FakeArkClient.posts = []
    monkeypatch.setattr(tools, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(tools.httpx, "AsyncClient", _FakeArkClient)
    _ws_file(tmp_path, "product.jpg")
    db = _FakeDB()
    ctx = _ctx(db=db, allow_image_generate=True)
    r = await tools._image_generate(ctx, {"prompt": "参考产品做海报", "images": ["product.jpg"]})
    assert "gen_imgs/" in r
    body = _FakeArkClient.posts[0]["json"]
    assert len(body["image"]) == 1 and body["image"][0].startswith("data:image/jpeg;base64,")
    audits = [x for x in db.added if getattr(x, "action", "") == "image.generate"]
    assert '"ref_images": 1' in audits[0].detail



@pytest.mark.asyncio
async def test_no_images_keeps_t2i(monkeypatch, tmp_path):
    """不带 images：请求体不出现 image 键（回归纯文生图行为）。"""
    _FakeArkClient.posts = []
    monkeypatch.setattr(tools, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(tools.httpx, "AsyncClient", _FakeArkClient)
    r = await tools._image_generate(_ctx(allow_image_generate=True), {"prompt": "一只猫"})
    assert "gen_imgs/" in r
    assert "image" not in _FakeArkClient.posts[0]["json"]



@pytest.mark.asyncio
async def test_png_mime_mapping(tmp_path, monkeypatch):
    """png/webp 的 data URL mime 映射正确。"""
    monkeypatch.setattr(tools, "get_settings", lambda: _settings(tmp_path))
    _ws_file(tmp_path, "logo.png", content=b"\x89PNG")
    _ws_file(tmp_path, "icon.webp", content=b"RIFF")
    urls, err = tools._load_ref_images(_ctx(), ["logo.png", "icon.webp"])
    assert err is None
    assert urls[0].startswith("data:image/png;base64,") and urls[1].startswith("data:image/webp;base64,")


# ---------- 参考图校验（不进 HTTP） ----------


@pytest.mark.asyncio
async def test_ref_path_traversal_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "get_settings", lambda: _settings(tmp_path))
    r = await tools._image_generate(_ctx(allow_image_generate=True), {"prompt": "x", "images": ["../evil.png"]})
    assert "不合法" in r



@pytest.mark.asyncio
async def test_ref_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "get_settings", lambda: _settings(tmp_path))
    r = await tools._image_generate(_ctx(allow_image_generate=True), {"prompt": "x", "images": ["nope.png"]})
    assert "找不到参考图" in r



@pytest.mark.asyncio
async def test_ref_bad_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "get_settings", lambda: _settings(tmp_path))
    _ws_file(tmp_path, "notes.txt", content=b"hello")
    r = await tools._image_generate(_ctx(allow_image_generate=True), {"prompt": "x", "images": ["notes.txt"]})
    assert "png/jpg/jpeg/webp" in r



@pytest.mark.asyncio
async def test_ref_too_large(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "get_settings", lambda: _settings(tmp_path))
    _ws_file(tmp_path, "big.png", content=b"x" * (10 * 1024 * 1024 + 1))
    r = await tools._image_generate(_ctx(allow_image_generate=True), {"prompt": "x", "images": ["big.png"]})
    assert "超过 10MB" in r



@pytest.mark.asyncio
async def test_ref_too_many(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "get_settings", lambda: _settings(tmp_path))
    r = await tools._image_generate(
        _ctx(allow_image_generate=True), {"prompt": "x", "images": [f"{i}.png" for i in range(11)]}
    )
    assert "最多 10 张" in r



@pytest.mark.asyncio
async def test_ref_not_a_list(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "get_settings", lambda: _settings(tmp_path))
    r = await tools._image_generate(_ctx(allow_image_generate=True), {"prompt": "x", "images": "a.png"})
    assert "列表" in r
