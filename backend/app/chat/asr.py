"""语音输入（M13b）：浏览器录音 WAV → 火山豆包大模型录音文件识别 → 返回文字。

前端录入的是 16kHz 单声道 WAV（≤60 秒）。火山 auc bigmodel 是异步接口：submit 提交任务
（base64 音频），query 轮询结果；状态码在**响应头** X-Api-Status-Code（20000000 成功 /
20000001 处理中 / 20000002 静音）。密钥只进不出，不落审计正文。
"""
import asyncio
import base64
import json
import time
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.user import AuditLog, User

router = APIRouter(prefix="/api/chat", tags=["chat"])

_AUC_SUBMIT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
_AUC_QUERY = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
_AUC_RESOURCE_ID = "volc.bigasr.auc"
_MAX_WAV_BYTES = 4 * 1024 * 1024  # 60s × 16kHz × 16bit 单声道 ≈ 1.9MB，留余量
_POLL_INTERVAL_S = 1.0
_POLL_TIMEOUT_S = 30.0

# 状态码（响应头 X-Api-Status-Code）
_ST_OK = "20000000"
_ST_PROCESSING = "20000001"
_ST_SILENT = "20000002"


def _auc_status(headers: httpx.Headers) -> str:
    """从响应头取火山状态码（火山的设计：状态码不在 body 而在 header）。"""
    return headers.get("X-Api-Status-Code", "")


@router.post("/asr")
async def asr(
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    s = get_settings()
    if not (s.VOLC_ASR_APP_KEY and s.VOLC_ASR_ACCESS_KEY):
        raise HTTPException(503, "语音输入未开通（服务端未配置 VOLC_ASR_APP_KEY / VOLC_ASR_ACCESS_KEY），请联系管理员")
    if not (file.filename or "").lower().endswith(".wav"):
        raise HTTPException(400, "仅支持 WAV 音频")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "音频为空")
    if len(raw) > _MAX_WAV_BYTES:
        raise HTTPException(400, "音频超过大小限制（最长约 60 秒）")

    started = time.monotonic()
    error = None
    text = ""
    headers = {
        "X-Api-App-Key": s.VOLC_ASR_APP_KEY,
        "X-Api-Access-Key": s.VOLC_ASR_ACCESS_KEY,
        "X-Api-Resource-Id": _AUC_RESOURCE_ID,
        "X-Api-Request-Id": uuid4().hex,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                _AUC_SUBMIT,
                headers={**headers, "X-Api-Sequence": "-1"},
                json={
                    "user": {"uid": str(user.id)},
                    "audio": {"data": base64.b64encode(raw).decode()},
                    "request": {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True},
                },
            )
            code = _auc_status(r.headers)
            if code == _ST_SILENT:
                pass  # 静音：text 保持 ""
            elif code in (_ST_OK, _ST_PROCESSING):
                # submit 受理成功（20000000=任务已提交；20000001=排队中），开始轮询 query
                deadline = time.monotonic() + _POLL_TIMEOUT_S
                while time.monotonic() < deadline:
                    await asyncio.sleep(_POLL_INTERVAL_S)
                    q = await client.post(_AUC_QUERY, headers=headers, json={})
                    code = _auc_status(q.headers)
                    if code == _ST_OK:
                        text = (((q.json().get("result") or {}).get("text")) or "").strip()
                        break
                    if code == _ST_SILENT:
                        break
                    if code != _ST_PROCESSING:
                        error = "语音识别失败，请重试"
                        break
                if error is None and code == _ST_PROCESSING:
                    error = "语音识别超时，请重试"
            else:
                error = f"语音识别服务返回错误（{code or f'HTTP {r.status_code}'}）"
    except httpx.TimeoutException:
        error = "语音识别服务超时，请重试"
    except httpx.HTTPError:
        error = "语音识别服务连接失败，请重试"

    db.add(AuditLog(
        actor_user_id=user.id,
        action="chat.asr",
        target_type="user",
        target_id=str(user.id),
        detail=json.dumps({"bytes": len(raw), "duration_ms": int((time.monotonic() - started) * 1000),
                           "ok": error is None}, ensure_ascii=False),
    ))
    await db.commit()
    if error:
        raise HTTPException(502, error)
    return {"text": text}
