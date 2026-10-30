# PPT 制作（ppt-make）

> 汇报 PPT：**默认产出 HTML 幻灯片**（套用内置主题模板，工作区里点开即真渲染预览）；
> 用户明确要"能在 WPS/Office 里改的稿子"时才用 python-pptx 导出 .pptx。

## 1. 流程纪律

1. **页数与受众先定**：问清汇报时长/页数上限（经验值：1 分钟 ≈ 1 页）。
2. **先出大纲**：每页一行——`P1 封面：标题+汇报人+日期` / `P2 核心结论：三句话` / …… 发给用户确认或修改。**大纲没确认前不生成文件**（用户明确说"直接做"除外）。
3. **选主题**：问用户用哪套（**黑白极简 minimal**：默认，正式汇报通用 / **商务深蓝 bizblue**：对外、给客户领导 / **深色叙事 darkdeck**：复盘会/战略会/经营分析会等"带压迫感的讨论型汇报"，全屏翻页、大数字冲击、提问块），用户没偏好默认黑白极简。
4. 读主题模板 → 填充内容 → `workspace_write` 存工作区 `ppt/` 子文件夹（.html）→ 回复给路径 + 每页一句话摘要，并告知"点文件名即可在线预览，方向键可翻页，浏览器打开可 Ctrl+P 导出 PDF"。

## 2. HTML 主题模板用法（主路径）

主题模板是技能附件，用 read_skill 读：

```
read_skill("ppt-make", path="themes/minimal.html")    # 黑白极简（1280×720 分页）
read_skill("ppt-make", path="themes/bizblue.html")    # 商务深蓝（1280×720 分页）
read_skill("ppt-make", path="themes/darkdeck.html")   # 深色叙事（全屏滚动翻页）
```

每套模板含多种版式页（文件内有注释标注用法；minimal/bizblue 为 cover/toc/points/table/compare/closing，darkdeck 为 cover/fact/points/think/quote/closing）。生成规则：

- **整体复制模板文件**，`<style>` 和末尾 `<script>`（缩放/翻页导航）原样保留，一个字符都不改；
- 按大纲复制/删除/重排 `<section class="slide" data-layout="...">` 页；每页 `data-layout` 属性保留；
- 只替换文字内容，不改 class、不动 HTML 结构；minimal/bizblue 每页右下角 `<span class="page">` 按实际页序改数字，darkdeck 改 `<span class="bignum">`；
- 文件名带日期与主题（英文小写下划线），如 `ppt/august_review_2026-08.html`。

## 3. 内容设计纪律

- **标题写结论句，不写栏目名**："8 月新品动销率 68%，超目标 8pt" ✅ / "销售情况" ❌。
- 每页一个观点；要点 ≤4 条、每条 ≤2 行；表格 ≤6 行 × ≤5 列。装不下就拆页，不缩字号硬塞。
- **关键页用大数字制造冲击**（darkdeck 的 fact 版式）：把最刺痛/最振奋的 1-2 个数字放大，其余信息让位。
- **复盘/讨论型汇报用提问块**（darkdeck 的 think 版式）：把"必须回答的问题"单独成页甩给全场，不要在要点里一笔带过。
- **数据页默认配图表**（见 §3.1），仅当用户明确说"不要图"才退回纯文字/表格。
- 不虚构数据；素材缺失处写【待补充：xxx】。

## 3.1 图表：沙箱出图 base64 内嵌（单文件自包含）

数据页需要图表时，用 run_code 在沙箱里 matplotlib 出 PNG，转 base64 内嵌进 HTML——不引外部图床、Ctrl+P 打印不掉图：

```python
import base64, io
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(8, 3.6), dpi=150)
ax.plot(months, values, marker="o", color="#1E3A5F")  # 配色跟随主题
ax.set_title("月度业绩走势")
fig.tight_layout()
buf = io.BytesIO(); fig.savefig(buf, format="png"); plt.close(fig)
b64 = base64.b64encode(buf.getvalue()).decode()
print(b64)  #  stdout 拿到后直接拼进 HTML
```

HTML 里：`<img class="chart-img" src="data:image/png;base64,{b64}">`（darkdeck 模板已带 .chart-img 样式；minimal/bizblue 用 `<img style="max-width:100%;margin-top:16px;">`）。

- 图要服务结论：一张图只讲一件事；配色跟随所选主题（minimal 黑灰 / bizblue 深蓝系 / darkdeck 黄+红绿涨跌色）。
- 中文字体镜像已配好（Noto Sans CJK SC），直接 `plt.rcParams["font.family"] = "Noto Sans CJK SC"` 更稳妥。
- 单页图表 ≤2 张；base64 会让 html 变大，图多时每张控制在 150KB 内（降 dpi 或 figsize）。
- **嵌了图表的 html 不要走 workspace_write**（200KB 文本上限会截断 base64）。改在 run_code 里组装：模板 HTML 与 b64 都在沙箱内拼好，直接写 `./output/ppt/xxx.html`（产物自动回写工作区同名路径）。


## 4. pptx 按需导出（次路径）

用户明确要可编辑稿时，用 run_code + python-pptx 生成，设计规范：

```python
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor

prs = Presentation()
prs.slide_width = Inches(13.333)   # 16:9 宽屏
prs.slide_height = Inches(7.5)

INK = RGBColor(0x11, 0x11, 0x11)        # 黑白极简主色
BLUE = RGBColor(0x1E, 0x3A, 0x5F)       # 商务深蓝主色
ACCENT = RGBColor(0x2B, 0x5A, 0xA0)     # 深蓝强调色

s = prs.slides.add_slide(prs.slide_layouts[6])  # 空白版式，自行排文本框
# ... 标题 28pt 粗体、正文 18pt、页边距 ≥0.6 英寸、右下角页码
prs.save("/out/9月促销方案_2026-08.pptx")
```

- 配色跟随所选主题（minimal 用黑灰、bizblue 用深蓝系），16:9，标题 28pt/正文 18pt，正文字号 <14pt 说明内容太多，拆页。
- 中文字体：run.font.name 之外同样要设 eastAsia（`run.font._rPr` 方式同 python-docx），不设会乱码回退。
- 交付时说明："pptx 为近似还原的可编辑稿，排版精修以 HTML 版为准。"
- 企业 potx 模板到位后：`Presentation("模板.potx")` 打开即继承母版版式，生成前先 `[l.name for l in prs.slide_layouts]` 列出可用版式再选。
