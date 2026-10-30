# -*- coding: utf-8 -*-
"""M31 事故定位第五轮：二分 app 依赖树——哪个 import 之后 dlopen 企微 SDK 必崩。

背景（四轮探针收窄）：
- 裸进程四种线程/符号组合 Init+GetChatData+DecryptData 全过；
- 完整 app 环境下 CDLL 加载 .so 本身就崩（E1 标记打不出来）；
- 与 DB、线程、执行器、deepbind 全无关。
结论：app 依赖树里某个原生扩展库与 .so 初始化冲突。本脚本 import 指定模块后立刻 CDLL，
一个容器跑一个模块，崩了=该模块（或其传递依赖）是凶手。

云上执行（每行一个容器，必须 python -u）：
    docker compose -f docker-compose.prod.yml run --rm app python -u /app/data/wecom_archive/wecom_cdll_after_import.py none; echo EXIT=$?
    docker compose -f docker-compose.prod.yml run --rm app python -u /app/data/wecom_archive/wecom_cdll_after_import.py app.core.config; echo EXIT=$?
    docker compose -f docker-compose.prod.yml run --rm app python -u /app/data/wecom_archive/wecom_cdll_after_import.py app.core.database; echo EXIT=$?
    docker compose -f docker-compose.prod.yml run --rm app python -u /app/data/wecom_archive/wecom_cdll_after_import.py app.wecom_archive.sync; echo EXIT=$?
    docker compose -f docker-compose.prod.yml run --rm app python -u /app/data/wecom_archive/wecom_cdll_after_import.py app.main; echo EXIT=$?

安全：只做 import+加载，不碰 Secret/私钥/消息正文。
"""
import ctypes
import importlib
import os
import sys

sys.path.insert(0, "/app")

MOD_RAW = sys.argv[1] if len(sys.argv) > 1 else "none"
PARTS = [x for x in MOD_RAW.split(",") if x]
MOD = PARTS[0] if PARTS else "none"
FLAGS = set(PARTS[1:])
SO_PATH = os.environ.get("WECOM_ARCHIVE_SO_PATH", "/app/data/wecom_archive/libWeWorkFinanceSdk_C.so")


class _Slice(ctypes.Structure):
    _fields_ = [("buf", ctypes.c_void_p), ("len", ctypes.c_int)]


class _BlockCext:
    """nocext 变体：让 sqlalchemy.cyextension.* 全部 ImportError，强制 SQLAlchemy 回落纯 Python 实现。"""

    def find_module(self, name, path=None):
        if name.startswith("sqlalchemy.cyextension"):
            return self
        return None

    def load_module(self, name):
        raise ImportError("blocked by probe (nocext)")


def main() -> int:
    if "nocext" in FLAGS:
        sys.meta_path.insert(0, _BlockCext())
        print("[..] nocext：已封锁 sqlalchemy C 扩展", flush=True)
    if MOD != "none":
        importlib.import_module(MOD)
        print(f"[..] import {MOD} 完成", flush=True)
    else:
        print("[..] 基线：不 import 任何 app 模块", flush=True)

    mode_flags = os.RTLD_LOCAL
    if "deepbind" in FLAGS:
        mode_flags |= getattr(os, "RTLD_DEEPBIND", 0)
        print("[..] deepbind：CDLL 加 RTLD_DEEPBIND", flush=True)
    lib = ctypes.CDLL(SO_PATH, mode=mode_flags)
    print("[OK] CDLL 加载成功", flush=True)
    lib.NewSdk.restype = ctypes.c_void_p
    lib.Init.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
    lib.DestroySdk.argtypes = [ctypes.c_void_p]
    sdk = lib.NewSdk()
    print("[OK] NewSdk 成功", flush=True)
    lib.DestroySdk(sdk)
    print("[DONE] 干净退出 ✔（不 Init，不消耗任何凭证配额）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
