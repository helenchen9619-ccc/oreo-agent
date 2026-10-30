# -*- coding: utf-8 -*-
"""M24 Phase 0：企微会话存档最小联调脚本。

只做一件事：ctypes 加载官方 SDK → Init → GetChatData 拉一批密文 → 私钥解 random_key →
SDK DecryptData 出明文 → 打印条数和摘要。验证 Secret / 私钥 / 可信IP / 席位 全链路是否打通。

云上执行（文件经 data 卷挂载进容器，无需重建镜像）：
    上传本文件到 /opt/enterprise-agent/backend/data/wecom_archive/wecom_smoke.py
    docker exec ea-app python /app/data/wecom_archive/wecom_smoke.py

注意：只打印发送人/类型/时间/极短文本摘要用于联调确认，严禁把会话正文写进日志文件。
"""
import base64
import ctypes
import json
import os
import sys
from ctypes import POINTER, Structure, c_char_p, c_int, c_uint, c_ulonglong, c_void_p
from datetime import datetime
from zoneinfo import ZoneInfo

SO_PATH = os.environ.get("WECOM_ARCHIVE_SO_PATH", "/app/data/wecom_archive/libWeWorkFinanceSdk_C.so")
KEY_PATH = os.environ.get("WECOM_ARCHIVE_KEY_PATH", "/app/data/wecom_archive/private_key.pem")
CORPID = os.environ.get("WECOM_CORPID", "")
SECRET = os.environ.get("WECOM_ARCHIVE_SECRET", "")

_TZ = ZoneInfo("Asia/Shanghai")


class _Slice(Structure):
    """官方 SDK 的 Slice_t：buf 不保证 \0 结尾，必须按 len 读。"""

    _fields_ = [("buf", c_void_p), ("len", c_int)]


def _slice_str(ptr) -> str:
    s = ctypes.string_at(ptr.contents.buf, ptr.contents.len)
    return s.decode("utf-8", errors="replace")


def main() -> int:
    if not (CORPID and SECRET):
        print("[FAIL] WECOM_CORPID / WECOM_ARCHIVE_SECRET 未配置（检查 .env.prod 是否被 compose env_file 加载）")
        return 1
    if not os.path.exists(SO_PATH):
        print(f"[FAIL] SDK 不存在: {SO_PATH}")
        return 1
    if not os.path.exists(KEY_PATH):
        print(f"[FAIL] 私钥不存在: {KEY_PATH}")
        return 1

    lib = ctypes.CDLL(SO_PATH)
    lib.NewSdk.restype = c_void_p
    lib.Init.argtypes = [c_void_p, c_char_p, c_char_p]
    # v3.0 头文件口径：Slice 由 SDK 的 NewSlice() 分配，函数收 Slice_t* 单指针（不是双指针！）
    lib.NewSlice.restype = POINTER(_Slice)
    lib.GetChatData.argtypes = [c_void_p, c_ulonglong, c_uint, c_char_p, c_char_p, c_int, POINTER(_Slice)]
    lib.DecryptData.argtypes = [c_char_p, c_char_p, POINTER(_Slice)]
    lib.FreeSlice.argtypes = [POINTER(_Slice)]
    lib.DestroySdk.argtypes = [c_void_p]

    sdk = lib.NewSdk()
    ret = lib.Init(sdk, CORPID.encode(), SECRET.encode())
    if ret != 0:
        print(f"[FAIL] Init 返回 {ret}（Secret 不对 / IP 不在可信名单）")
        lib.DestroySdk(sdk)
        return 1
    print("[OK] SDK Init 成功（Secret + 可信IP 通过）")

    out = lib.NewSlice()
    print("[..] 正在调用 GetChatData（若此处无后续输出且 EXIT=139，即 SDK 内部段错误）")
    ret = lib.GetChatData(sdk, 0, 100, b"", b"", 10, out)
    if ret != 0:
        print(f"[FAIL] GetChatData 返回 {ret}（网络/限频问题）")
        lib.DestroySdk(sdk)
        return 1
    payload = json.loads(_slice_str(out))
    lib.FreeSlice(out)
    if payload.get("errcode") != 0:
        print(f"[FAIL] GetChatData errcode={payload.get('errcode')} errmsg={payload.get('errmsg')}")
        lib.DestroySdk(sdk)
        return 1
    items = payload.get("chatdata") or []
    print(f"[OK] 拉到 {len(items)} 条密文（seq 游标模式正常）")
    if not items:
        print("[提示] 无数据：先用绑定了存档席位的账号发几条消息（私聊/群聊都行），等 1 分钟再跑")
        lib.DestroySdk(sdk)
        return 0
    # 全量列出 seq/ver 分布——换过公钥后新旧消息混杂，先看清哪些是 ver=2 再决定解谁
    for it in items:
        print(f"  [..] seq={it.get('seq')} publickey_ver={it.get('publickey_ver')}")
    cur_ver = int(os.environ.get("WECOM_ARCHIVE_PUBKEY_VER", "2"))
    targets = [it for it in items if it.get("publickey_ver") == cur_ver]
    if not targets:
        print(f"[提示] 没有 ver={cur_ver} 的消息（新消息可能还没归档，等 1~2 分钟再跑）；跳过解密")
        lib.DestroySdk(sdk)
        return 0

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    priv = serialization.load_pem_private_key(open(KEY_PATH, "rb").read(), password=None)

    ok = 0
    for it in targets[:5]:
        seq, ver = it.get("seq"), it.get("publickey_ver")
        try:
            rand_key = priv.decrypt(base64.b64decode(it["encrypt_random_key"]), padding.PKCS1v15()).strip()
        except Exception as e:  # noqa: BLE001 - 联调脚本要把所有异常亮出来
            print(f"  [FAIL] seq={seq} ver={ver} RSA 解密失败: {e}"
                  "（这条消息是用旧公钥加密的，新私钥解不开；换公钥前的历史消息属于正常）")
            continue
        # 联调诊断：随机密钥本身应是 base64 形态的短字符串；打出长度和前缀验证（仅联调期保留）
        print(f"  [..] seq={seq} ver={ver} rand_key len={len(rand_key)} head={rand_key[:12]!r}")
        try:
            msg_out = lib.NewSlice()
            ret = lib.DecryptData(rand_key, it["encrypt_chat_msg"].encode(), msg_out)
            if ret != 0:
                print(f"  [FAIL] seq={seq} ver={ver} DecryptData 返回 {ret}"
                      "（10008=随机密钥不被接受）")
                lib.FreeSlice(msg_out)
                continue
            msg = json.loads(_slice_str(msg_out))
            lib.FreeSlice(msg_out)
            ok += 1
            ts = datetime.fromtimestamp(msg.get("msgtime", 0) / 1000, tz=_TZ).strftime("%m-%d %H:%M:%S")
            text = ((msg.get("text") or {}).get("content") or "")[:30].replace("\n", " ")
            room = f" 群:{msg.get('roomid')[:8]}…" if msg.get("roomid") else " 单聊"
            print(f"  [OK] seq={it.get('seq')} {ts} {msg.get('from')}{room} [{msg.get('msgtype')}] {text}")
        except Exception as e:  # noqa: BLE001 - 联调脚本要把所有异常亮出来
            print(f"  [FAIL] seq={it.get('seq')} 解密异常: {e}")
    lib.DestroySdk(sdk)
    if ok:
        print(f"[DONE] 解密成功 {ok}/{min(len(items), 5)} 条，全链路打通 ✔")
        return 0
    print("[FAIL] 一条都没解开：大概率私钥和后台公钥不配对，或 WECOM_ARCHIVE_PUBKEY_VER 与后台版本号不一致")
    return 1


if __name__ == "__main__":
    sys.exit(main())
