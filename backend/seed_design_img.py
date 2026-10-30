# 视觉设计助手模板 seed：把 templates/design-img/ 灌入系统（幂等，可反复运行）。
# 用法：python seed_design_img.py   （SEED_BASE 覆盖后端地址；SEED_TOKEN 传 admin token，
# 云上 dev-login 关闭时用——从浏览器 F12 复制 localStorage 的 ea_token）
# 流程：4 个平台技能 upsert → 「视觉设计助手」模板 upsert → 绑技能 → 补全员领用授权。
# 能力位：allow_image_generate=on（本模板的核心）、code_exec=off、web_search=off。
import os
import sys
from pathlib import Path

import httpx

BASE = os.environ.get("SEED_BASE", "http://127.0.0.1:8000")
ROOT = Path(__file__).resolve().parent.parent / "templates" / "design-img"

SKILLS = [
    ("poster-design", "海报/宣传物料：尺寸选择 + 中文文字渲染纪律（主标题≤10字）+ 品牌元素参考图 + prompt 结构模板", "视觉设计"),
    ("product-shot", "产品图/电商图：产品一致性=必须传参考图；白底图/场景图/多角度变体/风格迁移", "视觉设计"),
    ("social-cover", "公众号/社媒配图：渠道尺寸对照 + 一稿多尺寸变体流程 + 头图设计要点", "视觉设计"),
    ("ppt-illustration", "PPT/文档插图：扁平插画默认风、配色跟随 PPT 主题、不画文字、全套风格统一", "视觉设计"),
]

TEMPLATE = {
    "name": "视觉设计助手",
    "description": "视觉设计助手：海报、产品图、公众号配图、PPT 插图。先确认用途与风格，支持参考图保持品牌/产品一致性，一稿多尺寸。",
    "skill_names": [n for n, _, _ in SKILLS],
}


def admin_headers() -> dict:
    token = os.environ.get("SEED_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token.strip()}"}
    r = httpx.post(f"{BASE}/api/auth/dev-login", json={"username": "admin"}, timeout=15)
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['token']}"}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def skill_files(skill_dir: Path) -> list[dict]:
    """读取技能文件夹全部文本文件，转成 [{path, content}]（path 为相对技能根目录）。"""
    files = []
    for p in sorted(skill_dir.rglob("*")):
        if p.is_file():
            files.append({"path": p.relative_to(skill_dir).as_posix(), "content": read(p)})
    assert any(f["path"] == "SKILL.md" for f in files), f"{skill_dir} 缺少 SKILL.md"
    return files


def _get_list(admin: dict, url: str) -> list:
    """GET 期望返回列表的接口；返回非列表（多为 401 的 {"detail": ...}）时给出人话提示并退出。"""
    r = httpx.get(url, headers=admin, timeout=15)
    data = r.json()
    if r.status_code != 200 or not isinstance(data, list):
        raise SystemExit(
            f"接口认证失败或返回异常（HTTP {r.status_code}）：{url} → {str(data)[:200]}\n"
            "SEED_TOKEN 应是 OREO 平台的登录 token（浏览器登录 oreo 后 F12 → 应用 → 本地存储 → ea_token），"
            "不是火山方舟等外部服务的 API key。"
        )
    return data


def upsert_skill(admin: dict, name: str, description: str, category: str) -> int:
    folder = ROOT / "skills" / name
    files = skill_files(folder)
    instructions = next(f["content"] for f in files if f["path"] == "SKILL.md")
    existing = _get_list(admin, f"{BASE}/api/admin/skills")
    hit = next((s for s in existing if s["name"] == name and s["owner_user_id"] is None), None)
    if hit is None:
        r = httpx.post(
            f"{BASE}/api/admin/skills", headers=admin, timeout=15,
            json={"name": name, "description": description, "category": category, "instructions": instructions},
        )
        r.raise_for_status()
        sid = r.json()["id"]
        print(f"  技能 {name}: 新建 #{sid}")
    else:
        sid = hit["id"]
        r = httpx.patch(
            f"{BASE}/api/admin/skills/{sid}", headers=admin, timeout=15,
            json={"name": name, "description": description, "category": category, "instructions": instructions},
        )
        r.raise_for_status()
        print(f"  技能 {name}: 更新 #{sid}")
    r = httpx.put(f"{BASE}/api/admin/skills/{sid}/files", headers=admin, timeout=15, json={"files": files})
    r.raise_for_status()
    return sid


def upsert_template(admin: dict, soul: str, agents_md: str) -> int:
    existing = _get_list(admin, f"{BASE}/api/admin/agents?view=templates")
    hit = next((a for a in existing if a["name"] == TEMPLATE["name"]), None)
    payload = {
        "name": TEMPLATE["name"],
        "description": TEMPLATE["description"],
        "allow_code_exec": False,
        "allow_web_search": False,
        "allow_image_generate": True,
    }
    if hit is None:
        r = httpx.post(f"{BASE}/api/admin/agents", headers=admin, timeout=15, json=payload)
        r.raise_for_status()
        aid = r.json()["id"]
        print(f"  模板 {TEMPLATE['name']}: 新建 #{aid}")
    else:
        aid = hit["id"]
        r = httpx.patch(f"{BASE}/api/admin/agents/{aid}", headers=admin, timeout=15, json=payload)
        r.raise_for_status()
        print(f"  模板 {TEMPLATE['name']}: 更新 #{aid}")
    for key, content in (("SOUL", soul), ("AGENTS", agents_md)):
        r = httpx.put(f"{BASE}/api/admin/agents/{aid}/files/{key}", headers=admin, timeout=15, json={"content": content})
        r.raise_for_status()
    return aid


def ensure_grant_all(admin: dict, agent_id: int) -> None:
    grants = httpx.get(f"{BASE}/api/admin/agents/{agent_id}/claim-grants", headers=admin, timeout=15).json()
    if any(g["grantee_type"] == "all" for g in grants):
        return
    r = httpx.post(
        f"{BASE}/api/admin/agents/{agent_id}/claim-grants", headers=admin, timeout=15,
        json={"grantee_type": "all", "grantee_id": None},
    )
    r.raise_for_status()
    print(f"  模板 #{agent_id}: 补全员领用授权")


def main() -> int:
    if not ROOT.is_dir():
        print(f"找不到内容目录: {ROOT}")
        return 1
    admin = admin_headers()

    print("== 平台技能 ==")
    skill_ids = {name: upsert_skill(admin, name, desc, cat) for name, desc, cat in SKILLS}

    print("== 模板 ==")
    aid = upsert_template(admin, read(ROOT / "SOUL.md"), read(ROOT / "AGENTS.md"))
    ids = [skill_ids[n] for n in TEMPLATE["skill_names"]]
    r = httpx.put(f"{BASE}/api/admin/agents/{aid}/skills", headers=admin, timeout=15, json={"skill_ids": ids})
    r.raise_for_status()
    ensure_grant_all(admin, aid)

    print("完成。技能 4 个、模板 1 个（视觉设计助手，image_generate=on/code_exec=off/web_search=off）均已灌入/更新。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
