"""沙箱服务的 HTTP 客户端（M3）：agent runtime 经内网 HTTP 调沙箱，沙箱容器内不存在任何 LLM key。"""
import httpx

from app.core.config import get_settings

EXEC_CLIENT_TIMEOUT = 660  # 略大于沙箱侧默认 600s 执行上限（M3.5：120/150 → 600/660）


async def exec_code(code: str, files: list[dict] | None = None, timeout_s: int | None = None) -> dict | None:
    """调用沙箱 /exec。不可达/响应异常返回 None（工具层转友好提示）；超时单独回报（区别于服务挂了）。"""
    url = get_settings().SANDBOX_BASE_URL.rstrip("/") + "/exec"
    wait = max(EXEC_CLIENT_TIMEOUT, (timeout_s or 600) + 60)
    try:
        async with httpx.AsyncClient(timeout=wait) as client:
            resp = await client.post(url, json={"code": code, "files": files or [], "timeout_s": timeout_s})
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not isinstance(data, dict) or "ok" not in data:
                return None
            return data
    except httpx.TimeoutException:
        # 超时要和"服务挂了"区分开：容器侧还在跑（会被内部 timeout 自动清理），
        # 若回 None 模型会误以为服务不可用而盲目重试，白烧一轮又几分钟 CPU
        return {
            "ok": False,
            "error": (
                f"执行超过 {wait}s 等待上限，后端已放弃等待（容器会被自动清理，无需重试同一代码）。"
                "任务过重：请拆分步骤、用向量化操作，或先把大文件转成 parquet 再分步处理"
            ),
        }
    except (httpx.HTTPError, ValueError):
        return None
