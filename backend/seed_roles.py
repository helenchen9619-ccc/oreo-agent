# 通用岗位模板 seed：把 templates/roles/ 灌入系统（幂等，可反复运行）。
# 用法：python seed_roles.py   （SEED_BASE 覆盖后端地址；SEED_TOKEN 传 admin token，
# 云上 dev-login 关闭时用——从浏览器 F12 复制 localStorage 的 ea_token）
# 流程：3 个岗位技能 upsert → 3 个岗位模板 upsert（SOUL=base+岗位补段）→ 绑技能 → 补全员领用授权。
# 底座技能（excel-ops/analysis-workflow/doc-report/ppt-make）与经营参谋的 research 技能
# 复用已有平台技能——按名字查，找不到会报错提示先跑对应 seed。
import os
import sys
from pathlib import Path

import httpx

BASE = os.environ.get("SEED_BASE", "http://127.0.0.1:8000")
ROOT = Path(__file__).resolve().parent.parent / "templates" / "roles"

# 本族自有的岗位技能（随本 seed 创建/更新）
SKILLS = [
    ("sales-routine", "销售日常：跟进纪要结构化（下一步是灵魂）+ 销售周报骨架 + 方案报价文档结构", "岗位技能"),
    ("site-selection", "门店开发：选址评分卡（6 维度）+ 点位对比表 + 商务谈判要点清单 + 拓展 pipeline 周报", "岗位技能"),
    ("exec-briefing", "经营参谋：一页纸经营简报（指标→异动归因→风险→决策建议）+ 飞书多源汇总 + 战略专题", "岗位技能"),
]

# 底座技能：所有岗位模板都绑（须已存在，由 seed_analyst_templates / seed_office_docs 灌入）
BASE_SKILL_NAMES = ["excel-ops", "analysis-workflow", "doc-report", "ppt-make"]

TEMPLATES = [
    {
        "name": "销售助手",
        "role_file": "sales.md",
        "extra_skills": ["sales-routine"],
        "description": "销售业务搭档：客户跟进纪要整理、销售周报、方案报价文档、客户与回款数据分析。少写字，多跑客户。",
        "allow_web_search": True,  # 客户背景调研（公开信息）
    },
    {
        "name": "门店开发助手",
        "role_file": "site-dev.md",
        "extra_skills": ["site-selection"],
        "description": "门店拓展参谋：选址评分卡、点位对比表、商务条件谈判清单、拓展进度周报。数据驱动选址，谈判带着弹药上桌。",
        "allow_web_search": True,  # 商圈/竞品/规划公开信息
    },
    {
        "name": "经营参谋",
        "role_file": "exec.md",
        "extra_skills": ["exec-briefing", "deep-research", "industry-report", "competitor-analysis"],
        "description": "管理层经营参谋：一页纸经营简报、飞书动态汇总（日报/审批/邮件）、行业与竞品战略专题。给判断和选项，不给信息流水账。",
        "allow_web_search": True,
        "allow_feishu": True,  # 老板要的内部动态汇总（per-user token 授权）
    },
]


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


def find_platform_skills(admin: dict, names: list[str]) -> dict[str, int]:
    """按名字查已有平台技能 id（底座/研究技能不重建，复用）。"""
    existing = _get_list(admin, f"{BASE}/api/admin/skills")
    out = {}
    missing = []
    for name in names:
        hit = next((s for s in existing if s["name"] == name and s["owner_user_id"] is None), None)
        if hit is None:
            missing.append(name)
        else:
            out[name] = hit["id"]
    if missing:
        raise SystemExit(
            f"缺少平台技能：{missing}。请先运行 seed_analyst_templates.py / "
            f"seed_office_docs.py / seed_market_insight.py 灌入底座技能。"
        )
    return out


def upsert_template(admin: dict, spec: dict, soul: str, agents_md: str) -> int:
    existing = _get_list(admin, f"{BASE}/api/admin/agents?view=templates")
    hit = next((a for a in existing if a["name"] == spec["name"]), None)
    payload = {
        "name": spec["name"],
        "description": spec["description"],
        "allow_code_exec": True,
        "allow_web_search": bool(spec.get("allow_web_search", False)),
        "allow_feishu": bool(spec.get("allow_feishu", False)),
    }
    if hit is None:
        r = httpx.post(f"{BASE}/api/admin/agents", headers=admin, timeout=15, json=payload)
        r.raise_for_status()
        aid = r.json()["id"]
        print(f"  模板 {spec['name']}: 新建 #{aid}")
    else:
        aid = hit["id"]
        r = httpx.patch(f"{BASE}/api/admin/agents/{aid}", headers=admin, timeout=15, json=payload)
        r.raise_for_status()
        print(f"  模板 {spec['name']}: 更新 #{aid}")
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

    print("== 岗位技能 ==")
    skill_ids = {name: upsert_skill(admin, name, desc, cat) for name, desc, cat in SKILLS}

    print("== 底座/研究技能（复用已有） ==")
    extra_names = sorted({n for t in TEMPLATES for n in t["extra_skills"] if n not in skill_ids})
    skill_ids.update(find_platform_skills(admin, BASE_SKILL_NAMES + extra_names))
    print(f"  复用 {len(BASE_SKILL_NAMES) + len(extra_names)} 个已有平台技能")

    print("== 岗位模板 ==")
    base_soul = read(ROOT / "base" / "SOUL.md")
    agents_md = read(ROOT / "base" / "AGENTS.md")
    for spec in TEMPLATES:
        soul = base_soul + read(ROOT / "roles" / spec["role_file"])
        aid = upsert_template(admin, spec, soul, agents_md)
        ids = [skill_ids[n] for n in BASE_SKILL_NAMES + spec["extra_skills"]]
        r = httpx.put(f"{BASE}/api/admin/agents/{aid}/skills", headers=admin, timeout=15, json={"skill_ids": ids})
        r.raise_for_status()
        ensure_grant_all(admin, aid)

    print("完成。岗位技能 4 个、岗位模板 3 个（销售助手/门店开发助手/经营参谋）均已灌入/更新。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
