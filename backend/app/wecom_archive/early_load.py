"""M31 事故修复（2026-09-23）：企微 SDK 必须抢在 SQLAlchemy 之前 dlopen。

六轮云上探针实锤：SQLAlchemy 先进进程后，dlopen libWeWorkFinanceSdk_C.so 必崩
（free(): invalid pointer / EXIT=139，SIGABRT 进程级崩溃 Python 捕不到）；
greenlet、SQLAlchemy C 扩展、RTLD_DEEPBIND 三个嫌疑全部排除；
反过来先 dlopen SDK 再 import sqlalchemy，Init/GetChatData/DecryptData 全链路正常
（探针=scripts/wecom_order_probe.py loadfirst 模式）。

因此本模块必须作为 app 进程的第一个 app 侧 import（main.py 第一行），抢在 SQLAlchemy
之前把 .so 驻留进程。dlopen 同路径有缓存，sdk.py 里的 CDLL 只是引用计数 +1，无副作用。

纪律：
- 本模块只准 import ctypes/logging/os——任何重量级依赖都会破坏「抢先」的意义；
- 未配置（本地开发/测试）或逃生开关关闭时 no-op，不影响本地跑测试。
"""
import ctypes
import logging
import os

logger = logging.getLogger(__name__)

PRELOADED = None  # 驻留句柄，防 GC 触发 dlclose

_enabled = os.environ.get("WECOM_ARCHIVE_SYNC_ENABLED", "true").strip().lower() not in ("0", "false", "no")
_so_path = os.environ.get("WECOM_ARCHIVE_SO_PATH", "/app/data/wecom_archive/libWeWorkFinanceSdk_C.so")

if (
    _enabled
    and os.environ.get("WECOM_CORPID")
    and os.environ.get("WECOM_ARCHIVE_SECRET")
    and os.path.exists(_so_path)
):
    # 注意：原生崩溃（SIGABRT）此处捕不到——逃生开关 WECOM_ARCHIVE_SYNC_ENABLED=false 是兜底
    PRELOADED = ctypes.CDLL(_so_path)
    logger.info("wecom archive sdk preloaded before sqlalchemy: %s", _so_path)
