"""Is our generated analysis a superset of the hand-built one?

Every fact a human wrote into the manual workbook is looked for in ours. A
multi-line cell is checked line by line, because the manual packs several facts
into one cell and we may spread them across columns. Two passes:

  strict      the text, ignoring case and whitespace
  identifier  additionally ignoring _ - : = . / , so the manual's
              "mct=DUMMY-CONCUR:EMPLOYEE" matches our "mct_DUMMY_CONCUR_EMPLOYEE"

Anything neither pass finds is a real difference, and gets printed.
"""
import re
import sys
from openpyxl import load_workbook


def strict(text):
    return re.sub(r"\s+", " ", str(text).replace(" ", " ")).strip().lower()


def ident(text):
    return re.sub(r"[_\-:=./,\s]+", "", str(text)).lower()


def facts(path):
    """Every distinct fact in a workbook: one per non-empty line of any cell."""
    wb = load_workbook(path, data_only=True)
    out = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            for value in row:
                if value is None:
                    continue
                for line in str(value).splitlines():
                    line = line.strip()
                    if len(line) > 2:
                        out.append((ws.title, line))
    return out


manual, ours = sys.argv[1], sys.argv[2]
mine = facts(ours)
strict_blob = " ␟ ".join(strict(t) for _, t in mine)
ident_blob = " ␟ ".join(ident(t) for _, t in mine)

hit_strict = hit_ident = 0
missing = []
seen = set()
for sheet, text in facts(manual):
    if text in seen:
        continue
    seen.add(text)
    if strict(text) in strict_blob:
        hit_strict += 1
    elif ident(text) in ident_blob:
        hit_ident += 1
    else:
        missing.append((sheet, text))

total = hit_strict + hit_ident + len(missing)
print(f"distinct facts in the manual : {total}")
print(f"  found as written           : {hit_strict}")
print(f"  found, named differently   : {hit_ident}")
print(f"  NOT in our output          : {len(missing)}"
      f"   ({100 * (total - len(missing)) // total}% covered)")
print()
for sheet, text in missing:
    print(f"  [{sheet}] {text[:160]}")
