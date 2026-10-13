"""Evaluate every formula of the pricer with pycel and print the results, so the workbook is
checked without opening Excel. Also tries a few input scenarios."""

import os
import sys

from openpyxl import load_workbook
from pycel import ExcelCompiler

path = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports", "otc_clip_pricer.xlsx"))
xl = ExcelCompiler(filename=path)
wb = load_workbook(path)
errors = []


def show(sheet, max_rows=40, cols="ABCDE"):
    ws = wb[sheet]
    print(f"--- {sheet}")
    for row in range(1, min(ws.max_row, max_rows) + 1):
        out = []
        for col in cols:
            cell = ws[f"{col}{row}"]
            if cell.value is None:
                continue
            if isinstance(cell.value, str) and cell.value.startswith("="):
                v = xl.evaluate(f"'{sheet}'!{col}{row}")
                if v is None or (isinstance(v, str) and v.startswith("#")):
                    errors.append(f"{sheet}!{col}{row}: {v}")
                out.append(f"{v:.3f}" if isinstance(v, float) else str(v))
            elif col != "C":
                out.append(str(cell.value)[:34])
        if out:
            print(" | ".join(out))


show("Pricer")
show("Quote grid", cols="ABCDE")
show("Cost of waiting", max_rows=12)

for asset, size, hour, side in (("ETH", 2, 18, "Client sells"), ("BTC", 10, 3, "Client buys"), ("BTC", 0.3, 14, "Client buys")):
    xl.set_value("Pricer!B5", asset)
    xl.set_value("Pricer!B6", side)
    xl.set_value("Pricer!B7", size)
    xl.set_value("Pricer!B8", hour)
    vals = {r: xl.evaluate(f"Pricer!B{r}") for r in (16, 19, 20, 24, 26)}
    print(f"scenario {asset} {size}M h{hour} {side}: window={vals[16]} sweep={vals[19]:.2f} "
          f"single={vals[20]:.2f} risk={vals[24]:.2f} spread={xl.evaluate('Pricer!B29'):.2f} "
          f"price={xl.evaluate('Pricer!B30'):.2f} method={vals[26]}")

print("formula errors:", errors or "none")
