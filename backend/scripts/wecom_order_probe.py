# -*- coding: utf-8 -*-
"""M31 事故定位终局探针：greenlet 定罪 + 「先装 SDK 再 import sqlalchemy」顺序疗法验证。

背景（五轮收窄）：sqlalchemy 一进进程，dlopen 企微 SDK 必崩；deepbind/nocext 都救不回；
asyncpg/pgvector/httpx/cryptography 全部清白。sqlalchemy 剩下的唯一原生依赖=greenlet。

变体：
    nogreenlet  封锁 greenlet 后 import sqlalchemy，再 CDLL —— 若过，greenlet 定罪
    loadfirst   先 CDLL+NewSdk+Init+拉一页+解密一条，再 import sqlalchemy，再拉+解一次
                —— 若两次都过，证明「SDK 抢先加载」可作为单进程疗法（把 CDLL 提到 main.py 第一行）

云上执行（一次性容器；必须 python -u）：
    docker compose -f docker-compose.prod.yml run --rm app python -u /app/data/wecom_archive/wecom_order_probe.py nogreenlet; echo EXIT=$?
    docker compose -f docker-compose.prod.yml run --rm app python -u /app/data/wecom_archive/wecom_order_probe.py loadfirst; echo EXIT=$?

安全：只打印计数/标记，绝不打印会话正文；Secret/私钥只从 env/文件读。
"""
import ctypes
import os
import sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "nogreenlet"
SO_PATH = os.environ.get("WECOM_ARCHIVE_SO_PATH", "/app/data/wecom_archive/libWeWorkFinanceSdk_C.so")
KEY_PATH = os.environ.get("WECOM_ARCHIVE_KEY_PATH", "/app/data/wecom_archive/private_key.pem")
CORPID = os.environ.get("WECOM_CORPID", "")
SECRET = os.environ.get("WECOM_ARCHIVE_SECRET", "")


class _Slice(ctypes.Structure):
    _fields_ = [("buf", ctypes.c_void_p), ("len", ctypes.c_int)]


class _Block:
    """让指定模块 import 抛 ImportError。"""

    def __init__(self, prefix):
        self.prefix = prefix

    def find_module(self, name, path=None):
        return self if name.startswith(self.prefix) else None

    def load_module(self, name):
        raise ImportError(f"blocked by probe: {name}")


def _load_sdk():
    from ctypes import POINTER, c_char_p, c_int, c_uint, c_ulonglong, c_void_p

    lib = ctypes.CDLL(SO_PATH)
    print("[OK] CDLL 加载成功", flush=True)
    lib.NewSdk.restype = c_void_p
    lib.Init.argtypes = [c_void_p, c_char_p, c_char_p]
    lib.NewSlice.restype = POINTER(_Slice)
    lib.GetChatData.argtypes = [c_void_p, c_ulonglong, c_uint, c_char_p, c_char_p, c_int, POINTER(_Slice)]
    lib.DecryptData.argtypes = [c_char_p, c_char_p, POINTER(_Slice)]
    lib.FreeSlice.argtypes = [POINTER(_Slice)]
    lib.DestroySdk.argtypes = [c_void_p]
    sdk = lib.NewSdk()
    print("[OK] NewSdk 成功", flush=True)
    return lib, sdk


def _roundtrip(lib, sdk, tag: str) -> None:
    """Init + 拉一条 + 解密一条（有 ver 匹配数据时），验证 SDK 在该进程状态下仍然可用。"""
    import base64
    import json

    ret = lib.Init(sdk, CORPID.encode(), SECRET.encode())
    print(f"[{tag}] Init 返回 {ret}", flush=True)
    if ret != 0:
        return
    out = lib.NewSlice()
    ret = lib.GetChatData(sdk, 0, 1, b"", b"", 10, out)
    print(f"[{tag}] GetChatData 返回 {ret}", flush=True)
    if ret == 0:
        payload = json.loads(ctypes.string_at(out.contents.buf, out.contents.len))
        items = payload.get("chatdata") or []
        print(f"[{tag}] 拉到 {len(items)} 条", flush=True)
        ver = int(os.environ.get("WECOM_ARCHIVE_PUBKEY_VER", "2"))
        target = next((it for it in items if it.get("publickey_ver") == ver), None)
        if target:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import padding

            priv = serialization.load_pem_private_key(open(KEY_PATH, "rb").read(), password=None)
            rand_key = priv.decrypt(base64.b64decode(target["encrypt_random_key"]), padding.PKCS1v15()).strip()
            mout = lib.NewSlice()
            ret = lib.DecryptData(rand_key, target["encrypt_chat_msg"].encode(), mout)
            print(f"[{tag}] DecryptData 返回 {ret}", flush=True)
            if ret == 0:
                m = json.loads(ctypes.string_at(mout.contents.buf, mout.contents.len))
                print(f"[{tag}] 解密成功 msgtype={m.get('msgtype')}（不打正文）", flush=True)
            lib.FreeSlice(mout)
    lib.FreeSlice(out)


def main() -> int:
    if MODE == "nogreenlet":
        sys.meta_path.insert(0, _Block("greenlet"))
        print("[..] 已封锁 greenlet，import sqlalchemy…", flush=True)
        import sqlalchemy  # noqa: F401

        print("[..] import sqlalchemy 完成（greenlet 被封锁）", flush=True)
        lib, sdk = _load_sdk()
        lib.DestroySdk(sdk)
        print("[DONE] nogreenlet 干净退出 ✔ → greenlet 就是凶手", flush=True)
        return 0

    # loadfirst：SDK 全套跑一遍 → import sqlalchemy → 再跑一遍
    lib, sdk = _load_sdk()
    _roundtrip(lib, sdk, "前")
    print("[..] 现在 import sqlalchemy…", flush=True)
    import sqlalchemy  # noqa: F401

    print("[OK] import sqlalchemy 完成（SDK 已驻留）", flush=True)
    _roundtrip(lib, sdk, "后")
    lib.DestroySdk(sdk)
    print("[DONE] loadfirst 干净退出 ✔ → 「SDK 抢先加载」疗法可行", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
