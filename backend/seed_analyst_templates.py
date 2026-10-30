# 数据分析师模板 seed：把 templates/analysts/ 灌入系统（幂等，可反复运行）。
# 用法：python seed_analyst_templates.py   （SEED_BASE 环境变量可覆盖后端地址）
# 流程：6 个平台技能 upsert → 4 个部门模板 upsert（SOUL=base+部门补段）→ 绑技能 → 补全员领用授权。
import os
import sys
from pathlib import Path

import httpx

BASE = os.environ.get("SEED_BASE", "http://127.0.0.1:8000")
ROOT = Path(__file__).resolve().parent.parent / "templates" / "analysts"

SKILLS = [
    ("excel-ops", "Excel 操作纪律：公式不写死值、副本试跑、重算校验、openpyxl 片段库", "数据分析"),
    ("analysis-workflow", "五步分析法：澄清口径→数据质检→探索下钻→可视化→管理层报告", "数据分析"),
    ("metrics-supply-chain", "供应链指标口径：库存周转率/缺货率/OTIF/滞销SKU/安全库存", "数据分析"),
    ("metrics-operations", "运营指标口径：RFM 分层/漏斗/复购留存/GMV 拆解", "数据分析"),
    ("metrics-product", "产品指标口径：北极星指标体系/A-B 检验/功能渗透率/异动归因", "数据分析"),
    ("metrics-ops-optimization", "营运优化口径：新品占比锚定/BOM 展开/规格换算/强配单生成", "数据分析"),
    ("metrics-finance", "财务指标口径：偿债/营运/盈利/现金流/预实对比/费用/供应链财务", "数据分析"),
]

TEMPLATES = [
    {
        "name": "供应链数据分析师",
        "dept_file": "supply-chain.md",
        "scenario_skill": "metrics-supply-chain",
        "description": "面向供应链团队的数据分析专家：库存周转、缺货、准时交付、滞销 SKU、安全库存。口径先行、结论可复现。",
    },
    {
        "name": "运营数据分析师",
        "dept_file": "operations.md",
        "scenario_skill": "metrics-operations",
        "description": "面向运营团队的数据分析专家：RFM 分层、漏斗、复购留存、GMV 拆解。口径先行、结论可复现。",
    },
    {
        "name": "产品数据分析师",
        "dept_file": "product.md",
        "scenario_skill": "metrics-product",
        "description": "面向产品中台的数据分析专家：北极星指标体系、A/B 检验、功能渗透率、异动归因。口径先行、结论可复现。",
    },
    {
        "name": "营运优化数据分析师",
        "dept_file": "ops-optimization.md",
        "scenario_skill": "metrics-ops-optimization",
        "description": "面向营运优化团队的数据分析专家：新品销量占比 × 研发 BOM × 物品规格推算门店原物料采购量、生成强配单。口径先行、过程可复算。",
    },
    {
        "name": "财务数据分析师",
        "dept_file": "finance.md",
        "scenario_skill": "metrics-finance",
        "description": "面向财务团队的数据分析专家：偿债/营运/盈利能力、现金流、预实对比、费用分析、供应链财务（采购价差量差/库存资金占用/账期）。口径法定、结论可复核。",
        "allow_web_search": True,  # 财税政策时效性强，需联网核实当前有效规定
    },
]


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


def upsert_template(admin: dict, spec: dict, soul: str, agents_md: str) -> int:
    existing = httpx.get(f"{BASE}/api/admin/agents", params={"view": "templates"}, headers=admin, timeout=15).json()
    hit = next((a for a in existing if a["name"] == spec["name"]), None)
    payload = {
        "name": spec["name"],
        "description": spec["description"],
        "allow_code_exec": True,
        "allow_web_search": bool(spec.get("allow_web_search", False)),
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


def bind_skills(admin: dict, agent_id: int, skill_ids: list[int]) -> None:
    r = httpx.put(f"{BASE}/api/admin/agents/{agent_id}/skills", headers=admin, timeout=15, json={"skill_ids": skill_ids})
    r.raise_for_status()


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

    print("== 部门模板 ==")
    base_soul = read(ROOT / "base" / "SOUL.md")
    agents_md = read(ROOT / "base" / "AGENTS.md")
    for spec in TEMPLATES:
        soul = base_soul + read(ROOT / "departments" / spec["dept_file"])
        aid = upsert_template(admin, spec, soul, agents_md)
        ids = [skill_ids["excel-ops"], skill_ids["analysis-workflow"], skill_ids[spec["scenario_skill"]]]
        bind_skills(admin, aid, ids)
        ensure_grant_all(admin, aid)

    print("完成。技能 7 个、模板 5 个均已灌入/更新。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
