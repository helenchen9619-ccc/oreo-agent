import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

# M31 事故修复：企微 SDK 必须抢在 SQLAlchemy 之前 dlopen（2026-09-23 探针实锤，详见模块 docstring）。
# 本行必须是整个文件的第一个 app 侧 import，不得下移。
from app.wecom_archive import early_load as _wecom_early_load  # noqa: F401

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings

from app.agents.router import admin_router as agents_admin_router
from app.agents.router import my_router as my_agents_router
from app.agents.router import router as agents_router
from app.announcements.router import router as announcements_router
from app.apihosts.router import router as apihosts_router
from app.auth.org_router import router as org_router
from app.auth.roles_router import router as roles_router
from app.auth.router import router as auth_router
from app.chat.asr import router as asr_router
from app.chat.router import router as chat_router
from app.gateway.router import router as gateway_router
from app.ops.router import router as ops_router
from app.scheduler.engine import (
    scheduler_loop,
    sweep_feishu_images_on_startup,
    sweep_stale_running,
    sweep_wecom_messages_on_startup,
    wecom_archive_sync_on_startup,
)
from app.scheduler.router import router as scheduler_router
from app.skills.router import admin_router as skills_admin_router
from app.skills.router import router as skills_router
from app.stats.router import router as stats_router
from app.system.router import router as system_router
from app.wecom_archive.router import router as wecom_archive_router
from app.workspace.router import router as workspace_router
from app.workspace.shares import owner_router as ws_shares_owner_router
from app.workspace.shares import router as ws_shared_router


logger = logging.getLogger("oreo.startup")


def _warn_if_data_unmounted() -> None:
    """2026-08-26 / 08-31 两次云上事故防复发：data 目录（workspaces/tmp_uploads/traces）
    必须挂在宿主机卷上；没挂载则写在容器层，下次重建容器即全部蒸发，且症状完全静默。
    挂载丢失 = 云上 compose 漂移（丢了 ./backend/data:/app/data），启动时响亮报警。
    本地开发（非 Linux 容器，读不到 /proc/mounts）不检查。
    注意不能用 os.path.ismount：bind mount 与 /app 同设备不同 inode 时它会误判 False，
    直接读 /proc/mounts 的挂载点列才可靠（与运维验证命令 grep /app/data /proc/mounts 同源）。"""
    data_dir = str(Path(getattr(get_settings(), "DATA_DIR", "data")).resolve())
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            mounted = any(line.split()[1] == data_dir for line in f if len(line.split()) > 1)
    except OSError:
        return
    if not mounted:
        logger.warning(
            "DATA VOLUME NOT MOUNTED: %s 正写在容器层，重建容器数据即蒸发！"
            "立即核对云上 docker-compose.prod.yml 是否丢失 `- ./backend/data:/app/data`"
            "（2026-08-26 / 08-31 事故），修复前不要再做任何 rebuild",
            data_dir,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 定时任务调度器（M3.8）：单 worker 内存态（同 chat._ACTIVE_TURNS 假设）。
    # 执行中的任务不主动取消——uvicorn 退出统一 cancel，engine 的 CancelledError 兜底会补齐提交。
    _warn_if_data_unmounted()
    await sweep_stale_running()
    await sweep_feishu_images_on_startup()
    await wecom_archive_sync_on_startup()  # M31：未配置企微存档时 no-op
    await sweep_wecom_messages_on_startup()  # M31.2：保留策略清扫（默认永久 no-op）
    loop_task = asyncio.create_task(scheduler_loop())
    yield
    loop_task.cancel()


app = FastAPI(title="OREO", version="0.2.6", lifespan=lifespan)

# 一期前后端同域部署，CORS 仅方便本地开发
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(asr_router)
app.include_router(gateway_router)
app.include_router(apihosts_router)
app.include_router(agents_router)
app.include_router(my_agents_router)
app.include_router(agents_admin_router)
app.include_router(skills_router)
app.include_router(skills_admin_router)
# shares 必须先于 workspace_router 注册：否则 GET /workspace/shares 会先撞上 workspace 的 /{file_id} 路由（file_id="shares" 转 int 422）
app.include_router(ws_shares_owner_router)
app.include_router(ws_shared_router)
app.include_router(workspace_router)
app.include_router(scheduler_router)
app.include_router(roles_router)
app.include_router(org_router)
app.include_router(stats_router)
app.include_router(ops_router)
app.include_router(announcements_router)
app.include_router(system_router)
app.include_router(wecom_archive_router)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}
