"""M20.1 逻辑测试：沙箱产物回收——除 output/ 约定目录外，散落在 /work 其他位置的
新文件/被改写的输入文件也回收（模型有项目文件夹时习惯直接写进去，不让产物静默丢失）。

runner.py 在 backend 包外且模块级 import docker：用 stub 顶替 docker 模块后按路径加载。
"""
import base64
import importlib.util
import io
import sys
import tarfile
import types
from pathlib import Path

import pytest

# ---- docker 模块 stub（runner 模块级 import，但本测试不碰真 Docker） ----
_docker = types.ModuleType("docker")
_errors = types.ModuleType("docker.errors")


class DockerException(Exception):
    pass


class ImageNotFound(DockerException):
    pass


_errors.DockerException = DockerException
_errors.ImageNotFound = ImageNotFound
_dtypes = types.ModuleType("docker.types")
_dtypes.LogConfig = type("LogConfig", (), {"__init__": lambda self, **kw: None})
_dtypes.Ulimit = type("Ulimit", (), {"__init__": lambda self, **kw: None})
_docker.errors = _errors
_docker.types = _dtypes
sys.modules.setdefault("docker", _docker)
sys.modules.setdefault("docker.errors", _errors)
sys.modules.setdefault("docker.types", _dtypes)

_runner_path = Path(__file__).resolve().parents[2] / "sandbox" / "app" / "runner.py"
_spec = importlib.util.spec_from_file_location("sandbox_runner", _runner_path)
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


def _make_tar(prefix: str, files: dict[str, bytes]) -> bytes:
    """构造 get_archive 风格的 tar 字节流：成员名带顶层目录前缀（如 work/x.png）。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path, data in files.items():
            info = tarfile.TarInfo(name=f"{prefix}/{path}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _FakeContainer:
    """get_archive('/work/output') 与 get_archive('/work') 的 fake（无 output 目录时抛 DockerException）。"""

    def __init__(self, work_files: dict[str, bytes]):
        self._work = work_files

    def get_archive(self, path):
        if path == "/work/output":
            files = {k[len("output/"):]: v for k, v in self._work.items() if k.startswith("output/")}
            if not files:
                raise DockerException("no such dir")
            return iter([_make_tar("output", files)]), {}
        if path == "/work":
            return iter([_make_tar("work", self._work)]), {}
        raise DockerException("bad path")


def _read(work_files, input_sizes):
    return {o["path"]: o for o in runner._read_outputs(_FakeContainer(work_files), input_sizes)}


# ---------- _is_stray_output 纯逻辑 ----------

def test_stray_predicate():
    ins = {"AI日报/a.md": 100}
    assert runner._is_stray_output("AI日报/a.docx", 500, ins) is True      # 新文件
    assert runner._is_stray_output("AI日报/a.md", 200, ins) is True        # 输入被改写（大小变了）
    assert runner._is_stray_output("AI日报/a.md", 100, ins) is False       # 输入没动过
    assert runner._is_stray_output("main.py", 10, ins) is False            # 代码本体
    assert runner._is_stray_output("output/x.png", 10, ins) is False       # 约定目录走另一条路
    assert runner._is_stray_output("__pycache__/m.cpython-312.pyc", 10, ins) is False
    assert runner._is_stray_output("", 0, ins) is False


# ---------- _read_outputs 回收 ----------

def test_stray_file_next_to_input_collected():
    """M20.1 核心场景：模型把 docx 直接存到输入文件旁边（没经 output/）→ 也回收。"""
    work = {
        "main.py": b"code",
        "AI日报/日报.md": b"markdown source",                       # 未动过的输入
        "AI日报/日报.docx": b"PK-docx-bytes",                        # 模型新写的产物
    }
    outs = _read(work, {"AI日报/日报.md": len(b"markdown source")})
    assert set(outs) == {"AI日报/日报.docx"}
    assert base64.b64decode(outs["AI日报/日报.docx"]["content_b64"]) == b"PK-docx-bytes"


def test_output_dir_still_works_and_wins():
    """output/ 约定目录优先回收；同相对路径不重复。"""
    work = {
        "main.py": b"code",
        "output/报表/r.xlsx": b"new-version",
        "报表/r.xlsx": b"stray-version",
    }
    outs = _read(work, {})
    assert set(outs) == {"报表/r.xlsx"}
    assert base64.b64decode(outs["报表/r.xlsx"]["content_b64"]) == b"new-version"  # output/ 优先


def test_modified_input_collected_as_overwrite():
    """输入文件被原地改写（ETL 更新基线场景）→ 按原路径回收（工具层据此覆盖 workspace 原文件）。"""
    work = {"main.py": b"c", "data/基线.parquet": b"v2-bigger"}
    outs = _read(work, {"data/基线.parquet": 2})
    assert set(outs) == {"data/基线.parquet"}


def test_no_output_dir_and_clean_run_returns_empty():
    """没有 output 目录、也没动任何文件 → 空产物（不报错）。"""
    work = {"main.py": b"c", "in/a.csv": b"12345"}
    assert _read(work, {"in/a.csv": 5}) == {}


def test_stray_respects_max_files_cap():
    work = {"main.py": b"c"}
    work.update({f"f{i}.txt": b"x" for i in range(12)})
    outs = runner._read_outputs(_FakeContainer(work), {})
    assert len(outs) == runner.MAX_FILES  # 8 个上限
