"""M7：飞书 open-apis 只读客户端（user_access_token 身份）。httpx 直连，无新依赖。

错误约定：飞书返回 code != 0 时抛 FeishuApiError(msg)，调用方转成给模型的友好文案。
"""
import asyncio
import json
import re
import time

import httpx

_BASE = "https://open.feishu.cn/open-apis"


class FeishuApiError(Exception):
    """飞书 API 业务错误（code != 0），message 原样来自飞书 msg 字段。"""


async def _get(client: httpx.AsyncClient, token: str, path: str, params: dict | None = None) -> dict:
    resp = await client.get(f"{_BASE}{path}", headers={"Authorization": f"Bearer {token}"}, params=params or {})
    data = resp.json()
    if data.get("code") != 0:
        raise FeishuApiError(data.get("msg") or f"HTTP {resp.status_code}")
    return data.get("data") or {}


async def _post(client: httpx.AsyncClient, token: str, path: str, payload: dict, params: dict | None = None) -> dict:
    resp = await client.post(
        f"{_BASE}{path}", headers={"Authorization": f"Bearer {token}"}, json=payload, params=params or {}
    )
    data = resp.json()
    if data.get("code") != 0:
        raise FeishuApiError(data.get("msg") or f"HTTP {resp.status_code}")
    return data.get("data") or {}


async def _put(client: httpx.AsyncClient, token: str, path: str, payload: dict, params: dict | None = None) -> dict:
    resp = await client.put(
        f"{_BASE}{path}", headers={"Authorization": f"Bearer {token}"}, json=payload, params=params or {}
    )
    data = resp.json()
    if data.get("code") != 0:
        raise FeishuApiError(data.get("msg") or f"HTTP {resp.status_code}")
    return data.get("data") or {}


async def _delete(client: httpx.AsyncClient, token: str, path: str, payload: dict | None = None) -> dict:
    """DELETE（可带 body；httpx 的 delete() 不收 json 参数，走 request）。"""
    kwargs: dict = {"headers": {"Authorization": f"Bearer {token}"}}
    if payload is not None:
        kwargs["json"] = payload
    resp = await client.request("DELETE", f"{_BASE}{path}", **kwargs)
    data = resp.json()
    if data.get("code") != 0:
        raise FeishuApiError(data.get("msg") or f"HTTP {resp.status_code}")
    return data.get("data") or {}


async def read_doc_raw(client: httpx.AsyncClient, token: str, doc_id: str) -> str:
    """docx 文档纯文本内容（免拼 blocks）。"""
    data = await _get(client, token, f"/docx/v1/documents/{doc_id}/raw_content")
    return data.get("content", "")


async def docx_blocks(client: httpx.AsyncClient, token: str, doc_id: str, max_pages: int = 3) -> list[dict]:
    """docx 文档块列表（分页拉取，防爆量最多 max_pages 页）。

    用于发现内嵌 sheet/bitable 块——raw_content 纯文本接口取不到内嵌块内容。
    返回块 dict 列表（各块含 block_type 及对应类型字段，如 sheet.token / bitable.token）。
    """
    items: list[dict] = []
    page_token = ""
    for _ in range(max_pages):
        params: dict = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        data = await _get(client, token, f"/docx/v1/documents/{doc_id}/blocks", params=params)
        items.extend(b for b in data.get("items") or [] if isinstance(b, dict))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token") or ""
        if not page_token:
            break
    return items


async def wiki_resolve(client: httpx.AsyncClient, token: str, wiki_token: str) -> dict:
    """知识库节点 → 底层对象。返回 {obj_token, obj_type, title}。"""
    data = await _get(client, token, "/wiki/v2/spaces/get_node", params={"token": wiki_token, "obj_type": "wiki"})
    node = data.get("node") or {}
    return {
        "obj_token": node.get("obj_token", ""),
        "obj_type": node.get("obj_type", ""),
        "title": node.get("title", ""),
    }


async def search_docs(client: httpx.AsyncClient, token: str, query: str, count: int = 10) -> list[dict]:
    """用户视野搜索云文档。返回 [{title, url, owner_id, obj_type}]。"""
    data = await _post(
        client, token, "/suite/docs-api/search/object",
        {"search_key": query, "count": min(count, 20), "offset": 0, "docs_types": []},
    )
    out = []
    for e in data.get("docs_entities") or []:
        out.append({
            "title": e.get("title", ""),
            "url": e.get("url", ""),
            "owner_id": e.get("owner_id", ""),
            "obj_type": e.get("docs_type", ""),
        })
    return out


async def list_chats(client: httpx.AsyncClient, token: str, max_pages: int = 10) -> list[dict]:
    """用户所在群列表（翻页）。返回 [{chat_id, name, member_count?}]。"""
    chats: list[dict] = []
    page_token = ""
    for _ in range(max_pages):
        params: dict = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        data = await _get(client, token, "/im/v1/chats", params=params)
        for c in data.get("items") or []:
            chats.append({
                "chat_id": c.get("chat_id", ""),
                "name": c.get("name", ""),
                "description": c.get("description", ""),
                "chat_mode": c.get("chat_mode", ""),  # M8：group/p2p/topic_group——p2p 即单聊（name 为空）
            })
        page_token = data.get("page_token") or ""
        if not data.get("has_more") or not page_token:
            break
    return chats


async def contact_user_names(client: httpx.AsyncClient, token: str, open_ids: list[str]) -> dict[str, str]:
    """open_id → 姓名批量解析。返回 {open_id: name}；需要 contact:contact.base:readonly。
    只查 ou_ 开头的真实用户（sender 里可能混 cli_ 开头的机器人账号，接口会拒）。"""
    ids = [i for i in open_ids if i.startswith("ou_")][:50]
    if not ids:
        return {}
    data = await _get(client, token, "/contact/v3/users/batch",
                      params={"user_ids": ids, "user_id_type": "open_id"})
    return {u.get("open_id", ""): u.get("name", "") for u in data.get("items") or []}


def _post_image_keys(raw: str) -> list[str]:
    """M8c.1：富文本（post）消息 content 里按文档序收集所有图片的 image_key（坏 JSON 返回 []）。"""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    post = data.get("post")
    if isinstance(post, dict):  # 带语言包形状 {"post":{"zh_cn":{...}}}
        data = post.get("zh_cn") or next((v for v in post.values() if isinstance(v, dict)) or {}, {})
    keys: list[str] = []
    for para in data.get("content") or []:
        if not isinstance(para, list):
            continue
        for el in para:
            if isinstance(el, dict) and el.get("tag") == "img" and el.get("image_key"):
                keys.append(str(el["image_key"]))
    return keys


def _normalize_msg(m: dict) -> dict:
    """飞书消息对象 → 平台统一结构（chat_messages / get_message 共用）。"""
    body = m.get("body") or {}
    raw_content = body.get("content", "")
    image_key = ""
    if m.get("msg_type") == "image":
        try:
            image_key = json.loads(raw_content).get("image_key", "")
        except (json.JSONDecodeError, ValueError, AttributeError):
            pass
    msg_type = m.get("msg_type", "")
    return {
        "sender": (m.get("sender") or {}).get("id", ""),
        "send_time": int(m.get("create_time", "0") or 0) // 1000,
        "msg_type": msg_type,
        "text": raw_content,
        "mentions": (body.get("mentions") or []),  # M8e：text 消息 @_user_N 占位符 → 真实姓名的 key→name 映射
        "message_id": m.get("message_id", ""),
        "image_key": image_key,
        # M8c.1：post 帖的全部附图（下载管线用；image 消息走上面单值字段）
        "image_keys": _post_image_keys(raw_content) if msg_type == "post" else [],
        "chat_id": m.get("chat_id", ""),
    }


async def chat_messages(
    client: httpx.AsyncClient, token: str, chat_id: str,
    start_ts: int, end_ts: int, max_messages: int = 200,
) -> list[dict]:
    """会话历史消息（用户身份）。时间戳为秒。返回 [_normalize_msg 结构]，升序。"""
    msgs: list[dict] = []
    page_token = ""
    while len(msgs) < max_messages:
        params: dict = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "start_time": str(start_ts),
            "end_time": str(end_ts),
            "sort_type": "ByCreateTimeAsc",
            "page_size": 50,
        }
        if page_token:
            params["page_token"] = page_token
        data = await _get(client, token, "/im/v1/messages", params=params)
        for m in data.get("items") or []:
            msgs.append(_normalize_msg(m))
        page_token = data.get("page_token") or ""
        if not data.get("has_more") or not page_token:
            break
    return msgs[:max_messages]


async def search_messages_page(
    client: httpx.AsyncClient, token: str, start_ts: int, end_ts: int,
    query: str = "", page_token: str = "",
) -> tuple[list[str], str, bool]:
    """消息搜索单页（M28.1 拆出，供定向深扫逐页扫描+命中即停）。返回 (message_id 列表, 下一页 token, has_more)。"""
    payload: dict = {"query": query, "from_time": str(start_ts), "to_time": str(end_ts), "page_size": 20}
    if page_token:
        payload["page_token"] = page_token
    data = await _post(client, token, "/search/v2/message", payload)
    ids = [x for x in (data.get("items") or []) if isinstance(x, str)]
    next_token = data.get("page_token") or ""
    return ids, next_token, bool(data.get("has_more") and next_token)


async def search_messages(
    client: httpx.AsyncClient, token: str, start_ts: int, end_ts: int,
    query: str = "", max_ids: int = 100,
) -> list[str]:
    """消息搜索（M8：时间窗全量扫，query 空=全部；2026-08-28 云上实测空 query 可用）。
    返回 message_id 列表（接口只给 id，正文用 get_message 逐条取）。需 search:message scope。"""
    ids: list[str] = []
    page_token = ""
    while len(ids) < max_ids:
        page, page_token, has_more = await search_messages_page(client, token, start_ts, end_ts, query, page_token)
        ids.extend(page)
        if not has_more:
            break
    return ids[:max_ids]


async def get_message(client: httpx.AsyncClient, token: str, message_id: str) -> dict:
    """单条消息详情（用户身份）。返回 _normalize_msg 结构（含 chat_id）。
    注意：人-人私聊消息会报 230027（开放接口不开放 p2p 正文，2026-08-28 实测）——调用方应跳过计数。"""
    data = await _get(client, token, f"/im/v1/messages/{message_id}")
    items = data.get("items") or []
    if not items:
        raise FeishuApiError("消息不存在或已删除")
    return _normalize_msg(items[0])


async def message_reactions(client: httpx.AsyncClient, token: str, message_id: str) -> list[dict]:
    """M32d：消息的表情回复列表。返回 [{"emoji": emoji_type, "operator": open_id}]（无回复=空列表）。
    需 im:message scope；缺权限抛 FeishuApiError，调用方静默降级。"""
    data = await _get(client, token, f"/im/v1/messages/{message_id}/reactions", params={"page_size": 50})
    out = []
    for r in data.get("items") or []:
        if not isinstance(r, dict):
            continue
        emoji = ((r.get("reaction_type") or {}).get("emoji_type")) or ""
        operator = ((r.get("operator") or {}).get("operator_id")) or ""
        if emoji:
            out.append({"emoji": emoji, "operator": operator})
    return out


async def get_chat_info(client: httpx.AsyncClient, token: str, chat_id: str) -> dict:
    """单个会话信息。返回 {name, chat_mode}。"""
    data = await _get(client, token, f"/im/v1/chats/{chat_id}")
    return {"name": data.get("name", ""), "chat_mode": data.get("chat_mode", "")}


async def download_im_resource(client: httpx.AsyncClient, token: str, message_id: str, file_key: str) -> bytes:
    """下载消息里的图片资源（二进制）。飞书错误时返回 JSON，按 FeishuApiError 抛。"""
    resp = await client.get(
        f"{_BASE}/im/v1/messages/{message_id}/resources/{file_key}",
        headers={"Authorization": f"Bearer {token}"},
        params={"type": "image"},
    )
    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type:
        data = resp.json()
        raise FeishuApiError(data.get("msg") or f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise FeishuApiError(f"HTTP {resp.status_code}")
    return resp.content


# ---------- M8：私聊枚举与机器人推送 ----------

async def chat_members(client: httpx.AsyncClient, token: str, chat_id: str) -> list[str]:
    """会话成员 open_id 列表（user 身份）。用于解析 p2p 单聊的对方是谁。"""
    data = await _get(client, token, f"/im/v1/chats/{chat_id}/members",
                      params={"member_id_type": "open_id", "page_size": 10})
    return [m.get("member_id", "") for m in data.get("items") or [] if m.get("member_id")]


async def send_p2p_card(client: httpx.AsyncClient, app_token: str, open_id: str, card: dict) -> None:
    """应用机器人给用户发 1 对 1 卡片消息（tenant 身份，M8 推送推给本人）。
    需要 im:message + 机器人能力（M7 已要求）；code!=0 抛 FeishuApiError。"""
    resp = await client.post(
        f"{_BASE}/im/v1/messages",
        headers={"Authorization": f"Bearer {app_token}"},
        params={"receive_id_type": "open_id"},
        json={"receive_id": open_id, "msg_type": "interactive", "content": json.dumps(card)},
    )
    data = resp.json()
    if data.get("code") != 0:
        raise FeishuApiError(data.get("msg") or f"HTTP {resp.status_code}")


# ---------- M9a：审批查询（tenant 身份；99991668 实测 user token 一律不支持，2026-08-31） ----------
# 三段视角接口契约（云上探针 check_feishu_approval.py 实测）：
#   待我审批  POST /approval/v4/tasks/search      → data.task_list   [{approval, group, instance, task}]
#   我发起的  POST /approval/v4/instances/query   → data.instance_list [{approval, group, instance}]
#   抄送我的  POST /approval/v4/instances/search_cc → data.cc_list    [{approval, group, instance}]
# 均需应用身份档 scope approval:approval.list:readonly（99991672 报错点名）；
# /approval/v4/tasks/query 与 /approval/v4/cc/search 是错路径（网关级 404）。


async def _approval_search(
    client: httpx.AsyncClient, app_token: str, path: str, list_key: str,
    open_id: str, start_ts: int, end_ts: int, payload_extra: dict | None = None,
    max_items: int = 50,
) -> list[dict]:
    """审批查询三通用的翻页封装（秒级时间窗字符串传参）。返回原始 item 列表（嵌套 approval/instance 等）。"""
    items: list[dict] = []
    page_token = ""
    while len(items) < max_items:
        payload: dict = {
            "user_id": open_id,
            "start_time": str(start_ts),
            "end_time": str(end_ts),
            "page_size": 50,
        }
        if payload_extra:
            payload.update(payload_extra)
        if page_token:
            payload["page_token"] = page_token
        data = await _post(client, app_token, path, payload, params={"user_id_type": "open_id"})
        items.extend(x for x in (data.get(list_key) or []) if isinstance(x, dict))
        page_token = data.get("page_token") or ""
        if not data.get("has_more") or not page_token:
            break
    return items[:max_items]


async def approval_pending_tasks(
    client: httpx.AsyncClient, app_token: str, open_id: str, days: float = 90, max_items: int = 50,
) -> list[dict]:
    """待我审批（PENDING 任务）。时间窗只是检索范围，结果以「当前仍待办」为准。"""
    now = int(time.time())
    return await _approval_search(
        client, app_token, "/approval/v4/tasks/search", "task_list", open_id,
        now - int(days * 86400), now, {"task_status_list": ["PENDING"]}, max_items)


async def approval_my_instances(
    client: httpx.AsyncClient, app_token: str, open_id: str, start_ts: int, end_ts: int, max_items: int = 50,
) -> list[dict]:
    """我发起的审批实例（时间窗内）。"""
    return await _approval_search(
        client, app_token, "/approval/v4/instances/query", "instance_list", open_id,
        start_ts, end_ts, None, max_items)


async def approval_cc_instances(
    client: httpx.AsyncClient, app_token: str, open_id: str, start_ts: int, end_ts: int, max_items: int = 50,
) -> list[dict]:
    """抄送我的审批实例（时间窗内）。"""
    return await _approval_search(
        client, app_token, "/approval/v4/instances/search_cc", "cc_list", open_id,
        start_ts, end_ts, None, max_items)


async def approval_instance_detail(
    client: httpx.AsyncClient, app_token: str, instance_code: str,
) -> dict:
    """审批实例详情（M9a.2）：approval_name/status/form（表单控件值 JSON 字符串）/
    task_list（审批节点进度，含 open_id + 毫秒时间戳）/timeline 等全量字段。
    2026-08-31 云上实测：详情接口要的是 approval:approval:readonly（应用身份档）——
    列表用的 approval:approval.list:readonly 不含详情（缺了报 99991672）；
    此端点不收 user token（99991668），与列表一样只能走应用身份。"""
    return await _get(client, app_token, f"/approval/v4/instances/{instance_code}",
                      params={"user_id_type": "open_id"})


# ---------- M9b：邮箱（user 身份；字段级 scope，2026-08-31 云上探针 check_feishu_mail.py 实测） ----------
# 契约要点：
#   文件夹  GET /mail/v1/user_mailboxes/me/folders
#           系统文件夹 folder_id 是字面量 INBOX/SENT/ARCHIVED/TRASH/DRAFT/SPAM，自带 unread_message_count
#   列表    GET /mail/v1/user_mailboxes/me/messages?folder_id=INBOX&page_size=N
#           items 直接是 message_id 字符串列表（不是对象），page_token 翻页，时间倒序；
#           folder_id 必须是 query 参数——放路径段是网关级 404
#           page_size 上限 20（30 会报 99992402 field validation failed，2026-09-01 云上实测）
#   详情    GET /mail/v1/user_mailboxes/me/messages/{id}?format=metadata
#           明文 subject + body_preview（免解码）+ attachments[{filename, attachment_type, id}]
#           + label_ids（UNREAD/IMPORTANT）+ internal_date 毫秒戳字符串；
#           format 合法枚举只有 full/metadata（其他报 99992402），full 只多 base64url 的 body_html/plain_text
# 边界：收发件人字段（head_from/to/cc/bcc）是敏感字段，需 scope mail:user_mailbox.message.address:read 才下发；
#       缺了 head_from/to/cc 不下发——sender/recipients 为空，调用方照常显示、不阻塞。


async def mail_folders(client: httpx.AsyncClient, token: str) -> list[dict]:
    """用户主邮箱文件夹列表。返回 [{folder_id, name, unread_count}]。"""
    data = await _get(client, token, "/mail/v1/user_mailboxes/me/folders")
    out = []
    for f in data.get("items") or []:
        if not isinstance(f, dict):
            continue
        out.append({
            "folder_id": f.get("id", ""),  # API 返回字段是 id（不是 folder_id），2026-09-01 探针实测
            "name": f.get("name", ""),
            "unread_count": int(f.get("unread_message_count") or 0),
        })
    return out


async def mail_message_ids(
    client: httpx.AsyncClient, token: str, folder_id: str = "INBOX", max_ids: int = 50,
) -> list[str]:
    """文件夹内邮件 id 列表（时间倒序；items 就是 message_id 字符串，无其他字段）。"""
    ids: list[str] = []
    page_token = ""
    while len(ids) < max_ids:
        params: dict = {"folder_id": folder_id, "page_size": min(20, max_ids)}  # page_size 上限 20，超出 99992402
        if page_token:
            params["page_token"] = page_token
        data = await _get(client, token, "/mail/v1/user_mailboxes/me/messages", params=params)
        ids.extend(x for x in (data.get("items") or []) if isinstance(x, str) and x)
        page_token = data.get("page_token") or ""
        if not data.get("has_more") or not page_token:
            break
    return ids[:max_ids]


def _mail_people(m: dict) -> tuple[str, list[str], list[str]]:
    """邮件 message → (发件人, 收件人列表, 抄送列表)。
    head_from/to/cc 是字段级敏感字段，需 scope mail:user_mailbox.message.address:read 才下发，缺了就是空。"""
    def _name(x) -> str:
        if not isinstance(x, dict):
            return ""
        return (x.get("name") or x.get("mail_address") or "").strip()

    head = m.get("head_from")
    sender = _name(head) if isinstance(head, dict) else ""
    recipients = [_name(x) for x in (m.get("to") or []) if _name(x)]
    cc = [_name(x) for x in (m.get("cc") or []) if _name(x)]
    return sender, recipients, cc


async def mail_message_meta(client: httpx.AsyncClient, token: str, message_id: str) -> dict:
    """邮件元数据（format=metadata）：subject/body_preview 明文免解码 + 附件文件名 + 标签 + 毫秒时间
    + 收发件人（head_from/to/cc，缺 address:read scope 时为空）。"""
    data = await _get(client, token, f"/mail/v1/user_mailboxes/me/messages/{message_id}",
                      params={"format": "metadata"})
    m = data.get("message") or {}
    try:
        date_ms = int(m.get("internal_date") or 0)
    except (TypeError, ValueError):
        date_ms = 0
    sender, recipients, cc = _mail_people(m)
    return {
        "message_id": m.get("message_id", message_id),
        "subject": m.get("subject", "") or "",
        "preview": m.get("body_preview", "") or "",
        "date_ms": date_ms,
        "labels": m.get("label_ids") or [],
        "attachments": [str(a.get("filename", "")) for a in (m.get("attachments") or [])
                        if isinstance(a, dict) and a.get("filename")],
        "sender": sender,
        "recipients": recipients,
        "cc": cc,
        "folder_id": m.get("folder_id", ""),  # M9b.3：搜索命中后据此判断发件箱/收件箱
    }


async def mail_search(
    client: httpx.AsyncClient, token: str, query: str, max_ids: int = 50,
) -> list[str]:
    """按关键词搜用户本人邮箱，返回命中的 message_id 列表。
    M9b.3：搜索接口是 POST /mail/v1/user_mailboxes/me/search，body {"query": ...}；
    items[].id 就是 message_id（2026-09-02 云上探针实测 code=0 items=15，无需额外 scope）。"""
    data = await _post(client, token, "/mail/v1/user_mailboxes/me/search", {"query": query})
    ids = [x.get("id") for x in (data.get("items") or []) if isinstance(x, dict) and x.get("id")]
    return ids[:max_ids]


# ---------- M10a：云盘读取生态（folder 列表 / 文件下载 / sheets / bitable） ----------

async def drive_list_folder(
    client: httpx.AsyncClient, token: str, folder_token: str, max_items: int = 100,
) -> list[dict]:
    """文件夹一级子项。返回 [{name, type, token}]，type ∈ doc/docx/sheet/bitable/folder/file/mindnote…"""
    out: list[dict] = []
    page_token = ""
    while len(out) < max_items:
        params: dict = {"folder_token": folder_token, "page_size": min(50, max_items - len(out))}
        if page_token:
            params["page_token"] = page_token
        data = await _get(client, token, "/drive/v1/files", params)
        for f in data.get("files") or []:
            if isinstance(f, dict):
                out.append({"name": f.get("name", ""), "type": f.get("type", ""), "token": f.get("token", "")})
        page_token = data.get("next_page_token") or ""
        if not data.get("has_more") or not page_token:
            break
    return out


async def drive_download_file(client: httpx.AsyncClient, token: str, file_token: str) -> tuple[bytes, str]:
    """下载云盘文件，返回 (二进制, 文件名)。裸 GET 非 JSON；JSON 响应视为错误。
    文件名从 Content-Disposition 的 filename*（RFC 5987 UTF-8）或 filename 解析。"""
    resp = await client.get(
        f"{_BASE}/drive/v1/files/{file_token}/download",
        headers={"Authorization": f"Bearer {token}"},
    )
    ctype = resp.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            raise FeishuApiError(str(resp.json().get("msg") or "下载失败"))
        except ValueError:
            raise FeishuApiError(f"HTTP {resp.status_code}")
    resp.raise_for_status()
    filename = ""
    disp = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", disp)
    if m:
        from urllib.parse import unquote
        filename = unquote(m.group(1))
    else:
        m = re.search(r'filename="?([^";]+)"?', disp)
        if m:
            filename = m.group(1)
    return resp.content, filename


async def drive_upload_file(
    client: httpx.AsyncClient, token: str, folder_token: str,
    file_name: str, content: bytes, mime: str = "",
) -> dict:
    """上传文件到云盘文件夹（upload_all 一次性上传，≤20MB）。返回 data（含 file_token）。
    M29：multipart 表单；file 段由 httpx 排在 data 各字段之后（upload_all 历史坑：file 必须在最后）。"""
    resp = await client.post(
        f"{_BASE}/drive/v1/files/upload_all",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "file_name": file_name,
            "parent_type": "explorer",
            "parent_node": folder_token,
            "size": str(len(content)),
        },
        files={"file": (file_name, content, mime or "application/octet-stream")},
    )
    data = resp.json()
    if data.get("code") != 0:
        raise FeishuApiError(data.get("msg") or f"HTTP {resp.status_code}")
    return data.get("data") or {}


async def drive_root_folder(client: httpx.AsyncClient, token: str) -> str:
    """用户「我的空间」根目录 folder_token（folder 参数缺省时作上传目标）。"""
    data = await _get(client, token, "/drive/explorer/v2/root_folder/meta")
    return data.get("token") or ""


async def drive_download_media(client: httpx.AsyncClient, token: str, file_token: str) -> bytes:
    """M32a：下载云盘媒体文件二进制（sheet 浮动图片等的 file_token）。
    需 drive:drive:readonly；飞书错误时返回 JSON，按 FeishuApiError 抛（照 download_im_resource 约定）。"""
    resp = await client.get(
        f"{_BASE}/drive/v1/medias/{file_token}/download",
        headers={"Authorization": f"Bearer {token}"},
    )
    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type:
        data = resp.json()
        raise FeishuApiError(data.get("msg") or f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        raise FeishuApiError(f"HTTP {resp.status_code}")
    return resp.content


async def sheet_meta(client: httpx.AsyncClient, token: str, ss_token: str) -> list[dict]:
    """表格的 sheet 页清单。返回 [{sheet_id, title, row_count, column_count}]。"""
    data = await _get(client, token, f"/sheets/v3/spreadsheets/{ss_token}/sheets/query")
    out = []
    for s in data.get("sheets") or []:
        if isinstance(s, dict):
            grid = s.get("grid_properties") or {}
            out.append({
                "sheet_id": s.get("sheet_id", ""),
                "title": s.get("title", ""),
                "row_count": grid.get("row_count", 0),
                "column_count": grid.get("column_count", 0),
            })
    return out


async def sheet_values(
    client: httpx.AsyncClient, token: str, ss_token: str, range_str: str,
) -> list[list]:
    """读指定范围的单元格值。range_str 形如 'sheetId!A1:T200'。返回二维数组（含 None/混合类型）。"""
    from urllib.parse import quote
    data = await _get(
        client, token,
        f"/sheets/v2/spreadsheets/{ss_token}/values/{quote(range_str, safe='!')}",
        params={"valueRenderOption": "ToString"},
    )
    vr = data.get("valueRange") or {}
    return vr.get("values") or []


async def sheet_float_images(client: httpx.AsyncClient, token: str, ss_token: str, sheet_id: str) -> list[dict]:
    """M32a：sheet 页的浮动图片清单。返回 [{file_token, range}]（range=锚点区域，可能为空）。
    需 sheets:spreadsheet:readonly。已知边界：单元格内嵌图片（非浮动）无列表 API，拿不到。"""
    data = await _get(client, token, f"/sheets/v3/spreadsheets/{ss_token}/sheets/{sheet_id}/float_images")
    out = []
    for it in data.get("items") or []:
        if not isinstance(it, dict):
            continue
        ft = it.get("float_image_token") or it.get("file_token") or ""
        if ft:
            out.append({"file_token": ft, "range": it.get("range") or ""})
    return out


async def bitable_tables(client: httpx.AsyncClient, token: str, app_token: str) -> list[dict]:
    """多维表格的数据表清单。返回 [{table_id, name}]。"""
    data = await _get(client, token, f"/bitable/v1/apps/{app_token}/tables", params={"page_size": 100})
    return [{"table_id": t.get("table_id", ""), "name": t.get("name", "")}
            for t in data.get("items") or [] if isinstance(t, dict)]


async def bitable_records(
    client: httpx.AsyncClient, token: str, app_token: str, table_id: str, max_items: int = 50,
    page_interval: float = 0.0,
) -> tuple[list[dict], int]:
    """数据表记录，page_token 翻页拉取（M34：原只取首页 50 条，「接口只能读 50 条」是误传）。
    max_items=总量预算；page_interval=页间节流秒数（大批量导出时防触发 100 次/分钟限流）。
    返回 ([{record_id, fields}], total)——total 为表里真实总条数（响应自带）。"""
    out: list[dict] = []
    total = 0
    page_token = ""
    while len(out) < max_items:
        params: dict = {"page_size": min(100, max_items - len(out))}
        if page_token:
            params["page_token"] = page_token
        data = await _get(
            client, token,
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            params=params,
        )
        total = data.get("total") or total
        items = [r for r in data.get("items") or [] if isinstance(r, dict)]
        out.extend({"record_id": r.get("record_id", ""), "fields": r.get("fields") or {}}
                   for r in items)
        if not data.get("has_more") or not items:
            break
        page_token = data.get("page_token") or ""
        if not page_token:
            break
        if page_interval > 0 and len(out) < max_items:
            await asyncio.sleep(page_interval)
    return out, total


# ---------- M10b：写入生态（docx 追加 / sheet 写值 / bitable 增改记录，全部 user token） ----------

async def docx_create(client: httpx.AsyncClient, token: str, title: str,
                      folder_token: str = "") -> dict:
    """新建 docx 文档（folder_token 指定目录，空=我的空间根目录）。返回 {document_id, title}。"""
    payload: dict = {"title": title}
    if folder_token:
        payload["folder_token"] = folder_token
    data = await _post(client, token, "/docx/v1/documents", payload)
    doc = data.get("document") or {}
    return {"document_id": doc.get("document_id", ""), "title": doc.get("title", title)}


async def sheet_create(client: httpx.AsyncClient, token: str, title: str,
                       folder_token: str = "") -> dict:
    """新建电子表格（spreadsheet，folder_token 空=我的空间根目录）。返回 {spreadsheet_token, title, url}。"""
    payload: dict = {"title": title}
    if folder_token:
        payload["folder_token"] = folder_token
    data = await _post(client, token, "/sheets/v3/spreadsheets", payload)
    ss = data.get("spreadsheet") or {}
    return {"spreadsheet_token": ss.get("spreadsheet_token", ""),
            "title": ss.get("title", title), "url": ss.get("url", "")}


async def bitable_create_app(client: httpx.AsyncClient, token: str, name: str,
                             folder_token: str = "") -> dict:
    """新建多维表格 app（自带一张默认数据表）。返回 {app_token, name, url, default_table_id}。"""
    payload: dict = {"name": name}
    if folder_token:
        payload["folder_token"] = folder_token
    data = await _post(client, token, "/bitable/v1/apps", payload)
    app = data.get("app") or {}
    return {"app_token": app.get("app_token", ""), "name": app.get("name", name),
            "url": app.get("url", ""), "default_table_id": app.get("default_table_id", "")}


async def bitable_create_table(client: httpx.AsyncClient, token: str, app_token: str,
                               name: str, fields: list[dict] | None = None) -> dict:
    """M32c：在已有多维表格 app 里新建数据表（fields 可一次性建列，格式同新增字段）。
    返回 {table_id, name}。"""
    table: dict = {"name": name}
    if fields:
        table["fields"] = fields
    data = await _post(client, token, f"/bitable/v1/apps/{app_token}/tables", {"table": table})
    return {"table_id": data.get("table_id", ""), "name": name}

async def docx_append_blocks(client: httpx.AsyncClient, token: str, doc_id: str, blocks: list[dict],
                             index: int | None = None) -> int:
    """docx 追加/插入内容块（root block_id = doc_id；index 不传=追加到末尾，传了=插入到该子块位次）。
    飞书单次数上限 50 块。blocks 为飞书原生块结构（text/heading/bullet 等，调用方负责拼装）。返回写入块数。"""
    payload: dict = {"children": blocks}
    if index is not None:
        payload["index"] = index
    await _post(client, token, f"/docx/v1/documents/{doc_id}/blocks/{doc_id}/children", payload)
    return len(blocks)


async def docx_create_descendants(client: httpx.AsyncClient, token: str, doc_id: str,
                                  children_id: list[str], descendants: list[dict],
                                  index: int | None = None) -> None:
    """M32b：docx 创建嵌套块（表格等容器块的官方入口——table 不能走 children 嵌套创建）。
    children_id=直接子块的客户端临时 id 列表；descendants=全部后代块（含临时 block_id 与 children 引用）。
    index 不传=追加到文末。挂在 root（block_id=doc_id）下。"""
    payload: dict = {"children_id": children_id, "descendants": descendants}
    if index is not None:
        payload["index"] = index
    await _post(client, token, f"/docx/v1/documents/{doc_id}/blocks/{doc_id}/descendant", payload)


async def docx_block_children(client: httpx.AsyncClient, token: str, doc_id: str,
                              block_id: str = "") -> list[dict]:
    """docx 某 block（默认根块=文档本身）的子块列表，全量翻页。返回原始 block dict 列表（按文档顺序）。"""
    block_id = block_id or doc_id
    items, page_token = [], ""
    while True:
        params: dict = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        data = await _get(client, token,
                          f"/docx/v1/documents/{doc_id}/blocks/{block_id}/children", params)
        items.extend(x for x in data.get("items") or [] if isinstance(x, dict))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token") or ""
        if not page_token:
            break
    return items


async def docx_delete_children(client: httpx.AsyncClient, token: str, doc_id: str,
                               start_index: int, end_index: int) -> None:
    """删除根块 [start_index, end_index) 区间的子块（end 不含）。"""
    await _delete(client, token,
                  f"/docx/v1/documents/{doc_id}/blocks/{doc_id}/children/batch_delete",
                  {"start_index": start_index, "end_index": end_index})


async def sheet_values_update(
    client: httpx.AsyncClient, token: str, ss_token: str, range_str: str, values: list[list],
) -> dict:
    """写指定范围的单元格值。range_str 形如 'sheetId!A1:B2'。返回响应里的 updatedRange 等信息。"""
    return await _put(client, token, f"/sheets/v2/spreadsheets/{ss_token}/values",
                      {"valueRange": {"range": range_str, "values": values}})


async def sheet_values_append(
    client: httpx.AsyncClient, token: str, ss_token: str, range_str: str, values: list[list],
) -> dict:
    """在 range 区域（形如 'sheetId!A1'）已有数据的末行之后追加行。返回响应信息。"""
    return await _post(client, token, f"/sheets/v2/spreadsheets/{ss_token}/values_append",
                       {"valueRange": {"range": range_str, "values": values}})


async def bitable_add_records(
    client: httpx.AsyncClient, token: str, app_token: str, table_id: str, records: list[dict],
) -> int:
    """批量新增记录（fields 字典列表）。返回新增条数。"""
    data = await _post(client, token,
                       f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
                       {"records": [{"fields": r} for r in records]})
    return len(data.get("records") or records)


async def bitable_fields(client: httpx.AsyncClient, token: str,
                         app_token: str, table_id: str) -> list[dict]:
    """数据表字段（列）清单。返回 [{field_id, name, type, options?}]（type 为飞书类型编号）。"""
    data = await _get(client, token, f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
                      params={"page_size": 100})
    out: list[dict] = []
    for f in data.get("items") or []:
        if not isinstance(f, dict):
            continue
        item: dict = {"field_id": f.get("field_id", ""),
                      "name": f.get("field_name", ""), "type": f.get("type", 0)}
        opts = ((f.get("property") or {}).get("options") or [])
        if opts:
            item["options"] = [o.get("name", "") for o in opts if isinstance(o, dict)]
        out.append(item)
    return out


async def bitable_add_field(client: httpx.AsyncClient, token: str,
                            app_token: str, table_id: str, field_name: str,
                            field_type: int, prop: dict | None = None) -> str:
    """新建字段（列），field_type 为飞书类型编号（1 文本/2 数字/3 单选/4 多选/5 日期…）。返回 field_id。"""
    payload: dict = {"field_name": field_name, "type": field_type}
    if prop:
        payload["property"] = prop
    data = await _post(client, token,
                       f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields", payload)
    return (data.get("field") or {}).get("field_id", "")


async def bitable_delete_field(client: httpx.AsyncClient, token: str,
                               app_token: str, table_id: str, field_id: str) -> None:
    """删除字段（列）。不可恢复；首列/主列飞书会拒绝。"""
    await _delete(client, token, f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields/{field_id}")


async def bitable_update_field(client: httpx.AsyncClient, token: str,
                               app_token: str, table_id: str, field_id: str,
                               field_name: str = "", field_type: int = 0,
                               prop: dict | None = None) -> None:
    """改字段（列名/类型/属性）。主列不能删不能移，但可改名、可在飞书允许的范围内改类型。"""
    payload: dict = {}
    if field_name:
        payload["field_name"] = field_name
    if field_type:
        payload["type"] = field_type
    if prop:
        payload["property"] = prop
    await _put(client, token, f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields/{field_id}", payload)


async def bitable_delete_records(client: httpx.AsyncClient, token: str,
                                 app_token: str, table_id: str, record_ids: list[str]) -> int:
    """批量删除记录（record_id 列表，单次 API 上限 500，调用方自行收窄）。返回删除条数。"""
    data = await _post(client, token,
                       f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_delete",
                       {"records": record_ids})
    return len(data.get("records") or record_ids)


async def bitable_update_record(
    client: httpx.AsyncClient, token: str, app_token: str, table_id: str, record_id: str, fields: dict,
) -> None:
    """按 record_id 更新记录字段。"""
    await _put(client, token,
               f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
               {"fields": fields})
