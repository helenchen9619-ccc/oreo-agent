# -*- coding: utf-8 -*-
"""M31.2 探针：验证 WECOM_CONTACT_SECRET 是否有通讯录读取权限（48009 定位用）。

分两步打印：①gettoken 是否成功（secret 本身有效）；②user/list 的 errcode/errmsg
（48009=该 secret 对应应用无通讯录权限，需企微后台开权限或换通讯录助手 secret）。

云上执行（一次性容器，不影响在跑服务；必须 python -u）：
    docker compose -f docker-compose.prod.yml run --rm \
      -e WECOM_ARCHIVE_SYNC_ENABLED=false app python -u /app/scripts/wecom_contact_probe.py
也可放 data 卷免重建：python -u /app/data/wecom_archive/wecom_contact_probe.py

安全：只打印 errcode/人数，绝不打印 secret/token/成员隐私字段。
"""
import asyncio
import sys

sys.path.insert(0, "/app")  # 放 data 卷运行时手动补应用包路径（镜像内运行无害）

import httpx

from app.core.config import get_settings

BASE = "https://qyapi.weixin.qq.com/cgi-bin"


async def main() -> int:
    s = get_settings()
    if not (s.WECOM_CORPID and s.WECOM_CONTACT_SECRET):
        print("[FAIL] WECOM_CORPID / WECOM_CONTACT_SECRET 未配置")
        return 1
    print(f"[..] corpid={s.WECOM_CORPID} contact_secret=***{s.WECOM_CONTACT_SECRET[-4:]}（只显示后4位）")
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{BASE}/gettoken", params={"corpid": s.WECOM_CORPID, "corpsecret": s.WECOM_CONTACT_SECRET})
        d = r.json()
        if d.get("errcode") != 0:
            print(f"[FAIL] gettoken errcode={d.get('errcode')} errmsg={d.get('errmsg')} → secret 本身无效/不是本企业应用")
            return 1
        print("[OK] gettoken 成功（secret 有效）")
        r = await c.get(f"{BASE}/user/list", params={"access_token": d["access_token"], "department_id": 1, "fetch_child": 1})
        d = r.json()
        if d.get("errcode") != 0:
            print(f"[FAIL] user/list errcode={d.get('errcode')} errmsg={d.get('errmsg')}")
            if d.get("errcode") == 48009:
                print("[HINT] 48009=该应用无通讯录读取权限：企微管理后台→应用管理→对应应用→开「通讯录同步/读取」权限，"
                      "或把 WECOM_CONTACT_SECRET 换成有权限的通讯录助手 secret")
            return 1
        print(f"[DONE] user/list 成功，成员数={len(d.get('userlist') or [])}")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
