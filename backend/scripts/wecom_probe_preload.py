# -*- coding: utf-8 -*-
"""M31 事故探针：定位企微 SDK 在应用进程内崩溃（free(): invalid pointer）的真凶。

背景：Phase 0 冒烟脚本（干净进程 + 主线程）全链路通过；M31 挂进 uvicorn 后启动即
SIGABRT。嫌疑二选一（或叠加）：
  A. 符号冲突——uvicorn 进程已加载 ssl/cryptography 的 OpenSSL，SDK 自带 crypto 被符号插断；
  B. 主线程亲和——SDK 内部要求在进程主线程 Init/调用，worker 线程（哪怕固定一条）也不行。

四种模式各跑一次（一次性容器，不影响在跑的服务）：
    docker compose -f docker-compose.prod.yml run --rm app python -u /app/data/wecom_archive/wecom_probe_preload.py plain
    docker compose -f docker-compose.prod.yml run --rm app python -u /app/data/wecom_archive/wecom_probe_preload.py thread
    docker compose -f docker-compose.prod.yml run --rm app python -u /app/data/wecom_archive/wecom_probe_preload.py deepbind
    docker compose -f docker-compose.prod.yml run --rm app python -u /app/data/wecom_archive/wecom_probe_preload.py deepbind+thread

判读：
  plain 崩、deepbind 好 → 符号冲突，修复=CDLL 加 RTLD_DEEPBIND
  plain 好、thread 崩   → 主线程亲和，修复=SDK 调用挪主线程或独立进程
  都崩                 → 两因叠加，deepbind+thread 必须好才有单进程方案
  都好                 → 探针没复现 app 环境差异，再议（看 lifespan 里还干了什么）

必须 python -u（段错误会丢 print 缓冲，Phase 0 教训）。
安全：只打印返回码/字节数/密文索引前缀，绝不打印解密正文；Secret 从 env 读，不落任何输出。
"""
import ctypes
import os
import sys
import threading

MODE = sys.argv[1] if len(sys.argv) > 1 else "plain"

# 模拟 uvicorn 应用进程的真实状态：SDK 加载之前这些库已驻留（app.main 启动即导入）
import ssl  # noqa: F401
import asyncio  # noqa: F401
import httpx  # noqa: F401
import asyncpg  # noqa: F401
from cryptography.fernet import Fernet  # noqa: F401

ssl.create_default_context()  # 强制 OpenSSL 真实载入（import 本身可能是惰性的）
Fernet(Fernet.generate_key())  # 强制 cryptography 绑定 libcrypto

SO_PATH = os.environ.get("WECOM_ARCHIVE_SO_PATH", "/app/data/wecom_archive/libWeWorkFinanceSdk_C.so")
KEY_PATH = os.environ.get("WECOM_ARCHIVE_KEY_PATH", "/app/data/wecom_archive/private_key.pem")
CORPID = os.environ.get("WECOM_CORPID", "")
SECRET = os.environ.get("WECOM_ARCHIVE_SECRET", "")


class _Slice(ctypes.Structure):
    _fields_ = [("buf", ctypes.c_void_p), ("len", ctypes.c_int)]


def _run() -> int:
    import base64
    import json

    mode_flags = os.RTLD_LOCAL
    if "deepbind" in MODE:
        mode_flags |= getattr(os, "RTLD_DEEPBIND", 0)
    lib = ctypes.CDLL(SO_PATH, mode=mode_flags)
    lib.NewSdk.restype = ctypes.c_void_p
    lib.Init.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
    lib.NewSlice.restype = ctypes.POINTER(_Slice)
    lib.GetChatData.argtypes = [ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_uint,
                                ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(_Slice)]
    lib.DecryptData.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(_Slice)]
    lib.FreeSlice.argtypes = [ctypes.POINTER(_Slice)]
    lib.DestroySdk.argtypes = [ctypes.c_void_p]

    sdk = lib.NewSdk()
    ret = lib.Init(sdk, CORPID.encode(), SECRET.encode())
    print(f"[..] Init 返回 {ret}")
    if ret != 0:
        lib.DestroySdk(sdk)
        return 1
    out = lib.NewSlice()
    ret = lib.GetChatData(sdk, 0, 1, b"", b"", 10, out)
    print(f"[..] GetChatData 返回 {ret}")
    if ret != 0:
        lib.FreeSlice(out)
        lib.DestroySdk(sdk)
        return 1
    raw = ctypes.string_at(out.contents.buf, out.contents.len)
    print(f"[OK] 拿到应答 {len(raw)} 字节（密文索引，非正文）")
    lib.FreeSlice(out)

    # 第二阶段（v2 探针新增）：RSA 解随机密钥 + SDK DecryptData——crypto 符号冲突的崩点在这里
    payload = json.loads(raw)
    items = payload.get("chatdata") or []
    print(f"[..] chatdata {len(items)} 条")
    ver = int(os.environ.get("WECOM_ARCHIVE_PUBKEY_VER", "2"))
    target = next((it for it in items if it.get("publickey_ver") == ver), None)
    if not target:
        print(f"[提示] 本页无 ver={ver} 消息，DecryptData 阶段跳过（结果仅供参考）")
    else:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        priv = serialization.load_pem_private_key(open(KEY_PATH, "rb").read(), password=None)
        rand_key = priv.decrypt(base64.b64decode(target["encrypt_random_key"]), padding.PKCS1v15()).strip()
        print(f"[..] RSA 解随机密钥成功 len={len(rand_key)}，调 DecryptData…")
        mout = lib.NewSlice()
        ret = lib.DecryptData(rand_key, target["encrypt_chat_msg"].encode(), mout)
        print(f"[..] DecryptData 返回 {ret}")
        if ret == 0:
            m = json.loads(ctypes.string_at(mout.contents.buf, mout.contents.len))
            print(f"[OK] 解密成功 msgtype={m.get('msgtype')} from={m.get('from')}（不打正文）")
        lib.FreeSlice(mout)
    lib.DestroySdk(sdk)
    return 0


def main() -> int:
    print(f"[..] mode={MODE}；已预载 ssl/httpx/asyncpg/cryptography（模拟 uvicorn 进程）")
    if "thread" in MODE:
        box: dict[str, int] = {}

        def _target() -> None:
            box["rc"] = _run()

        t = threading.Thread(target=_target, name="wecom-probe")
        t.start()
        t.join()
        rc = box.get("rc", 99)
    else:
        rc = _run()
    print(f"[DONE] mode={MODE} 进程干净退出 rc={rc} ✔")
    return rc


if __name__ == "__main__":
    sys.exit(main())
