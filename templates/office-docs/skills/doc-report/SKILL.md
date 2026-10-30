# 工作文书写作（doc-report）

> 工作总结 / 周报 / 月报 / 汇报文档的结构模板与 docx 生成模式。

## 1. 通用骨架（结论先行）

```
标题（含周期/部门/姓名）
一、本期重点工作与成果    ← 最重要，按重要性排序，每条 = 事项 + 量化结果
二、数据看板（可选）      ← 关键指标表格：指标 | 本期 | 上期/目标 | 环比
三、问题与风险            ← 客观陈述 + 已采取/建议的应对
四、下期计划              ← 可执行、可检查，能量化的量化
```

- **周报**：压到一页，重"进度与阻塞"；问题与计划比成果更重要。
- **月报/季度总结**：重"成果与数据"，数据看板几乎必有；问题部分写原因分析。
- **晋升/述职类**：重"能力证据"，每件事 = 背景 → 你的动作 → 可验证的结果。

## 2. 素材转写纪律

- 口语 → 书面语：删语气词，"搞了个 xx 活动" → "策划并落地 xx 活动"。
- 同类归并：5 条碎素材可能归成 2 个工作项；归并拿不准就问。
- 每条成果尽量量化；素材没数字就不造，标【待补充：xx 数据】。
- 保持事实时态：完成的写"已完成"，进行中的写"推进中（进度 xx%）"，不混淆。

## 3. docx 生成模式（python-docx，沙箱内执行）

```python
from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

doc = Document()
# 正文默认字体设置（中文必须同时设 eastasia）
style = doc.styles["Normal"]
style.font.name = "Times New Roman"
style.font.size = Pt(12)
style.element.rPr.rFonts.set(__import__("docx.oxml.ns", fromlist=["qn"]).qn("w:eastAsia"), "宋体")

title = doc.add_heading("运营部周报（2026-W33）", level=0)
title.alignment = WD_ALIGN_PARAGRAPH.CENTER

doc.add_heading("一、本期重点工作与成果", level=1)
p = doc.add_paragraph(style="List Number")
p.add_run("完成 8 月新品上市首周销量追踪：铺货门店 120 家，首周动销率 68%。")

# 表格：数据看板
table = doc.add_table(rows=1, cols=4)
table.style = "Light Grid Accent 1"
hdr = table.rows[0].cells
for i, h in enumerate(["指标", "本期", "目标", "达成率"]):
    hdr[i].text = h
for row in rows_data:
    cells = table.add_row().cells
    for i, v in enumerate(row):
        cells[i].text = str(v)

doc.save("/out/周报_运营部_2026-W33.docx")  # /out 为沙箱输出目录，按平台约定回存工作区
```

- 标题层级：add_heading(level=0) 文章题、level=1 章节题；不要全文用加粗正文冒充标题（目录/导航窗格会失效）。
- 中文字体：每个自定义 run 若设 font.name，必须同步设 eastAsia，否则中文回退默认字体。
- 表格样式用内置名（Light Grid Accent 1 等），不要手动逐格描边。

## 4. 交付前检查

- 通读一遍：有没有残留的【待补充】？有则在交付消息里逐条列出提醒用户。
- 数字与素材原文核对一遍（尤其是百分比、日期、人名）。
- 文件名带日期与主题；保存到 reports/ 子文件夹。
