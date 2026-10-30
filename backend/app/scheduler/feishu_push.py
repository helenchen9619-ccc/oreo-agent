"""飞书群自定义机器人 webhook 推送（M4b）。

平台→群的单向通知：定时任务跑完把结果 POST 到群机器人的 webhook。
不要公网入口、不要事件订阅——只要平台能出网。

签名算法（飞书官方）：sign = Base64(HMAC-SHA256(key=f"{timestamp}\\n{secret}", msg=空))
timestamp/sign 放请求 JSON 体内，不是 header。
"""
import base64
import hashlib
import hmac
import logging
import time

import httpx

logger = logging.getLogger(__name__)

WEBHOOK_PREFIX = "https://open.feishu.cn/open-apis/bot/v2/hook/"
MAX_PUSH_CHARS = 3000  # 飞书 text 消息够用的上限；全文永远回平台会话看
CARD_BODY_CHARS = 6000  # 单卡片正文字符预算：6000 汉字≈18KB UTF-8，加头部余量 < 30KB 卡片上限


def gen_sign(secret: str, timestamp: int) -> str:
    """飞书自定义机器人签名：timestamp\nsecret 作为 HMAC key，对空消息签名。"""
    digest = hmac.new(f"{timestamp}\n{secret}".encode("utf-8"), b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def validate_webhook(url: str) -> str | None:
    """返回 None=合法，否则错误文案。"""
    if not url.startswith(WEBHOOK_PREFIX):
        return f"Webhook 地址须以 {WEBHOOK_PREFIX} 开头（飞书群 → 设置 → 群机器人 → 自定义机器人 里复制）"
    if len(url) <= len(WEBHOOK_PREFIX):
        return "Webhook 地址不完整（缺少 hook id）"
    return None


def truncate_push(text: str, limit: int = MAX_PUSH_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(内容过长已截断，全文见平台会话)"


def split_for_cards(text: str, per_card: int | None = None) -> list[str]:
    """把长文按行贪心装箱成若干片（每片 ≤ per_card 字符）；单行自身超预算则硬切。拼接无损。"""
    if per_card is None:
        per_card = CARD_BODY_CHARS  # 运行期读模块常量（不用默认参数——定义期就绑死了，测试无法 monkeypatch）
    if not text:
        return ["(无内容)"]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        # 单行超预算：先把 current 封箱，再硬切该行
        while len(line) > per_card:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:per_card])
            line = line[per_card:]
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) > per_card:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current or not chunks:
        chunks.append(current)
    return chunks


def build_card(title: str, content: str, ok: bool = True) -> dict:
    """legacy 卡片 schema（custom bot 兼容）：蓝头=成功 / 红头=失败，正文一个 lark_md div。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue" if ok else "red"},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
    }


async def _post(webhook: str, body: dict, secret: str | None) -> str | None:
    """签名 + 发送 + 错误归一。返回 None=成功，否则错误描述（不抛异常）。"""
    if secret:
        ts = int(time.time())
        body["timestamp"] = str(ts)
        body["sign"] = gen_sign(secret, ts)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook, json=body)
        data = resp.json()
    except Exception as e:  # noqa: BLE001 网络/JSON 异常统一归为推送失败
        logger.warning("feishu push failed: %s", e)
        return f"推送请求失败：{e!s}"[:300]
    code = data.get("code", data.get("StatusCode"))  # text 回 code/msg，卡片回 StatusCode/StatusMessage
    if code != 0:
        return f"飞书返回错误 {code}: {data.get('msg') or data.get('StatusMessage')}"[:300]
    return None


async def push_feishu_text(webhook: str, text: str, secret: str | None = None) -> str | None:
    """推一条 text 消息。返回 None=成功，否则错误描述（不抛异常——推送失败不该弄挂调用方）。"""
    return await _post(webhook, {"msg_type": "text", "content": {"text": truncate_push(text)}}, secret)


async def push_feishu_card(webhook: str, title: str, content: str, secret: str | None = None, ok: bool = True) -> str | None:
    """推卡片消息：全文装进卡片，超 CARD_BODY_CHARS 自动拆多张连发（标题带 (i/N)）。

    任一失败即返回错误串；已发出的不撤回（群里看到部分，用户从 last_push_error 可知没发全）。
    """
    chunks = split_for_cards(content)
    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        t = title if total == 1 else f"{title} ({i}/{total})"
        err = await _post(webhook, {"msg_type": "interactive", "card": build_card(t, chunk, ok)}, secret)
        if err:
            return err
    return None


async def push_feishu_card_to_me(open_id: str, title: str, content: str, ok: bool = True) -> str | None:
    """M8：应用机器人 1 对 1 私聊推送（替代群 webhook——个人摘要含私聊视野，不能推群）。
    复用卡片拆片逻辑；走 im/v1/messages（tenant 身份），需 im:message + 机器人能力（M7 已要求）。
    返回 None=成功，否则错误描述（不抛异常——推送失败不该弄挂调用方）。"""
    from app.auth.feishu import app_access_token  # 延迟 import：auth↔scheduler 避免环
    from app.feishu.client import send_p2p_card

    chunks = split_for_cards(content)
    total = len(chunks)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            token = await app_access_token(client)
            for i, chunk in enumerate(chunks, 1):
                t = title if total == 1 else f"{title} ({i}/{total})"
                await send_p2p_card(client, token, open_id, build_card(t, chunk, ok))
    except Exception as e:  # noqa: BLE001 网络/飞书错误统一归为推送失败
        logger.warning("feishu p2p push failed: %s", e)
        return f"私聊推送失败：{e!s}"[:300]
    return None
