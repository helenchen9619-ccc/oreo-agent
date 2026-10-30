"""M2.7 纯逻辑单测：stats 日期范围解析、系统提示 workspace 能力文案。"""
from datetime import timedelta

from app.runtime.prompt import PLATFORM_LAYER
from app.stats.router import _range


def test_range_start_end_same_day_covers_full_day():
    since, until = _range("2026-08-01", "2026-08-01", 30)
    assert until - since == timedelta(days=1)
    assert since.day == 1 and since.hour == 0


def test_range_span_is_inclusive_of_end():
    since, until = _range("2026-08-01", "2026-08-05", 30)
    assert (until - since).days == 5  # 8/1 ~ 8/5 闭区间 = 5 天


def test_range_swaps_reversed_dates():
    since, until = _range("2026-08-05", "2026-08-01", 30)
    assert since.day == 1 and until.day == 6


def test_range_invalid_falls_back_to_days():
    since, until = _range("not-a-date", "2026-08-01", 7)
    assert 6 < (until - since).days < 8


def test_range_none_falls_back_to_days():
    since, until = _range(None, None, 30)
    assert 29 < (until - since).days < 31


def test_platform_prompt_teaches_workspace_write_and_folders():
    assert "workspace_write" in PLATFORM_LAYER
    assert "子文件夹" in PLATFORM_LAYER
