"""模型网关核心逻辑：授权校验、配额检查、成本计算、逐次调用记录。

key 只在 call_model 内部短暂解密使用，函数外不可见。
"""
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from openai import AsyncOpenAI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_secret
from app.models.gateway import Model, ModelGrant, ModelProvider, Quota, UsageRecord
from app.models.user import User


class GatewayError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


@dataclass
class ChatResult:
    content: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    latency_ms: int


# ---------- 纯逻辑（可单测） ----------

def evaluate_grants(grants: list[ModelGrant], user: User) -> bool:
    """授权判定：admin 放行；user 直授 > 部门 > all。"""
    if user.role == "admin":
        return True
    for g in grants:
        if g.grantee_type == "user" and g.grantee_id == user.id:
            return True
    for g in grants:
        if g.grantee_type == "department" and g.grantee_id is not None and g.grantee_id == user.department_id:
            return True
    return any(g.grantee_type == "all" for g in grants)


def compute_cost(model: Model, input_tokens: int, output_tokens: int) -> Decimal:
    pin = Decimal(str(model.price_input_per_1k or 0))
    pout = Decimal(str(model.price_output_per_1k or 0))
    return (pin * input_tokens + pout * output_tokens) / Decimal(1000)


def quota_exceeded(quota: Quota, used_tokens: int, used_cost: Decimal) -> bool:
    if not quota.enabled:
        return False
    if quota.limit_tokens is not None and used_tokens >= quota.limit_tokens:
        return True
    return bool(quota.limit_cost is not None and used_cost >= Decimal(str(quota.limit_cost)))


# ---------- DB 相关 ----------

async def check_model_access(db: AsyncSession, user: User, model: Model) -> None:
    grants = (
        (await db.execute(select(ModelGrant).where(ModelGrant.model_id == model.id, ModelGrant.deleted_at.is_(None))))
        .scalars()
        .all()
    )
    if not evaluate_grants(list(grants), user):
        raise GatewayError(403, "没有该模型的使用权限，请联系 IT 信息部开通")


async def check_quota(db: AsyncSession, user: User) -> None:
    if user.role == "admin":
        return
    now = datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    quotas = (
        (
            await db.execute(
                select(Quota).where(
                    Quota.deleted_at.is_(None),
                    Quota.enabled.is_(True),
                    (Quota.scope_type == "user") & (Quota.scope_id == user.id)
                    | (Quota.scope_type == "department") & (Quota.scope_id == (user.department_id or -1)),
                )
            )
        )
        .scalars()
        .all()
    )
    for q in quotas:
        start = day_start if q.period == "day" else month_start
        row = (
            await db.execute(
                select(
                    func.coalesce(func.sum(UsageRecord.input_tokens + UsageRecord.output_tokens), 0),
                    func.coalesce(func.sum(UsageRecord.cost), 0),
                ).where(
                    UsageRecord.user_id == user.id,
                    UsageRecord.created_at >= start,
                )
            )
        ).one()
        if quota_exceeded(q, int(row[0]), Decimal(str(row[1]))):
            raise GatewayError(429, f"已超出{('每日' if q.period == 'day' else '每月')}用量配额，请联系 IT 信息部")


async def record_usage(
    db: AsyncSession,
    *,
    user: User,
    model: Model,
    result: ChatResult,
    conversation_id: int | None = None,
    agent_id: int | None = None,
) -> UsageRecord:
    rec = UsageRecord(
        user_id=user.id,
        agent_id=agent_id,
        conversation_id=conversation_id,
        provider_id=model.provider_id,
        model_id=model.id,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cached_tokens=result.cached_tokens,
        cost=compute_cost(model, result.input_tokens, result.output_tokens),
        latency_ms=result.latency_ms,
    )
    db.add(rec)
    return rec


# ---------- 调用 ----------

def _build_client(provider: ModelProvider) -> AsyncOpenAI:
    return AsyncOpenAI(base_url=provider.base_url, api_key=decrypt_secret(provider.api_key_encrypted), timeout=120)


async def chat_completion(
    db: AsyncSession,
    *,
    user: User,
    model: Model,
    provider: ModelProvider,
    messages: list[dict],
    conversation_id: int | None = None,
) -> ChatResult:
    """非流式调用（内部用；流式走 chat_completion_stream）。"""
    await check_model_access(db, user, model)
    await check_quota(db, user)
    client = _build_client(provider)
    started = time.monotonic()
    resp = await client.chat.completions.create(model=model.model_id, messages=messages)
    latency_ms = int((time.monotonic() - started) * 1000)
    usage = resp.usage
    result = ChatResult(
        content=resp.choices[0].message.content or "",
        input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
        output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
        cached_tokens=getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0 if usage else 0,
        latency_ms=latency_ms,
    )
    await record_usage(db, user=user, model=model, result=result, conversation_id=conversation_id)
    return result


async def chat_completion_stream(
    db: AsyncSession,
    *,
    user: User,
    model: Model,
    provider: ModelProvider,
    messages: list[dict],
    conversation_id: int | None = None,
) -> AsyncGenerator[str, None]:
    """流式调用：yield 文本增量；结束后自动落 usage_records。"""
    await check_model_access(db, user, model)
    await check_quota(db, user)
    client = _build_client(provider)
    started = time.monotonic()
    stream = await client.chat.completions.create(
        model=model.model_id,
        messages=messages,
        stream=True,
        stream_options={"include_usage": True},
    )
    input_tokens = output_tokens = cached_tokens = 0
    async for chunk in stream:
        if chunk.usage is not None:
            input_tokens = chunk.usage.prompt_tokens or 0
            output_tokens = chunk.usage.completion_tokens or 0
            details = getattr(chunk.usage, "prompt_tokens_details", None)
            cached_tokens = getattr(details, "cached_tokens", 0) or 0
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content
    latency_ms = int((time.monotonic() - started) * 1000)
    result = ChatResult(
        content="",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        latency_ms=latency_ms,
    )
    await record_usage(db, user=user, model=model, result=result, conversation_id=conversation_id)
    await db.commit()
