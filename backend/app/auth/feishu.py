"""飞书自建应用 OAuth（网页免登）。用 httpx 直连开放平台接口，保持依赖简单。"""
from urllib.parse import quote

import httpx

from app.core.config import get_settings

settings = get_settings()
_BASE = "https://open.feishu.cn/open-apis"

# M7：增量授权时显式申请的 user scope 全集（空格分隔，区分大小写）。
# 不传 scope 时飞书只授予最小权限 auth:user.id:read（2026-08-28 云上实测证实）。
# 含 offline_access 才会签发 refresh_token。普通登录不带 scope（避免租户未开通某 scope 时授权报错打死登录）。
FEISHU_USER_SCOPES = (
    "docx:document:readonly wiki:wiki:readonly drive:drive:readonly "
    "search:docs:read im:message im:chat:readonly offline_access "
    "contact:contact.base:readonly "  # 群聊总结把发送者 open_id 解析成姓名（batch 接口实际要求这个，99991679 实测）；缺了静默降级
    "contact:user.base:readonly "  # 用户基本信息（当前 token 已含；重授权若不带会被覆盖丢，M9c 顺带保留）
    "search:message "  # M8：消息搜索（p2p 不在 /im/v1/chats，私聊只能靠搜索发现，99991679 实测 scope 名）
    "im:message.p2p_msg:get_as_user "  # M8：以用户身份读单聊消息正文（解开 p2p 单条 GET 的 230027）
    "mail:user_mailbox.message:readonly "  # M9：读收件箱邮件元数据（列表/详情基础档）
    "mail:user_mailbox.message.subject:read "  # M9：邮件主题字段（字段级粒度 scope，2026-08-31 实测 readonly 档不含 subject）
    "mail:user_mailbox.message.body:read "  # M9：邮件正文字段（同上；metadata 档另有明文 body_preview 兜底）
    "mail:user_mailbox.message.address:read "  # M9b.2：收发件人字段（head_from/to/cc/bcc）——缺了不下发
    "mail:user_mailbox.mail_contact:read "  # M9：邮箱联系人（发件人地址→姓名解析）
    "mail:user_mailbox.mail_contact.mail_address:read "  # M9：联系人的邮箱地址字段
    "mail:user_mailbox.folder:read "  # M9：邮箱文件夹列表（99991679 实测点名）
    "contact:user.employee_id:readonly"  # M9c：用户 user_id（用户ID）字段——智书 user_id_type=user_id 过滤用它；缺了 user_info 不返回该字段
    " sheets:spreadsheet:readonly bitable:app:readonly"  # M10a：读表格/多维表格（folder 列表+文件下载复用 drive:drive:readonly）
    " docx:document sheets:spreadsheet bitable:app"  # M10b：写 docx/sheet/bitable——提前随 M10a 一次授权，免得二次重授权
    " drive:file:upload drive:drive.metadata:readonly"  # M29：上传文件到云盘；M29.1：metadata:readonly=取「我的空间」根目录 root_folder/meta 用（99991679 实测 drive:drive:readonly 不覆盖该接口）
)


def build_authorize_url(redirect_uri: str, state: str = "", scope: str = "") -> str:
    url = (
        f"{_BASE}/authen/v1/authorize"
        f"?app_id={settings.FEISHU_APP_ID}&redirect_uri={redirect_uri}&state={state}"
    )
    if scope:
        url += f"&scope={quote(scope, safe=':')}"  # 空格→%20；冒号保留原样
    return url


async def app_access_token(client: httpx.AsyncClient) -> str:
    """应用级 tenant token（M4a 登录交换用；M8 复用于机器人私聊推送）。"""
    resp = await client.post(
        f"{_BASE}/auth/v3/app_access_token/internal",
        json={"app_id": settings.FEISHU_APP_ID, "app_secret": settings.FEISHU_APP_SECRET},
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取 app_access_token 失败: {data.get('msg')}")
    return data["app_access_token"]


async def fetch_feishu_user(code: str) -> dict:
    """授权码 → 用户信息 + token 三元组（M7：存库用于读用户飞书视野内容）。
    返回 {open_id, name, avatar_url, access_token, refresh_token, expires_in, refresh_expires_in, scope}。"""
    async with httpx.AsyncClient(timeout=10) as client:
        app_token = await app_access_token(client)
        resp = await client.post(
            f"{_BASE}/authen/v1/oidc/access_token",
            headers={"Authorization": f"Bearer {app_token}"},
            json={"grant_type": "authorization_code", "code": code},
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"授权码换 token 失败: {data.get('msg')}")
        d = data["data"]
        user_token = d["access_token"]

        resp = await client.get(
            f"{_BASE}/authen/v1/user_info",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        info = resp.json()
        if info.get("code") != 0:
            raise RuntimeError(f"获取用户信息失败: {info.get('msg')}")
        u = info["data"]
        user_id = u.get("user_id", "") or ""
        # M9c：user_info 缺 user_id（未开 contact:user.employee_id:readonly）时兜底走 contact 单查。
        # 拿到的是飞书「用户ID」字段，智书 user_id_type=user_id 过滤正好用它；拿不到就留空（调用方保留旧值）。
        if not user_id:
            try:
                cr = await client.get(
                    f"{_BASE}/contact/v3/users/{u['open_id']}",
                    headers={"Authorization": f"Bearer {user_token}"},
                    params={"user_id_type": "open_id"},
                )
                cd = cr.json()
                if cd.get("code") == 0:
                    user_id = ((cd.get("data") or {}).get("user") or {}).get("user_id", "") or ""
            except Exception:
                user_id = ""
        return {
            "open_id": u["open_id"],
            "name": u.get("name", ""),
            "avatar_url": u.get("avatar_url", ""),
            "user_id": user_id,
            "access_token": user_token,
            "refresh_token": d.get("refresh_token", ""),
            "expires_in": d.get("expires_in", 7200),
            "refresh_expires_in": d.get("refresh_expires_in", 0),
            "scope": d.get("scope", ""),
        }


async def refresh_user_token(refresh_token: str) -> dict:
    """refresh_token → 新 token 三元组（滚动续期：每次刷新 refresh_token 也换新、30 天重计）。
    返回 {access_token, refresh_token, expires_in, refresh_expires_in, scope}；失败抛 RuntimeError。"""
    async with httpx.AsyncClient(timeout=10) as client:
        app_token = await app_access_token(client)
        resp = await client.post(
            f"{_BASE}/authen/v1/oidc/refresh_access_token",
            headers={"Authorization": f"Bearer {app_token}"},
            json={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"刷新飞书 token 失败: {data.get('msg')}")
        d = data["data"]
        return {
            "access_token": d["access_token"],
            "refresh_token": d.get("refresh_token", ""),
            "expires_in": d.get("expires_in", 7200),
            "refresh_expires_in": d.get("refresh_expires_in", 0),
            "scope": d.get("scope", ""),
        }
