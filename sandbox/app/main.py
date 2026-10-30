"""沙箱服务（M3）：在隔离 Docker 容器中执行 agent 生成的 Python 代码。

设计约束（不可违反）：
- 容器无外网、2C/2G（禁 swap）/默认 600s 限额、容器内 timeout 自杀、日志 10MB 上限、用完即销毁
- 容器内不存在任何 LLM key
- 输入/产物走 HTTP body（base64），容器不挂载宿主机目录
"""
import anyio.to_thread
from fastapi import FastAPI
from pydantic import BaseModel, Field

from app import runner

app = FastAPI(title="enterprise-agent-sandbox", version="0.3.1")


@app.on_event("startup")
async def _startup() -> None:
    # 清理上一代残留的沙箱容器（宿主重启/服务被杀会留下还在烧 CPU 的孤儿）
    n = await anyio.to_thread.run_sync(runner.cleanup_stale_containers)
    if n:
        print(f"[sandbox] 清理了 {n} 个残留沙箱容器")


class ExecFile(BaseModel):
    path: str = Field(max_length=256)
    content_b64: str


class ExecIn(BaseModel):
    code: str = Field(max_length=runner.MAX_CODE_BYTES + 1024)
    files: list[ExecFile] = Field(default_factory=list, max_length=runner.MAX_FILES)
    timeout_s: int | None = Field(default=None, ge=1, le=900)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "docker": runner.docker_ok(), "image": runner.SANDBOX_IMAGE}


@app.post("/exec")
async def exec_code(body: ExecIn) -> dict:
    return await runner.execute(body.code, [f.model_dump() for f in body.files], body.timeout_s)
