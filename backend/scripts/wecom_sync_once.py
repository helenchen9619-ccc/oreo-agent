# -*- coding: utf-8 -*-
"""M31 事故定位第二轮：一次性容器里跑真实 sync_wecom_archive（真 SDK + 真 DB 全代码路径）。

第一轮探针（wecom_probe_preload.py）证明 SDK 本身在四种线程/符号组合下全部干净，
本脚本把真实同步代码原样跑一遍——若在 uvicorn 之外也崩，崩点在同步代码与运行环境的
交织；若干净通过，崩点只剩 uvicorn 进程上下文（lifespan/事件循环/并发任务）。

云上执行（一次性容器，不影响在跑的服务；必须 python -u，段错误会丢缓冲）：
    docker compose -f docker-compose.prod.yml run --rm app python -u /app/data/wecom_archive/wecom_sync_once.py

安全：只打印统计计数，绝不打印会话正文；Secret/私钥只从 env/文件读。
"""
import asyncio
import sys

sys.path.insert(0, "/app")  # 脚本在 data 卷里运行，手动补上应用包路径


async def main() -> int:
    from app.core.database import SessionLocal
    from app.wecom_archive.sync import sync_wecom_archive

    print("[..] 开始一轮真实 sync_wecom_archive（真 SDK + 真 DB）")
    async with SessionLocal() as db:
        stats = await sync_wecom_archive(db)
    print(f"[DONE] sync 返回统计（只有计数，无正文）: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
