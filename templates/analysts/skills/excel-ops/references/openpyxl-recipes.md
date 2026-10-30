# openpyxl 标准写法片段库

写 openpyxl 代码前从这里抄，不要凭记忆写。所有片段均验证过 API 形态。

## 读取

```python
import openpyxl

# 读计算结果（缓存值）；文件未被 Excel 保存过时缓存为 None
wb = openpyxl.load_workbook(path, data_only=True)
# 读公式本身
wb = openpyxl.load_workbook(path)  # cell.value 是 "=B2/C2" 字符串

ws = wb["Sheet1"]
print(ws.max_row, ws.max_column)  # 注意：max_row 可能被空样式行撑大，不可全信
```

## 写公式（交付可重算表格）

```python
ws["D2"] = "=B2*C2"            # 公式字符串，Excel 打开后自动算
ws["D10"] = "=SUM(D2:D9)"
ws["E2"] = "=IF(C2=0,\"\",B2/C2)"   # 防 #DIV/0! 的写法
ws["E2"].number_format = "0.0%"      # 百分比显示
ws["B2"].number_format = "#,##0.00"  # 千分位
```

## 复制样式（openpyxl 没有整行复制 API）

```python
from copy import copy

def copy_row_style(ws, src_row: int, dst_row: int, max_col: int):
    for col in range(1, max_col + 1):
        s, d = ws.cell(src_row, col), ws.cell(dst_row, col)
        d.font = copy(s.font)
        d.fill = copy(s.fill)
        d.border = copy(s.border)
        d.alignment = copy(s.alignment)
        d.number_format = s.number_format
```

## 冻结窗格 / 列宽 / 自动筛选

```python
ws.freeze_panes = "A2"          # 冻结首行；"B2" = 冻结首行首列
ws.column_dimensions["A"].width = 18
ws.auto_filter.ref = f"A1:E{ws.max_row}"
```

## 合并单元格

```python
ws.merge_cells("A1:E1")
ws["A1"] = "标题"               # 只能写左上角锚点
ws["A1"].alignment = openpyxl.styles.Alignment(horizontal="center", vertical="center")
# 拆分
ws.unmerge_cells("A1:E1")
```

## 用 pandas 出"透视"再写入（推荐路线）

openpyxl 没有真透视表 API。**正确做法：pandas groupby 出聚合结果 → 写入新 sheet → 合计行写公式。**

```python
import pandas as pd

df = pd.read_excel(src, sheet_name="明细")
pivot = df.groupby(["仓库", "SKU"], as_index=False).agg(
    销量=("数量", "sum"), 销售额=("金额", "sum"),
)
pivot.to_excel(out_path, sheet_name="汇总", index=False)
# 然后 openpyxl 打开 out_path 补合计公式和样式
```

## 新建 sheet 的标准样式（表头）

```python
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

header_font = Font(bold=True)
header_fill = PatternFill("solid", fgColor="F2F2F2")
thin = Side(style="thin", color="D9D9D9")
border = Border(left=thin, right=thin, top=thin, bottom=thin)

for cell in ws[1]:
    cell.font = header_font
    cell.fill = header_fill
    cell.border = border
    cell.alignment = Alignment(horizontal="center")
```

## 图表（openpyxl.chart）

```python
from openpyxl.chart import BarChart, Reference

chart = BarChart()
chart.title = "3 月周转率环比下降 12%"   # 标题说结论
data = Reference(ws, min_col=3, min_row=1, max_row=10)   # 含表头
cats = Reference(ws, min_col=1, min_row=2, max_row=10)
chart.add_data(data, titles_from_data=True)
chart.set_categories(cats)
ws.add_chart(chart, "F2")
```

注意：复杂图表优先用 matplotlib 出 PNG（沙箱已配中文字体），Excel 内嵌图表只用于简单柱状/折线。

## 保存

```python
wb.save(out_path)   # 必须另存新文件，绝不覆盖用户原件
```
