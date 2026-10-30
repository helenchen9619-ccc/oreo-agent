"""M13b 逻辑测试：语音输入 /api/chat/asr（配置降级/入参校验/火山轮询三分支）。外部 HTTP 全 fake。"""
import io
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException, UploadFile

import app.chat.asr as asr_mod


class _FakeDB:
    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        pass


def _user():
    return SimpleNamespace(id=7, role="employee")


def _wav(data=b"RIFF-fake-wav-bytes", name="voice.wav"):
    return UploadFile(file=io.BytesIO(data), filename=name)


def _settings(app_key="ak", access_key="sk"):
    return SimpleNamespace(VOLC_ASR_APP_KEY=app_key, VOLC_ASR_ACCESS_KEY=access_key)


class _FakeResp:
    def __init__(self, code, payload=None):
        self.headers = httpx.Headers({"X-Api-Status-Code": code})
        self.status_code = 200
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeAUC:
    """submit 返回 submit_code（真实服务受理成功=20000000），query 按队列依次返回状态码。"""

    submit_code = "20000000"
    query_codes = []

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        if url.endswith("/submit"):
            return _FakeResp(type(self).submit_code)
        code = self.query_codes.pop(0) if self.query_codes else "20000001"
        return _FakeResp(code, {"result": {"text": "你好世界"}} if code == "20000000" else {})


def _patch(monkeypatch, query_codes, submit_code="20000000"):
    monkeypatch.setattr(asr_mod, "get_settings", lambda: _settings())
    _FakeAUC.submit_code = submit_code
    _FakeAUC.query_codes = list(query_codes)
    monkeypatch.setattr(asr_mod.httpx, "AsyncClient", _FakeAUC)


# ---------- 配置降级与入参校验 ----------

@pytest.mark.asyncio
async def test_no_key_503(monkeypatch):
    monkeypatch.setattr(asr_mod, "get_settings", lambda: _settings(app_key="", access_key=""))
    with pytest.raises(HTTPException) as ei:
        await asr_mod.asr(file=_wav(), db=_FakeDB(), user=_user())
    assert ei.value.status_code == 503 and "VOLC_ASR" in ei.value.detail


@pytest.mark.asyncio
async def test_reject_non_wav(monkeypatch):
    monkeypatch.setattr(asr_mod, "get_settings", lambda: _settings())
    with pytest.raises(HTTPException) as ei:
        await asr_mod.asr(file=_wav(name="voice.webm"), db=_FakeDB(), user=_user())
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_reject_empty_and_oversize(monkeypatch):
    monkeypatch.setattr(asr_mod, "get_settings", lambda: _settings())
    with pytest.raises(HTTPException) as ei:
        await asr_mod.asr(file=_wav(data=b""), db=_FakeDB(), user=_user())
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei2:
        await asr_mod.asr(file=_wav(data=b"x" * (asr_mod._MAX_WAV_BYTES + 1)), db=_FakeDB(), user=_user())
    assert ei2.value.status_code == 400


def test_auc_status_header():
    assert asr_mod._auc_status(httpx.Headers({"X-Api-Status-Code": "20000000"})) == "20000000"
    assert asr_mod._auc_status(httpx.Headers()) == ""


# ---------- 轮询三分支 ----------

@pytest.mark.asyncio
async def test_poll_success(monkeypatch):
    _patch(monkeypatch, ["20000001", "20000000"])
    monkeypatch.setattr(asr_mod, "_POLL_INTERVAL_S", 0)
    r = await asr_mod.asr(file=_wav(), db=_FakeDB(), user=_user())
    assert r == {"text": "你好世界"}


@pytest.mark.asyncio
async def test_poll_silent_returns_empty(monkeypatch):
    _patch(monkeypatch, ["20000002"])
    monkeypatch.setattr(asr_mod, "_POLL_INTERVAL_S", 0)
    r = await asr_mod.asr(file=_wav(), db=_FakeDB(), user=_user())
    assert r == {"text": ""}


@pytest.mark.asyncio
async def test_poll_timeout_502(monkeypatch):
    _patch(monkeypatch, [])  # query 永远「处理中」
    monkeypatch.setattr(asr_mod, "_POLL_INTERVAL_S", 0)
    monkeypatch.setattr(asr_mod, "_POLL_TIMEOUT_S", 0.01)
    with pytest.raises(HTTPException) as ei:
        await asr_mod.asr(file=_wav(), db=_FakeDB(), user=_user())
    assert ei.value.status_code == 502 and "超时" in ei.value.detail


@pytest.mark.asyncio
async def test_poll_failure_code_502(monkeypatch):
    _patch(monkeypatch, ["45000001"])
    monkeypatch.setattr(asr_mod, "_POLL_INTERVAL_S", 0)
    with pytest.raises(HTTPException) as ei:
        await asr_mod.asr(file=_wav(), db=_FakeDB(), user=_user())
    assert ei.value.status_code == 502


@pytest.mark.asyncio
async def test_submit_20000001_also_accepted(monkeypatch):
    """submit 返回排队中（20000001）同样进入轮询。"""
    _patch(monkeypatch, ["20000000"], submit_code="20000001")
    monkeypatch.setattr(asr_mod, "_POLL_INTERVAL_S", 0)
    r = await asr_mod.asr(file=_wav(), db=_FakeDB(), user=_user())
    assert r == {"text": "你好世界"}


@pytest.mark.asyncio
async def test_submit_unknown_code_502(monkeypatch):
    _patch(monkeypatch, [], submit_code="45000001")
    with pytest.raises(HTTPException) as ei:
        await asr_mod.asr(file=_wav(), db=_FakeDB(), user=_user())
    assert ei.value.status_code == 502


@pytest.mark.asyncio
async def test_audit_written_without_text(monkeypatch):
    _patch(monkeypatch, ["20000000"])
    monkeypatch.setattr(asr_mod, "_POLL_INTERVAL_S", 0)
    db = _FakeDB()
    await asr_mod.asr(file=_wav(), db=db, user=_user())
    audits = [x for x in db.added if getattr(x, "action", "") == "chat.asr"]
    assert len(audits) == 1 and "你好世界" not in audits[0].detail  # 审计不落识别文本
