# 数据分析师模板（templates/analysts）

五个部门数据分析模板数字人的全部内容：**供应链 / 运营 / 产品 / 营运优化 / 财务 数据分析师**。
共享同一套底座人格与底座技能，各自叠加本部门指标场景包。

## 目录

```
base/SOUL.md            共享人格（严谨分析师：口径先行、可复现、留档）
base/AGENTS.md          共享工作约定（先读技能再动手、输出规范、红线）
departments/*.md        各部门人格补段（seed 时拼在 base/SOUL.md 之后）
skills/                 7 个平台技能（SKILL.md + references/ + scripts/）
  excel-ops/              底座① Excel 操作纪律 + openpyxl 代码片段库 + 重算校验脚本
  analysis-workflow/      底座② 五步分析法
  metrics-supply-chain/   场景包：库存周转率/缺货率/准时交付/滞销SKU/安全库存
  metrics-operations/     场景包：RFM/漏斗/复购留存/GMV 拆解
  metrics-product/        场景包：北极星指标/A-B 检验/功能渗透率
  metrics-ops-optimization/ 场景包：新品占比锚定/BOM 展开/规格换算/强配单生成
  metrics-finance/        场景包：偿债/营运/盈利/现金流/预实对比/费用/供应链财务（财务模板额外开联网查财税政策）
```

## 灌入 / 更新

```bash
cd backend
python seed_analyst_templates.py            # 幂等：可反复运行，按 name 匹配更新
SEED_BASE=http://host:8000 python seed_analyst_templates.py   # 指定后端地址
```

脚本会：创建/更新 7 个平台技能 → 创建/更新 5 个模板（SOUL=base+部门补段、AGENTS=base、
allow_code_exec=on；财务模板额外 allow_web_search=on）→ 绑定技能（底座×2 + 本部门场景包）→ 补全员领用授权。

## 修订流程

改本目录文件 → 重跑 seed 脚本 → 已领用的实例在详情页点「从模板同步」获取新版人格；
技能内容对实例实时生效（技能是绑定不是复制）。

场景包的指标口径是**业内通用定义草稿**，请业务方确认后按上述流程修订。
