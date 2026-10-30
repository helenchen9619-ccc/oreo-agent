"""工作区相对路径校验（M21）：单一实现，tools.py / workspace/router.py / shares.py 共用。

历史是白名单正则 [\\w.\\-]+，把 ERP 导出的「物品导出清单-总部 (16).xlsx」
（空格/半角括号）挡在 run_code 门外；而上传入口原本不校验文件名，
文件进得来却用不了，坏在半路。改为按段黑名单校验：
放行空格、半角/全角括号、【】、顿号等真实世界文件名；
仍拦截 .. 穿越、空段、\\ : * ? " < > |、控制字符、尾空格/尾点（Windows 兼容）、
Windows 保留设备名（本地开发机落盘会炸）。
"""
import re

_BAD_CHARS = re.compile(r'[\\:*?"<>|\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

INVALID_HINT = '路径不合法：不能含 \\ : * ? " < > | 和控制字符，每段不能为空/纯 . 或 ..，且不能以空格/点结尾'


def valid_rel(path: str) -> bool:
    """工作区相对路径（/ 分层）是否合法。"""
    if not path or "\\" in path:
        return False
    for seg in path.split("/"):
        if not seg or seg in (".", ".."):
            return False
        if _BAD_CHARS.search(seg):
            return False
        if seg.endswith((" ", ".")):
            return False
        if seg.split(".", 1)[0].upper() in _RESERVED:
            return False
    return True


def sanitize_name(name: str) -> str:
    """把外部来源的文件名（飞书下载等）清洗成合法单段：只替换黑名单字符、
    去掉尾空格/尾点——保留空格/括号等原名可读字符（M21 放宽后不再全洗成下划线）。
    洗不干净（全非法/保留名）时返回空串，由调用方给兜底名。"""
    seg = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip().rstrip(" .")
    if not seg or seg in (".", "..") or seg.split(".", 1)[0].upper() in _RESERVED:
        return ""
    return seg
