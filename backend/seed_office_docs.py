# 办公文书专家模板 seed：把 templates/office-docs/ 灌入系统（幂等，可反复运行）。
# 用法：python seed_office_docs.py   （SEED_BASE 环境变量可覆盖后端地址）
# 流程：2 个平台技能 upsert → 「办公文书专家」模板 upsert → 绑技能 → 补全员领用授权。
# 能力位：allow_code_exec=on（python-docx/pptx 生成文件）、allow_web_search=off（文书不需要联网）。
import os
import sys
from pathlib import Path

import httpx

BASE = os.environ.get("SEED_BASE", "http://127.0.0.1:8000")
ROOT = Path(__file__).resolve().parent.parent / "templates" / "office-docs"

SKILLS = [
    ("doc-report", "工作文书写作：周报/月报/总结骨架（结论先行）+ 素材转写纪律 + python-docx 生成模式", "办公文书"),
    ("ppt-make", "PPT 制作：大纲先行确认 + HTML 主题幻灯片（黑白极简/商务深蓝/深色叙事，在线预览）+ 沙箱出图 base64 内嵌 + pptx 按需导出", "办公文书"),
]

TEMPLATE = {
    "name": "办公文书专家",
    "description": "办公文书专家：工作总结、周报月报、汇报 PPT。先问清读者与用途，骨架先行，交付 .docx / HTML 幻灯片。不虚构数据。",
    "skill_names": ["doc-report", "ppt-make"],
}


def login(username: str) -> dict:
    r = httpx.post(f"{BASE}/api/auth/dev-login", json={"username": username}, timeout=15)
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


def upsert_skill(admin: dict, name: str, description: str, category: str) -> int:
    folder = ROOT / "skills" / name
    files = skill_files(folder)
    instructions = next(f["content"] for f in files if f["path"] == "SKILL.md")
    existing = httpx.get(f"{BASE}/api/admin/skills", headers=admin, timeout=15).json()
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
    existing = httpx.get(f"{BASE}/api/admin/agents", params={"view": "templates"}, headers=admin, timeout=15).json()
    hit = next((a for a in existing if a["name"] == TEMPLATE["name"]), None)
    payload = {
        "name": TEMPLATE["name"],
        "description": TEMPLATE["description"],
        "allow_code_exec": True,
        "allow_web_search": False,
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
    admin = login("admin")

    print("== 平台技能 ==")
    skill_ids = {name: upsert_skill(admin, name, desc, cat) for name, desc, cat in SKILLS}

    print("== 模板 ==")
    aid = upsert_template(admin, read(ROOT / "SOUL.md"), read(ROOT / "AGENTS.md"))
    ids = [skill_ids[n] for n in TEMPLATE["skill_names"]]
    r = httpx.put(f"{BASE}/api/admin/agents/{aid}/skills", headers=admin, timeout=15, json={"skill_ids": ids})
    r.raise_for_status()
    ensure_grant_all(admin, aid)

    print("完成。技能 2 个、模板 1 个（办公文书专家，code_exec=on/web_search=off）均已灌入/更新。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
