"""recalc_check.py — 交付前重算校验：重算 Excel 全部公式并扫描错误值。

用法：python recalc_check.py <file.xlsx>
退出码：0 = 零错误；1 = 发现公式错误或文件无法重算。

依赖 formulas 库（沙箱镜像已装）。大文件（公式数 > 几千）重算可能较慢，属正常。
"""
import sys

ERRORS = ("#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#N/A", "#NULL!", "#NUM!")


def scan(path: str) -> int:
    import formulas

    model = formulas.ExcelModel().loads(path).finish()
    solution = model.calculate()

    bad: list[str] = []
    for key, rng in solution.items():
        try:
            values = rng.value.flat if hasattr(rng.value, "flat") else [rng.value]
            for v in values:
                s = str(v)
                for err in ERRORS:
                    if err in s:
                        bad.append(f"{key}: {err}")
                        break
        except Exception:
            continue  # 单个单元格取值异常不阻塞整体扫描

    if bad:
        print(f"[FAIL] {path}: 发现 {len(bad)} 处公式错误")
        for line in bad[:50]:
            print("  " + line)
        if len(bad) > 50:
            print(f"  ... 其余 {len(bad) - 50} 处省略")
        return 1
    print(f"[OK] {path}: 重算完成，零公式错误")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    try:
        sys.exit(scan(sys.argv[1]))
    except Exception as e:
        print(f"[FAIL] 无法重算文件: {e!s}")
        sys.exit(1)
