"""cron/时区纯函数（M3.8 从 engine 拆出，M3.10 起供 runtime.tools 复用——防 tools→engine→chat.router→runtime.engine→tools 循环导入）。"""
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from croniter import croniter


def is_valid_cron(expr: str) -> bool:
    """5 段标准 cron（分 时 日 月 周）且 croniter 能解析。"""
    if not expr or len(expr.split()) != 5:
        return False
    return croniter.is_valid(expr)


def is_valid_tz(tz: str) -> bool:
    try:
        ZoneInfo(tz)
        return True
    except Exception:
        return False


def compute_next(expr: str, tz: str, after: datetime) -> datetime:
    """下一次触发时刻（UTC tz-aware）。cron 表达式按任务时区解释。

    after 锚定"现在"而不是过期的 next_run_at——停机多久都只补出 1 个未来触发点。
    """
    base = after.astimezone(ZoneInfo(tz))
    return croniter(expr, base).get_next(datetime).astimezone(UTC)
