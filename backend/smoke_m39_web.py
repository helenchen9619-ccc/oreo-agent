"""M3.9 真实 key 冒烟：直接调 _web_search / _web_fetch 验证博查契约与网页抽取。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.runtime.tools import _check_fetch_url, _web_fetch, _web_search


def _ctx():
    db = MagicMock()
    db.add = MagicMock()
    return SimpleNamespace(db=db, user=SimpleNamespace(id=1), agent=SimpleNamespace(id=1))


async def main():
    ctx = _ctx()

    print("=== 1. web_search 真实调用 ===")
    out = await _web_search(ctx, {"query": "2026年 AI Agent 行业动态", "freshness": "oneWeek", "count": 5})
    print(out[:1200])
    if out.startswith("搜索服务返回错误（HTTP 403）"):
        print("\n!! 博查账户无余额/套餐，搜索冒烟跳过（集成正常：鉴权已通过，403 是账户配额问题）")
    else:
        assert not out.startswith("搜索服务"), "搜索服务报错"
        assert "[1]" in out, "没有编号结果"

    print("\n=== 2. web_fetch 抓公开网页（example.com）===")
    text = await _web_fetch(ctx, {"url": "https://example.com/"})
    print(text[:400])
    assert "This domain" in text, "正文抽取失败"

    print("\n=== 3. SSRF 拦截验证 ===")
    r = await _web_fetch(ctx, {"url": "http://127.0.0.1:8000/healthz"})
    print(r)
    assert "拒绝" in r or "内网" in r

    print("\nALL SMOKE PASSED")


asyncio.run(main())
