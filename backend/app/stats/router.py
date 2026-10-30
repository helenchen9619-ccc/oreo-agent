"""用量监控（仅管理员）：日期范围趋势 + 按人/部门/模型/数字人聚合 token 与费用 + Excel 导出。

数据源是 usage_records（每次模型调用落一条，cost 由网关按价目表计算）。
"""
import io
from datetime import UTC, date, datetime, time, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import require_module
from app.core.database import get_db
from app.models.agent import Agent
from app.models.base import utcnow
from app.models.gateway import Model, UsageRecord
from app.models.user import Department, User

router = APIRouter(prefix="/api/admin/stats", tags=["admin-stats"])

MAX_DAYS = 90


def _range(start: str | None, end: str | None, days: int) -> tuple[datetime, datetime]:
    """起止日期（YYYY-MM-DD，闭区间）→ [start 00:00, end+1天 00:00)；未传则回退最近 N 天。"""
    if start and end:
        try:
            s = date.fromisoformat(start)
            e = date.fromisoformat(end)
        except ValueError:
            s, e = None, None
        if s and e:
            if s > e:
                s, e = e, s
            return (
                datetime.combine(s, time.min, tzinfo=UTC),
                datetime.combine(e + timedelta(days=1), time.min, tzinfo=UTC),
            )
    return utcnow() - timedelta(days=days), utcnow() + timedelta(minutes=1)


def _f(v) -> float:
    return float(v or 0)


def _agg_select():
    return (
        func.count(UsageRecord.id),
        func.coalesce(func.sum(UsageRecord.input_tokens), 0),
        func.coalesce(func.sum(UsageRecord.output_tokens), 0),
        func.coalesce(func.sum(UsageRecord.cost), 0),
    )


def _row(key_id, label, sub, calls, tin, tout, cost) -> dict:
    return {
        "id": key_id, "label": label, "sub": sub,
        "calls": int(calls), "input_tokens": int(tin), "output_tokens": int(tout), "cost": _f(cost),
    }


# ---------- 内部聚合（JSON 端点与 Excel 导出共用） ----------

async def _daily_rows(db: AsyncSession, since: datetime, until: datetime) -> list[dict]:
    rows = (
        await db.execute(
            select(
                func.date(UsageRecord.created_at).label("d"),
                func.coalesce(func.sum(UsageRecord.input_tokens + UsageRecord.output_tokens), 0),
                func.coalesce(func.sum(UsageRecord.cost), 0),
            )
            .where(UsageRecord.created_at >= since, UsageRecord.created_at < until)
            .group_by(func.date(UsageRecord.created_at))
            .order_by(func.date(UsageRecord.created_at))
        )
    ).all()
    return [{"date": str(d), "tokens": int(t), "cost": _f(c)} for d, t, c in rows]


async def _by_user_rows(db: AsyncSession, since: datetime, until: datetime) -> list[dict]:
    rows = (
        await db.execute(
            select(UsageRecord.user_id, User.name, Department.name, *_agg_select())
            .join(User, User.id == UsageRecord.user_id)
            .outerjoin(Department, Department.id == User.department_id)
            .where(UsageRecord.created_at >= since, UsageRecord.created_at < until)
            .group_by(UsageRecord.user_id, User.name, Department.name)
            .order_by(func.sum(UsageRecord.cost).desc())
        )
    ).all()
    return [_row(r[0], r[1], r[2] or "未分配部门", *r[3:]) for r in rows]


async def _by_department_rows(db: AsyncSession, since: datetime, until: datetime) -> list[dict]:
    rows = (
        await db.execute(
            select(User.department_id, Department.name, *_agg_select())
            .join(User, User.id == UsageRecord.user_id)
            .outerjoin(Department, Department.id == User.department_id)
            .where(UsageRecord.created_at >= since, UsageRecord.created_at < until)
            .group_by(User.department_id, Department.name)
            .order_by(func.sum(UsageRecord.cost).desc())
        )
    ).all()
    return [_row(r[0], r[1] or "未分配部门", None, *r[2:]) for r in rows]


async def _by_model_rows(db: AsyncSession, since: datetime, until: datetime) -> list[dict]:
    rows = (
        await db.execute(
            select(UsageRecord.model_id, Model.display_name, Model.model_id, *_agg_select())
            .join(Model, Model.id == UsageRecord.model_id)
            .where(UsageRecord.created_at >= since, UsageRecord.created_at < until)
            .group_by(UsageRecord.model_id, Model.display_name, Model.model_id)
            .order_by(func.sum(UsageRecord.cost).desc())
        )
    ).all()
    return [_row(r[0], r[1], r[2], *r[3:]) for r in rows]


async def _by_agent_rows(db: AsyncSession, since: datetime, until: datetime) -> list[dict]:
    """agent_id 为 NULL 的历史记录（M2.6 前未填）归到"未关联"。"""
    rows = (
        await db.execute(
            select(UsageRecord.agent_id, Agent.name, *_agg_select())
            .outerjoin(Agent, Agent.id == UsageRecord.agent_id)
            .where(UsageRecord.created_at >= since, UsageRecord.created_at < until)
            .group_by(UsageRecord.agent_id, Agent.name)
            .order_by(func.sum(UsageRecord.cost).desc())
        )
    ).all()
    return [_row(r[0], r[1] or "未关联数字人", None, *r[2:]) for r in rows]


# ---------- JSON 端点 ----------

@router.get("/overview")
async def overview(
    start: str | None = Query(None),
    end: str | None = Query(None),
    days: int = Query(30, ge=1, le=MAX_DAYS),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_module("stats")),
) -> dict:
    since, until = _range(start, end, days)
    totals = (
        await db.execute(
            select(
                func.count(UsageRecord.id),
                *_agg_select()[1:],
                func.count(func.distinct(UsageRecord.user_id)),
            ).where(UsageRecord.created_at >= since, UsageRecord.created_at < until)
        )
    ).one()
    return {
        "totals": {
            "calls": int(totals[0]),
            "input_tokens": int(totals[1]),
            "output_tokens": int(totals[2]),
            "cost": _f(totals[3]),
            "active_users": int(totals[4]),
        },
        "daily": await _daily_rows(db, since, until),
    }


@router.get("/by-user")
async def by_user(start: str | None = Query(None), end: str | None = Query(None), days: int = Query(30, ge=1, le=MAX_DAYS), db: AsyncSession = Depends(get_db), _: User = Depends(require_module("stats"))) -> list[dict]:
    since, until = _range(start, end, days)
    return await _by_user_rows(db, since, until)


@router.get("/by-department")
async def by_department(start: str | None = Query(None), end: str | None = Query(None), days: int = Query(30, ge=1, le=MAX_DAYS), db: AsyncSession = Depends(get_db), _: User = Depends(require_module("stats"))) -> list[dict]:
    since, until = _range(start, end, days)
    return await _by_department_rows(db, since, until)


@router.get("/by-model")
async def by_model(start: str | None = Query(None), end: str | None = Query(None), days: int = Query(30, ge=1, le=MAX_DAYS), db: AsyncSession = Depends(get_db), _: User = Depends(require_module("stats"))) -> list[dict]:
    since, until = _range(start, end, days)
    return await _by_model_rows(db, since, until)


@router.get("/by-agent")
async def by_agent(start: str | None = Query(None), end: str | None = Query(None), days: int = Query(30, ge=1, le=MAX_DAYS), db: AsyncSession = Depends(get_db), _: User = Depends(require_module("stats"))) -> list[dict]:
    since, until = _range(start, end, days)
    return await _by_agent_rows(db, since, until)


# ---------- Excel 导出 ----------

DIM_SHEETS = [
    ("按人", _by_user_rows),
    ("按部门", _by_department_rows),
    ("按模型", _by_model_rows),
    ("按数字人", _by_agent_rows),
]


@router.get("/export")
async def export_excel(
    start: str | None = Query(None),
    end: str | None = Query(None),
    days: int = Query(30, ge=1, le=MAX_DAYS),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_module("stats")),
):
    """导出用量统计 xlsx：按日汇总 + 四个维度各一个 sheet。"""
    from openpyxl import Workbook

    since, until = _range(start, end, days)
    wb = Workbook()

    ws = wb.active
    ws.title = "按日汇总"
    ws.append(["日期", "Token 总量", "费用（¥)"])
    for r in await _daily_rows(db, since, until):
        ws.append([r["date"], r["tokens"], round(r["cost"], 6)])

    for title, fn in DIM_SHEETS:
        ws = wb.create_sheet(title)
        ws.append(["名称", "说明", "调用次数", "输入 Tokens", "输出 Tokens", "费用（¥)"])
        for r in await fn(db, since, until):
            ws.append([r["label"], r["sub"] or "", r["calls"], r["input_tokens"], r["output_tokens"], round(r["cost"], 6)])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"usage_{since.date().isoformat()}_{(until - timedelta(days=1)).date().isoformat()}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
