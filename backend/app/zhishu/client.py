"""M9c：智书合同开放平台只读客户端（tenant_access_token 身份）。httpx 直连，无新依赖。

与飞书 per-user OAuth 不同：智书是公司级单凭证（ZHISHU_APP_ID/ZHISHU_APP_SECRET 在 .env），
tenant_access_token 2h 有效（JWT），本模块做进程内缓存 + 临期重取。

「与我相关」的过滤：智书 user_id 是它自己的 8 位字符串 id 空间（如 944f4998），
与飞书 open_id/union_id 完全不通（v6 实测），只能在智书后台/合同上反查 →
存进 users.zhishu_user_id，工具运行时按 user_id + permission=1 过滤本人可见合同。

错误约定：code != 0 时抛 ZhishuApiError(msg)，调用方转成给模型的友好文案。
"""
import time

import httpx

from app.core.config import get_settings

_BASE = "https://open.qfei.cn/open-apis"

# token 缓存：进程内（单 worker），临期 5 分钟重取
_token_cache: dict = {"token": "", "expires_at": 0.0}
_TOKEN_SKEW = 300.0


class ZhishuApiError(Exception):
    """智书 API 业务错误（code != 0），message 原样来自智书 msg 字段。"""


async def zhishu_token(client: httpx.AsyncClient) -> str:
    """取可用 tenant_access_token：缓存未临期直接用，否则重取。未配置凭证抛 ZhishuApiError。"""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - _TOKEN_SKEW:
        return _token_cache["token"]
    s = get_settings()
    if not s.ZHISHU_APP_ID or not s.ZHISHU_APP_SECRET:
        raise ZhishuApiError("未配置智书凭证（ZHISHU_APP_ID/ZHISHU_APP_SECRET），请管理员在 backend/.env.prod 配置")
    # camelCase 实测可用（snake_case 也通，文档两派写法都有，2026-09-01 云上实测）
    r = await client.post(
        f"{_BASE}/auth/v3/tenant_access_token/internal",
        json={"appId": s.ZHISHU_APP_ID, "appSecret": s.ZHISHU_APP_SECRET},
    )
    data = r.json()
    token = data.get("tenant_access_token")  # 顶层返回（非 data 嵌套），expire 顶层秒级
    if not token:
        raise ZhishuApiError(data.get("msg") or f"HTTP {r.status_code}")
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + int(data.get("expire") or 7200)
    return token


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---------- 合同搜索（user_id 是 8 位智书 id；permission=1 我的可见 / 2 我申请的） ----------
# 时间窗字段映射（字段名/取值来自本地文档 智书合同API资料/合同API具体内容.txt；
# 注意智书的拼写就是 submited_time_*，时间格式 '2021-10-01 11:11:11'）
_TIME_FIELDS = {
    "create": ("create_time_start", "create_time_end"),
    "submit": ("submited_time_start", "submited_time_end"),
    "update": ("update_time_start", "update_time_end"),
    "archive": ("archived_time_start", "archived_time_end"),
}


async def contract_search(
    client: httpx.AsyncClient, token: str, *,
    user_id: str, permission: int = 1,
    contract_name: str = "",
    time_field: str = "", time_start: str = "", time_end: str = "",
    status_in: str = "",
    page_size: int = 20, page_token: str = "",
) -> dict:
    """搜索合同。返回 {items, page_token, has_more}。
    item 含 contract_name/number/status_code/status_name/counter_party_list/our_party_list/
    amount+currency_name/create_user_name/submitter_user_name/submitted_time/signed_time/
    create_user_id 等（v3/v7 实测字段）。
    contract_name 是子串模糊匹配（2026-09-02 探针 v8 实测：传「申请函」命中 9 条名称含该词的合同），
    与 user_id/permission 是 AND 关系——按人过滤不受关键词影响。
    time_field/time_start/time_end/status_in 同为 AND 叠加（M9c.3）。
    """
    body: dict = {
        "combine_condition": {"user_id": user_id, "permission": permission},
        "page_size": min(page_size, 50),
    }
    cc = body["combine_condition"]
    if contract_name:
        cc["contract_name"] = contract_name
    tf = _TIME_FIELDS.get(time_field)
    if tf and (time_start or time_end):
        if time_start:
            cc[tf[0]] = time_start
        if time_end:
            cc[tf[1]] = time_end
    if status_in:
        cc["contract_status_in"] = status_in
    if page_token:
        body["page_token"] = page_token
    r = await client.post(
        f"{_BASE}/contract/v1/contracts/search",
        headers=_headers(token), params={"user_id_type": "user_id"}, json=body,
    )
    data = r.json()
    if data.get("code") != 0:
        raise ZhishuApiError(data.get("msg") or f"HTTP {r.status_code}")
    return data.get("data") or {}


# ---------- 合同详情 ----------
async def contract_detail(client: httpx.AsyncClient, token: str, contract_id: str) -> dict:
    """单个合同详情。返回 contract 对象（全量：双方主体/签约状态/付款计划/预算/附件 form 字段等）。"""
    r = await client.get(
        f"{_BASE}/contract/v1/contracts/{contract_id}",
        headers=_headers(token), params={"user_id_type": "user_id"},
    )
    data = r.json()
    if data.get("code") != 0:
        raise ZhishuApiError(data.get("msg") or f"HTTP {r.status_code}")
    return (data.get("data") or {}).get("contract") or {}
