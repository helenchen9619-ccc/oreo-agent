"""模型网关管理端 API：全部 require_admin，操作写审计日志。

红线：api_key 只进不出——任何响应里都不包含 key 明文或密文。
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_admin
from app.core.security import encrypt_secret
from app.models.gateway import Model, ModelGrant, ModelProvider, Quota
from app.models.user import AuditLog, User

router = APIRouter(prefix="/api/admin/gateway", tags=["admin-gateway"])


async def _audit(db: AsyncSession, actor: User, action: str, target_type: str, target_id: object, detail: dict) -> None:
    db.add(AuditLog(actor_user_id=actor.id, action=action, target_type=target_type, target_id=str(target_id), detail=json.dumps(detail, ensure_ascii=False)))


# ---------- Provider ----------

class ProviderIn(BaseModel):
    name: str
    slug: str
    base_url: str
    api_key: str = Field(min_length=1)
    enabled: bool = True


class ProviderUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None  # 不传则不换 key
    enabled: bool | None = None


def _provider_out(p: ModelProvider) -> dict:
    return {"id": p.id, "name": p.name, "slug": p.slug, "base_url": p.base_url, "provider_type": p.provider_type, "enabled": p.enabled}


@router.get("/providers")
async def list_providers(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)) -> list[dict]:
    rows = (await db.execute(select(ModelProvider).where(ModelProvider.deleted_at.is_(None)))).scalars().all()
    return [_provider_out(p) for p in rows]


@router.post("/providers", status_code=201)
async def create_provider(body: ProviderIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> dict:
    p = ModelProvider(
        name=body.name,
        slug=body.slug,
        base_url=body.base_url,
        api_key_encrypted=encrypt_secret(body.api_key),
        enabled=body.enabled,
    )
    db.add(p)
    await db.flush()
    await _audit(db, admin, "provider.create", "model_provider", p.id, {"slug": p.slug})
    await db.commit()
    return _provider_out(p)


@router.patch("/providers/{pid}")
async def update_provider(pid: int, body: ProviderUpdate, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> dict:
    p = (await db.execute(select(ModelProvider).where(ModelProvider.id == pid, ModelProvider.deleted_at.is_(None)))).scalar_one_or_none()
    if p is None:
        raise HTTPException(404, "供应商不存在")
    changed = body.model_dump(exclude_none=True)
    if "api_key" in changed:
        p.api_key_encrypted = encrypt_secret(changed.pop("api_key"))
    for k, v in changed.items():
        setattr(p, k, v)
    await _audit(db, admin, "provider.update", "model_provider", p.id, {"fields": list(changed)})
    await db.commit()
    return _provider_out(p)


@router.delete("/providers/{pid}", status_code=204)
async def delete_provider(pid: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> None:
    p = (await db.execute(select(ModelProvider).where(ModelProvider.id == pid, ModelProvider.deleted_at.is_(None)))).scalar_one_or_none()
    if p is None:
        raise HTTPException(404, "供应商不存在")
    has_models = (await db.execute(select(Model.id).where(Model.provider_id == pid, Model.deleted_at.is_(None)).limit(1))).scalar_one_or_none()
    if has_models is not None:
        raise HTTPException(400, "该供应商下还有模型，请先删除其全部模型")
    from app.models.base import utcnow
    p.deleted_at = utcnow()
    await _audit(db, admin, "provider.delete", "model_provider", pid, {"slug": p.slug})
    await db.commit()


# ---------- Model ----------

@router.get("/providers/{pid}/remote-models")
async def list_remote_models(pid: int, db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)) -> list[dict]:
    """用已存的 key 调供应商 GET /models，列出该 key 实际可用的模型，避免手填 model_id 出错。"""
    p = (await db.execute(select(ModelProvider).where(ModelProvider.id == pid, ModelProvider.deleted_at.is_(None)))).scalar_one_or_none()
    if p is None:
        raise HTTPException(404, "供应商不存在")
    from openai import AsyncOpenAI

    from app.core.security import decrypt_secret
    client = AsyncOpenAI(base_url=p.base_url, api_key=decrypt_secret(p.api_key_encrypted), timeout=30)
    try:
        remote = await client.models.list()
    except Exception as e:
        raise HTTPException(400, f"拉取失败（请检查 base_url 和 key）: {e!s}") from e
    return [{"model_id": m.id} for m in remote.data]


class ModelIn(BaseModel):
    provider_id: int
    model_id: str
    display_name: str
    model_type: str = "chat"
    price_input_per_1k: float = 0
    price_output_per_1k: float = 0
    context_window: int | None = None
    supports_vision: bool = False
    enabled: bool = True


def _model_out(m: Model) -> dict:
    return {
        "id": m.id, "provider_id": m.provider_id, "model_id": m.model_id,
        "display_name": m.display_name, "model_type": m.model_type,
        "price_input_per_1k": float(m.price_input_per_1k or 0),
        "price_output_per_1k": float(m.price_output_per_1k or 0),
        "context_window": m.context_window, "supports_vision": m.supports_vision, "enabled": m.enabled,
    }


@router.get("/models")
async def list_models(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)) -> list[dict]:
    rows = (await db.execute(select(Model).where(Model.deleted_at.is_(None)))).scalars().all()
    return [_model_out(m) for m in rows]


@router.post("/models", status_code=201)
async def create_model(body: ModelIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> dict:
    m = Model(**body.model_dump())
    db.add(m)
    await db.flush()
    await _audit(db, admin, "model.create", "model", m.id, {"model_id": m.model_id})
    await db.commit()
    return _model_out(m)


@router.patch("/models/{mid}")
async def update_model(mid: int, body: ModelIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> dict:
    m = (await db.execute(select(Model).where(Model.id == mid, Model.deleted_at.is_(None)))).scalar_one_or_none()
    if m is None:
        raise HTTPException(404, "模型不存在")
    for k, v in body.model_dump().items():
        setattr(m, k, v)
    await _audit(db, admin, "model.update", "model", m.id, {})
    await db.commit()
    return _model_out(m)


@router.delete("/models/{mid}", status_code=204)
async def delete_model(mid: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> None:
    m = (await db.execute(select(Model).where(Model.id == mid, Model.deleted_at.is_(None)))).scalar_one_or_none()
    if m is None:
        raise HTTPException(404, "模型不存在")
    from app.models.base import utcnow
    now = utcnow()
    m.deleted_at = now
    # 连带失效该模型的全部授权，避免残留"幽灵授权"
    grants = (await db.execute(select(ModelGrant).where(ModelGrant.model_id == mid, ModelGrant.deleted_at.is_(None)))).scalars().all()
    for g in grants:
        g.deleted_at = now
    await _audit(db, admin, "model.delete", "model", mid, {"model_id": m.model_id, "grants_removed": len(grants)})
    await db.commit()


# ---------- Grant ----------

class GrantIn(BaseModel):
    model_id: int
    grantee_type: str  # user / department / all
    grantee_id: int | None = None


@router.get("/grants")
async def list_grants(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)) -> list[dict]:
    rows = (await db.execute(select(ModelGrant).where(ModelGrant.deleted_at.is_(None)))).scalars().all()
    return [{"id": g.id, "model_id": g.model_id, "grantee_type": g.grantee_type, "grantee_id": g.grantee_id} for g in rows]


@router.post("/grants", status_code=201)
async def create_grant(body: GrantIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> dict:
    if body.grantee_type not in ("user", "department", "all"):
        raise HTTPException(400, "grantee_type 必须是 user/department/all")
    g = ModelGrant(**body.model_dump())
    db.add(g)
    await db.flush()
    await _audit(db, admin, "grant.create", "model_grant", g.id, body.model_dump())
    await db.commit()
    return {"id": g.id, "model_id": g.model_id, "grantee_type": g.grantee_type, "grantee_id": g.grantee_id}


@router.delete("/grants/{gid}", status_code=204)
async def delete_grant(gid: int, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> None:
    g = (await db.execute(select(ModelGrant).where(ModelGrant.id == gid, ModelGrant.deleted_at.is_(None)))).scalar_one_or_none()
    if g is None:
        raise HTTPException(404, "授权不存在")
    from app.models.base import utcnow
    g.deleted_at = utcnow()
    await _audit(db, admin, "grant.delete", "model_grant", gid, {})
    await db.commit()


# ---------- Quota ----------

class QuotaIn(BaseModel):
    scope_type: str  # user / department
    scope_id: int
    period: str = "day"
    limit_tokens: int | None = None
    limit_cost: float | None = None
    enabled: bool = True


@router.get("/quotas")
async def list_quotas(db: AsyncSession = Depends(get_db), _: User = Depends(require_admin)) -> list[dict]:
    rows = (await db.execute(select(Quota).where(Quota.deleted_at.is_(None)))).scalars().all()
    return [{"id": q.id, "scope_type": q.scope_type, "scope_id": q.scope_id, "period": q.period,
             "limit_tokens": q.limit_tokens, "limit_cost": float(q.limit_cost) if q.limit_cost is not None else None,
             "enabled": q.enabled} for q in rows]


@router.post("/quotas", status_code=201)
async def create_quota(body: QuotaIn, db: AsyncSession = Depends(get_db), admin: User = Depends(require_admin)) -> dict:
    q = Quota(**body.model_dump())
    db.add(q)
    await db.flush()
    await _audit(db, admin, "quota.create", "quota", q.id, body.model_dump())
    await db.commit()
    return {"id": q.id}
