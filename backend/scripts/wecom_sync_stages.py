# -*- coding: utf-8 -*-
"""M31 事故定位第四轮：阶段 E（SDK 加载+Init）细分 + 加载顺序/线程/符号变体。

背景：第三轮证明崩点在阶段 E（_ensure_sdk），且仅在「完整 app 导入 + 真 DB 查询之后」。
裸探针（wecom_probe_preload.py）同样的 Init 四种模式全过。
本轮：E 拆成 E1 CDLL 加载 / E2 NewSdk / E3 Init 三小段，并用 argv 变体测三个假设：
    （无参数）      与线上一致：先 DB 后 SDK、走单线程执行器 —— 预期复现崩溃
    sdkfirst       SDK 抢在打开 DB 会话之前加载 —— 验证「加载顺序/后来者干扰」
    direct         SDK 在主线程直接 Init，不走执行器 —— 验证 run_in_executor 本身
    deepbind       CDLL 加 RTLD_DEEPBIND（真实 app 依赖树下）—— 验证符号冲突
可组合：deepbind,sdkfirst

云上执行（一次性容器；必须 python -u）：
    docker compose -f docker-compose.prod.yml run --rm app python -u /app/data/wecom_archive/wecom_sync_stages.py [变体]

安全：只打印计数/标记，绝不打印会话正文；Secret/私钥只从 env/文件读。
"""
import asyncio
import ctypes
import os
import sys
import threading

sys.path.insert(0, "/app")  # 脚本在 data 卷里运行，手动补上应用包路径
os.environ["WECOM_ARCHIVE_SYNC_ENABLED"] = "true"  # 覆盖 .env.prod 的逃生开关（本脚本专为复现崩溃）

FLAGS = {x for x in (sys.argv[1] if len(sys.argv) > 1 else "").split(",") if x}


def mark(tag: str) -> None:
    print(f"== 阶段 {tag} 通过 ==", flush=True)


def _build_sdk(st):
    """手工复刻 WecomSdk.__init__ 的三步，逐步打标记；返回可用的 WecomSdk 实例。"""
    from ctypes import POINTER, c_char_p, c_char_p, c_int, c_uint, c_ulonglong, c_void_p

    from app.wecom_archive.sdk import WecomSdk, _Slice

    flags = os.RTLD_LOCAL
    if "deepbind" in FLAGS:
        flags |= getattr(os, "RTLD_DEEPBIND", 0)
    lib = ctypes.CDLL(st.WECOM_ARCHIVE_SO_PATH, mode=flags)
    mark("E1 CDLL 加载 .so")
    lib.NewSdk.restype = c_void_p
    lib.Init.argtypes = [c_void_p, c_char_p, c_char_p]
    lib.NewSlice.restype = POINTER(_Slice)
    lib.GetChatData.argtypes = [c_void_p, c_ulonglong, c_uint, c_char_p, c_char_p, c_int, POINTER(_Slice)]
    lib.DecryptData.argtypes = [c_char_p, c_char_p, POINTER(_Slice)]
    lib.FreeSlice.argtypes = [POINTER(_Slice)]
    lib.DestroySdk.argtypes = [c_void_p]
    sdk = lib.NewSdk()
    mark("E2 NewSdk")
    ret = lib.Init(sdk, st.WECOM_CORPID.encode(), st.WECOM_ARCHIVE_SECRET.encode())
    print(f"    Init 返回 {ret}", flush=True)
    mark("E3 Init")
    obj = WecomSdk.__new__(WecomSdk)
    obj._lib = lib
    obj._sdk = sdk
    return obj


async def _init_sdk(st, S):
    if "direct" in FLAGS:
        return _build_sdk(st)  # 主线程直接调，不过任何线程
    if "thread" in FLAGS:
        box: dict = {}
        t = threading.Thread(target=lambda: box.setdefault("sdk", _build_sdk(st)), name="wecom-probe")
        t.start()
        t.join()
        return box["sdk"]
    return await S._sdk_run(_build_sdk, st)  # 线上路径：单线程执行器


async def main() -> int:
    print(f"[..] FLAGS={sorted(FLAGS) or '无（线上路径复现）'}", flush=True)

    from app.core.database import SessionLocal
    from app.wecom_archive import sync as S

    mark("A 导入 app 包（database/sync 模块）")
    st = S.get_settings()

    sdk = None
    if "sdkfirst" in FLAGS:
        sdk = await _init_sdk(st, S)
        mark("E' SDK 先于 DB 加载")

    async with SessionLocal() as db:
        mark("B 打开 DB 会话")

        seq = await S._get_seq(db)
        print(f"    seq 游标 = {seq}", flush=True)
        mark("C 读 seq 游标（asyncpg 真查询）")

        keywords = await S.load_keywords(db)
        print(f"    敏感词 {len(keywords)} 个", flush=True)
        mark("D 读敏感词")

        if sdk is None:
            sdk = await _init_sdk(st, S)
            mark("E SDK 加载+Init")

        items = await S._sdk_run(sdk.get_chat_data, seq, 100)
        print(f"    拉到 {len(items)} 条密文", flush=True)
        mark("F GetChatData limit=100")

        priv = S._private_key_pem(st.WECOM_ARCHIVE_KEY_PATH)
        ver = int(st.WECOM_ARCHIVE_PUBKEY_VER or "2")
        decrypted, skipped = await S._sdk_run(S._decrypt_page, sdk, priv, items, ver)
        print(f"    解密 {len(decrypted)} 条，跳过版本不符 {skipped} 条", flush=True)
        mark("H RSA+DecryptData 整页解密")

        await db.rollback()  # 定位用脚本，不落库
        mark("M rollback（不写库）")

    print("[DONE] 全部阶段干净通过 ✔", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
