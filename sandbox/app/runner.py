"""Docker 容器执行逻辑（M3 沙箱核心）。

安全约束（不可违反）：
- 容器无外网（network_mode="none"）、read_only 根文件系统、/work 为 tmpfs
- 资源限额：2C / 2G（禁 swap，超限直接 OOM 杀，绝不允许换页拖垮宿主机）/ 默认 600s / pids 256
- 日志上限 10MB（json-file max-size）：print 炸弹不能撑爆 Docker VM 磁盘
- 不用 bind mount：代码与输入文件 put_archive 进容器，产物 get_archive 取出——容器完全不碰宿主机文件系统
- 容器内不存在任何 LLM key（environment={}）
- 产物约定：/work 下的新文件与被改写的输入文件都会按相对路径回收（output/ 为约定目录，
  散落在项目文件夹旁的产物同样回收——模型有项目文件夹时习惯直接写进去，不让产物静默丢失）

M3.1.2 事故教训：模型手写纯 Python parquet 解码器（镜像没装 pyarrow），逐字节循环跑了几小时，
内存顶到 2G 后开始用 2G swap 疯狂换页，把 Windows 宿主机磁盘打到 100% 整机卡死重启。
由此加四道保险：禁 swap、容器内 GNU timeout 自杀（沙箱服务死了容器也会自己死）、
日志轮转上限、镜像预装 pyarrow（让模型永远不需要手写解码器）。
"""
import base64
import io
import os
import tarfile
import time

import anyio.to_thread
import docker
from docker.errors import DockerException, ImageNotFound
from docker.types import LogConfig, Ulimit

SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "ea-sandbox-runner:local")
EXEC_TIMEOUT_SECONDS = int(os.environ.get("EXEC_TIMEOUT_SECONDS", "600"))  # M3.5：120→600，大文件 ETL 是常态
MAX_TIMEOUT_SECONDS = 900  # 单次执行硬上限（含按请求传入的 timeout_s）
MAX_OUTPUT_BYTES = int(os.environ.get("MAX_OUTPUT_BYTES", "1048576"))  # stdout/stderr 各截断到 1MB

SANDBOX_LABEL = "ea.sandbox"  # 容器标签：服务启动时清理上一代的孤儿容器（宿主重启/服务被杀会留下）

MAX_CODE_BYTES = 64 * 1024          # 代码 ≤ 64KB
MAX_FILES = 8                        # 输入/产物各最多 8 个文件
MAX_FILE_BYTES = 100 * 1024 * 1024   # 单个输入/产物文件 ≤ 100MB（与工作区上传上限一致；内存 2G 是执行侧约束，靠 prompt 指引流式读大文件）
MAX_LOG_BYTES = 10 * 1024 * 1024     # 容器日志硬上限（daemon 侧轮转）：print 炸弹最多占 10MB 磁盘
MAX_WRITE_BYTES = 512 * 1024 * 1024  # 单文件写入上限（ulimit fsize）：防跑代码把 VM 磁盘写爆

_client = None


def _docker_client():
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def docker_ok() -> bool:
    try:
        return bool(_docker_client().ping())
    except DockerException:
        return False


def cleanup_stale_containers() -> int:
    """清理带沙箱标签的残留容器（宿主重启/服务被杀会留下上代容器，可能还在烧 CPU）。

    服务启动时调用一次。返回清理数量。"""
    n = 0
    try:
        for c in _docker_client().containers.list(all=True, filters={"label": SANDBOX_LABEL}):
            try:
                c.remove(force=True)
                n += 1
            except DockerException:
                pass
    except DockerException:
        pass
    return n


def _error(msg: str, started: float) -> dict:
    return {
        "ok": False,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "duration_ms": int((time.monotonic() - started) * 1000),
        "outputs": [],
        "error": msg,
    }


def _make_tar(files: dict[str, bytes]) -> bytes:
    """内存构造 tar：{容器内相对路径: 内容}。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path, data in files.items():
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _ChunksReader(io.RawIOBase):
    """把 docker get_archive 的 chunk 迭代器包成流式 fileobj（tarfile r| 模式用），
    避免把整个 /work（可能含 100MB 输入文件）一次性读进内存。"""

    def __init__(self, chunks):
        self._it = iter(chunks)
        self._buf = bytearray()

    def readable(self):
        return True

    def readinto(self, b):
        while len(self._buf) < len(b):
            try:
                self._buf.extend(next(self._it))
            except StopIteration:
                break
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        del self._buf[:n]
        return n


def _is_stray_output(rel: str, size: int, input_sizes: dict[str, int]) -> bool:
    """/work 根下（output/ 约定目录之外）的成员是否算产物：
    代码新写的文件，或被改写的输入文件（大小变了）。跳过 main.py、__pycache__、未动过的输入。"""
    if not rel or rel == "main.py" or rel.startswith("output/"):
        return False
    if "__pycache__" in rel.split("/") or rel.endswith(".pyc"):
        return False
    if rel in input_sizes and input_sizes[rel] == size:
        return False  # 未动过的输入文件
    return True


def _read_outputs(container, input_sizes: dict[str, int]) -> list[dict]:
    """取回产物：/work/output/ 约定目录优先；/work 其他位置的新文件/被改写的输入文件也回收。
    （M20.1：模型有项目文件夹时习惯把产物直接写进该文件夹——如 ./AI日报/x.docx——
    此前只收 output/ 导致"执行成功但产物静默丢失"；散落产物按相对路径落位，与 output/ 下同路径等效）"""
    outputs: list[dict] = []
    seen: set[str] = set()

    def _take(tar, member, rel: str) -> None:
        if len(outputs) >= MAX_FILES or not rel or rel in seen:
            return
        f = tar.extractfile(member)
        data = f.read(MAX_FILE_BYTES + 1) if f else b""
        if len(data) > MAX_FILE_BYTES:
            return  # 超限产物丢弃（工具层会在产物清单里看不到它）
        seen.add(rel)
        outputs.append({"path": rel, "content_b64": base64.b64encode(data).decode(), "size": len(data)})

    # ① /work/output/ 约定目录
    try:
        stream, _stat = container.get_archive("/work/output")
    except DockerException:
        stream = None  # 没有 output 目录
    if stream is not None:
        raw = b"".join(stream)
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                # member.name 形如 output/xx.png
                _take(tar, member, member.name.split("/", 1)[-1])

    # ② 散落在 /work 其他位置的产物（流式读 tar，未改动的大输入文件只跳读不进内存）
    try:
        stream, _stat = container.get_archive("/work")
    except DockerException:
        return outputs
    with tarfile.open(fileobj=io.BufferedReader(_ChunksReader(stream), buffer_size=65536), mode="r|") as tar:
        for member in tar:
            if not member.isfile():
                continue
            # member.name 形如 work/xx.png
            rel = member.name.split("/", 1)[-1]
            if _is_stray_output(rel, member.size, input_sizes):
                _take(tar, member, rel)
    return outputs


def _execute_sync(code: str, files: list[dict], timeout_s: int) -> dict:
    started = time.monotonic()
    code_bytes = code.encode("utf-8")
    if len(code_bytes) > MAX_CODE_BYTES:
        return _error(f"代码超过 64KB 上限（{len(code_bytes)} 字节）", started)
    if len(files) > MAX_FILES:
        return _error(f"输入文件超过 {MAX_FILES} 个上限", started)

    try:
        client = _docker_client()
        client.ping()
    except DockerException as e:
        return _error(f"Docker 守护进程不可达：{e}", started)

    try:
        client.images.get(SANDBOX_IMAGE)
    except ImageNotFound:
        return _error(f"执行镜像 {SANDBOX_IMAGE} 不存在，请先在宿主机构建", started)
    except DockerException as e:
        return _error(f"Docker 错误：{e}", started)

    payload: dict[str, bytes] = {"main.py": code_bytes}
    input_sizes: dict[str, int] = {}
    for f in files:
        path = (f.get("path") or "").lstrip("/")
        if not path or ".." in path.split("/"):
            return _error(f"输入文件路径不合法：{f.get('path')}", started)
        try:
            data = base64.b64decode(f.get("content_b64") or "")
        except Exception:
            return _error(f"输入文件 {path} 的 base64 解码失败", started)
        if len(data) > MAX_FILE_BYTES:
            return _error(f"输入文件 {path} 超过 100MB 上限", started)
        payload[path] = data
        input_sizes[path] = len(data)

    container = None
    try:
        # create（不启动）→ put_archive 注入代码与输入文件 → start
        container = client.containers.create(
            SANDBOX_IMAGE,
            # 容器内 GNU timeout 自杀（比外层 wait 宽限 15s）：沙箱服务即使被杀/宿主机卡死重启，
            # 容器也会自己终结，绝不留着烧 CPU 的孤儿（M3.1.2 事故教训）
            command=["timeout", "-s", "KILL", str(timeout_s + 15), "python", "/work/main.py"],

            network_mode="none",
            mem_limit="2g",
            memswap_limit="2g",  # 等于 mem_limit = 禁 swap：内存超限直接 OOM 杀，绝不允许换页把宿主机磁盘打到 100%
            nano_cpus=2_000_000_000,
            pids_limit=256,
            ulimits=[Ulimit(name="fsize", soft=MAX_WRITE_BYTES, hard=MAX_WRITE_BYTES)],  # 单文件 ≤512MB
            log_config=LogConfig(type="json-file", config={"max-size": str(MAX_LOG_BYTES), "max-file": "1"}),
            labels={SANDBOX_LABEL: "1"},
            # 注意一：不开 read_only——Docker 对 read-only 容器连 tmpfs 路径也拒绝 put_archive。
            # 注意二：不用 tmpfs /work——tmpfs 在容器启动时才挂载，会遮住 put_archive 预置的文件。
            # 隔离由 无外网 + 无 bind mount + 资源限额 + 容器用完即销毁（overlay 层随容器删除）保证。
            working_dir="/work",
            environment={},
        )
        container.put_archive("/work", _make_tar(payload))
        container.start()

        timed_out = False
        try:
            result = container.wait(timeout=timeout_s)
            exit_code = result.get("StatusCode")
        except Exception:
            timed_out = True
            try:
                container.kill()
            except DockerException:
                pass
            exit_code = None

        # stdout/stderr 分离收取，各自截断（daemon 侧日志已轮转限 10MB，这里再截到 1MB）
        stdout = container.logs(stdout=True, stderr=False)[-MAX_OUTPUT_BYTES:].decode("utf-8", errors="replace")
        stderr = container.logs(stdout=False, stderr=True)[-MAX_OUTPUT_BYTES:].decode("utf-8", errors="replace")
        outputs = [] if timed_out else _read_outputs(container, input_sizes)

        resp = {
            "ok": not timed_out and exit_code == 0,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "outputs": outputs,
        }
        if timed_out:
            resp["error"] = f"执行超过 {timeout_s}s 上限，已强制终止"
        elif exit_code == 137:
            # SIGKILL：内存超限 OOM，或容器内看门狗 timeout 触发
            resp["error"] = "进程被强制终止（内存超过 2G 上限被 OOM 终止，或看门狗超时）"
        return resp
    except DockerException as e:
        return _error(f"容器执行失败：{e}", started)
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except DockerException:
                pass


async def execute(code: str, files: list[dict] | None = None, timeout_s: int | None = None) -> dict:
    """异步入口：docker SDK 是阻塞调用，丢到线程池。"""
    t = min(timeout_s or EXEC_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS)
    return await anyio.to_thread.run_sync(_execute_sync, code, files or [], t)
