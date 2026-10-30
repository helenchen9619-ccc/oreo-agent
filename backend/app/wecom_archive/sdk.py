"""企业微信会话存档官方 SDK 的 ctypes 封装（M31，蓝本=scripts/wecom_smoke.py Phase 0 联调脚本）。

ctypes 纪律（Phase 0 踩坑沉淀，勿改）：
- SDK v3.0 头文件口径：Slice 由 SDK 的 NewSlice() 分配，GetChatData/DecryptData 收 Slice_t* **单指针**
  （传双指针会段错误 EXIT=139）；buf 不保证 \0 结尾，必须按 len 读。
- 每条消息带 publickey_ver，**换公钥前的历史消息永远解不开**（DecryptData 10008=随机密钥错）。
- 严禁把会话正文 print 进日志。
- 所有方法都是阻塞调用，调用方必须走 sync.py 的 _sdk_run（单线程执行器）。
- dlopen 顺序敏感：本模块依赖 early_load 抢在 SQLAlchemy 前预载（M31 事故修复）。
"""
import base64
import ctypes
import json
from ctypes import POINTER, Structure, c_char_p, c_int, c_uint, c_ulonglong, c_void_p


class WecomSdkError(Exception):
    """SDK 调用失败（Init/GetChatData/DecryptData 返回非 0 或 errcode!=0）。"""


class _Slice(Structure):
    _fields_ = [("buf", c_void_p), ("len", c_int)]


def _slice_bytes(ptr) -> bytes:
    return ctypes.string_at(ptr.contents.buf, ptr.contents.len)


def decrypt_random_key(priv_key_pem: bytes, encrypt_random_key_b64: str) -> bytes:
    """RSA PKCS1v15 解 encrypt_random_key 得到随机密钥（纯 Python，可单测）。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    priv = serialization.load_pem_private_key(priv_key_pem, password=None)
    return priv.decrypt(base64.b64decode(encrypt_random_key_b64), padding.PKCS1v15()).strip()


class WecomSdk:
    """一个实例=一次 NewSdk+Init；用完必须 close()（DestroySdk）。"""

    def __init__(self, so_path: str, corpid: str, secret: str):
        # 优先复用 early_load 的预载句柄（M31 事故修复：SDK 必须抢在 SQLAlchemy 之前 dlopen，
        # 见 early_load.py docstring）。dlopen 同路径本就有缓存，此处为语义显化。
        from app.wecom_archive import early_load

        self._lib = early_load.PRELOADED or ctypes.CDLL(so_path)
        lib = self._lib
        lib.NewSdk.restype = c_void_p
        lib.Init.argtypes = [c_void_p, c_char_p, c_char_p]
        lib.NewSlice.restype = POINTER(_Slice)
        lib.GetChatData.argtypes = [c_void_p, c_ulonglong, c_uint, c_char_p, c_char_p, c_int, POINTER(_Slice)]
        lib.DecryptData.argtypes = [c_char_p, c_char_p, POINTER(_Slice)]
        lib.FreeSlice.argtypes = [POINTER(_Slice)]
        lib.DestroySdk.argtypes = [c_void_p]
        self._sdk = lib.NewSdk()
        ret = lib.Init(self._sdk, corpid.encode(), secret.encode())
        if ret != 0:
            lib.DestroySdk(self._sdk)
            self._sdk = None
            raise WecomSdkError(f"SDK Init 返回 {ret}（Secret 不对 / IP 不在可信名单）")

    def get_chat_data(self, seq: int, limit: int = 100) -> list[dict]:
        """拉一页密文。返回 [{seq, publickey_ver, encrypt_random_key, encrypt_chat_msg}, ...]。"""
        out = self._lib.NewSlice()
        try:
            ret = self._lib.GetChatData(self._sdk, seq, limit, b"", b"", 10, out)
            if ret != 0:
                raise WecomSdkError(f"GetChatData 返回 {ret}（网络/限频）")
            payload = json.loads(_slice_bytes(out).decode("utf-8", errors="replace"))
        finally:
            self._lib.FreeSlice(out)
        if payload.get("errcode") != 0:
            raise WecomSdkError(f"GetChatData errcode={payload.get('errcode')} errmsg={payload.get('errmsg')}")
        return payload.get("chatdata") or []

    def decrypt_data(self, rand_key: bytes, encrypt_chat_msg: str) -> dict:
        """SDK DecryptData 出明文 JSON。"""
        out = self._lib.NewSlice()
        try:
            ret = self._lib.DecryptData(rand_key, encrypt_chat_msg.encode(), out)
            if ret != 0:
                raise WecomSdkError(f"DecryptData 返回 {ret}（10008=随机密钥不被接受/公钥版本不匹配）")
            return json.loads(_slice_bytes(out).decode("utf-8", errors="replace"))
        finally:
            self._lib.FreeSlice(out)

    def close(self) -> None:
        if self._sdk is not None:
            self._lib.DestroySdk(self._sdk)
            self._sdk = None
