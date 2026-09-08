"""Write the integration analysis workbook.

Two sheets, mirroring the hand-built analysis:

* **Mapping Details** - one row per source/target leg of each taskflow step
* **Field Values** - one row per expression field, key and parameter

A third **Parse Report** sheet lists anything the parser could not resolve, so
a reviewer can see what needs a human eye instead of discovering it later.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .. import coverage
from ..coverage import CoverageLine
from ..model.ir import Integration
from .rows import (
    FIELD_LEVEL_COLUMNS,
    MAPPING_DETAIL_COLUMNS,
    OBJECT_FIELD_COLUMNS,
    field_level_rows,
    mapping_detail_rows,
    object_field_rows,
)

_TITLE_FONT = Font(bold=True, size=13)
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=10)
_HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
_STEP_FILL = PatternFill("solid", fgColor="F2F6FA")
_BORDER = Border(*[Side(style="thin", color="BFBFBF")] * 4)
_TOP_LEFT = Alignment(horizontal="left", vertical="top", wrap_text=True)

#: Excel's hard per-cell limit. Longer values make the file unopenable, and
#: openpyxl clips them silently, so we clip deliberately and say so.
MAX_CELL_CHARS = 32767
_TRUNCATED = "\n… [truncated — see the source asset for the full value]"

#: Control characters Excel's XML cannot carry. Tabs, newlines and carriage
#: returns are legal and must survive; the rest appear in SQL and email bodies
#: often enough to matter, and raise IllegalCharacterError if left in.
_ILLEGAL_CHARS = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")


def clean_cell(value: object) -> Tuple[Optional[str], Optional[str]]:
    """Make a value safe for a worksheet cell.

    Returns ``(value, note)`` where ``note`` describes any change made, so the
    Parse Report can tell a reader that a cell is not the whole story.
    """
    if value is None or value == "":
        return None, None
    text = str(value)
    note = None

    stripped = _ILLEGAL_CHARS.sub("", text)
    if stripped != text:
        note = "contained control characters, which were removed"
        text = stripped

    if len(text) > MAX_CELL_CHARS:
        keep = MAX_CELL_CHARS - len(_TRUNCATED)
        lost = len(text) - keep
        text = text[:keep] + _TRUNCATED
        note = (f"was {lost:,} characters over Excel's {MAX_CELL_CHARS:,}-character "
                f"cell limit and was truncated")

    return text, note


#: Sheet names, in workbook order.
DETAIL_SHEET = "Mapping Details"
FIELD_SHEET = "Field Values"
OBJECT_SHEET = "Source & Target Fields"
REPORT_SHEET = "Parse Report"

#: Per-sheet column widths, in the order of the column lists.
_WIDTHS = {
    DETAIL_SHEET: [7, 26, 18, 38, 38, 18, 40, 34, 26, 32, 40, 26, 32, 44, 40],
    FIELD_SHEET: [7, 26, 18, 38, 38, 26, 20, 52, 34, 28],
    OBJECT_SHEET: [7, 26, 40, 32, 12, 28, 12, 10, 8, 13, 9, 6, 32, 26],
}

#: Every sheet starts in column A - no decorative leading gutter.
_FIRST_COLUMN = 1


def write_workbook(integration: Integration, path: Path) -> Path:
    """Render ``integration`` to an .xlsx workbook at ``path``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    wb.remove(wb.active)

    title = f"{integration.meta.taskflow_name} - Analysis"
    # Cell repairs are reported so a reader is never silently shown a
    # shortened value as if it were complete.
    notes: List[str] = []
    detail = mapping_detail_rows(integration)
    fields = field_level_rows(integration)
    _sheet(wb, DETAIL_SHEET, title, MAPPING_DETAIL_COLUMNS, detail, notes)
    _sheet(wb, FIELD_SHEET, title, FIELD_LEVEL_COLUMNS, fields, notes)
    objects = object_field_rows(integration)
    _sheet(wb, OBJECT_SHEET, title, OBJECT_FIELD_COLUMNS, objects, notes)
    _report_sheet(wb, integration, notes,
                  coverage.audit(integration, detail, fields, objects))

    wb.save(path)
    return path


def _sheet(wb: Workbook, name: str, title: str, columns: List[str],
           rows: List[List[str]], notes: List[str]) -> None:
    ws = wb.create_sheet(name)

    ws.cell(row=2, column=_FIRST_COLUMN, value=title).font = _TITLE_FONT

    header_row = 4
    for i, column in enumerate(columns, start=_FIRST_COLUMN):
        cell = ws.cell(row=header_row, column=i, value=column)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = _BORDER

    for r, values in enumerate(rows, start=header_row + 1):
        # A filled S. No marks the start of a step - tint it so the eye can
        # find step boundaries in a long sheet.
        new_step = bool(values and values[0])
        for c, value in enumerate(values, start=_FIRST_COLUMN):
            index = c - _FIRST_COLUMN
            safe, note = clean_cell(value)
            if note:
                column = columns[index] if index < len(columns) else f"column {c}"
                notes.append(f"'{name}' {get_column_letter(c)}{r} ({column}) {note}")
            cell = ws.cell(row=r, column=c, value=safe)
            cell.alignment = _TOP_LEFT
            cell.border = _BORDER
            if new_step:
                cell.fill = _STEP_FILL
                if index < 2:            # S. No and Taskflow name
                    cell.font = Font(bold=True, size=10)

    for i, width in enumerate(_WIDTHS.get(name, []), start=_FIRST_COLUMN):
        ws.column_dimensions[get_column_letter(i)].width = width

    _finish(ws, header_row, len(columns))


def _report_sheet(wb: Workbook, integration: Integration,
                  cell_notes: Optional[List[str]] = None,
                  coverage_lines: Optional[List[CoverageLine]] = None) -> None:
    """Everything the parser wants a human to look at."""
    ws = wb.create_sheet(REPORT_SHEET)
    label_col = _FIRST_COLUMN
    value_col = label_col + 1
    detail_col = label_col + 2
    last_col = label_col + 4

    ws.cell(row=2, column=label_col, value=REPORT_SHEET).font = _TITLE_FONT

    meta = integration.meta
    summary = [
        ("Taskflow", meta.taskflow_name),
        ("Project / Folder", meta.project),
        ("Source org", meta.source_org),
        ("Version", meta.version_label),
        ("Last modified", f"{meta.modified_date} by {meta.modified_by}".strip(" by")),
        ("Steps parsed", str(len(integration.steps))),
        ("Mappings parsed", str(len(integration.mappings))),
        ("Tasks parsed", str(len(integration.tasks))),
        ("Connections", str(len(integration.connections))),
    ]
    row = 4
    for label, value in summary:
        ws.cell(row=row, column=label_col, value=label).font = Font(bold=True, size=10)
        ws.cell(row=row, column=value_col, value=value).alignment = _TOP_LEFT
        row += 1

    # Coverage: what the package held vs what these sheets show. This is the
    # check that catches a whole category being missed, which no individual
    # parse failure would reveal.
    if coverage_lines:
        totals = coverage.overall(coverage_lines)
        row += 1
        ws.cell(row=row, column=label_col, value="Coverage").font = _TITLE_FONT
        ws.cell(row=row, column=value_col,
                value=f"{totals['percent']}% of objects found in the package "
                      f"appear in this workbook").alignment = _TOP_LEFT
        row += 1

        for header, col in (("Category", label_col), ("Result", value_col),
                            ("Not represented", detail_col)):
            cell = ws.cell(row=row, column=col, value=header)
            cell.font = _HEADER_FONT
            cell.fill = _HEADER_FILL
            cell.alignment = _TOP_LEFT
        row += 1

        for line in coverage_lines:
            ws.cell(row=row, column=label_col, value=line.category).alignment = _TOP_LEFT
            result = ws.cell(row=row, column=value_col, value=line.summary)
            result.alignment = _TOP_LEFT
            if not line.complete:
                result.font = Font(bold=True, color="9C4221", size=10)
            detail = "; ".join(line.missing[:6])
            if len(line.missing) > 6:
                detail += f" … and {len(line.missing) - 6} more"
            if detail and line.note:
                detail = f"{detail}  ({line.note})"
            value, _ = clean_cell(detail)
            ws.cell(row=row, column=detail_col, value=value).alignment = _TOP_LEFT
            ws.merge_cells(start_row=row, start_column=detail_col,
                           end_row=row, end_column=last_col)
            row += 1

    row += 1
    ws.cell(row=row, column=label_col, value="Items needing review").font = _TITLE_FONT
    row += 1
    items = list(integration.warnings) + list(cell_notes or [])
    if items:
        for item in items:
            value, _ = clean_cell(item)
            ws.cell(row=row, column=label_col, value=value).alignment = _TOP_LEFT
            ws.merge_cells(start_row=row, start_column=label_col,
                           end_row=row, end_column=last_col)
            row += 1
    else:
        ws.cell(row=row, column=label_col,
                value="None - every asset in the package was parsed.").alignment = _TOP_LEFT

    ws.column_dimensions[get_column_letter(label_col)].width = 34
    for col in range(value_col, last_col + 1):
        ws.column_dimensions[get_column_letter(col)].width = 30
    ws.sheet_view.showGridLines = False


def _finish(ws: Worksheet, header_row: int, column_count: int) -> None:
    first = get_column_letter(_FIRST_COLUMN)
    last = get_column_letter(_FIRST_COLUMN + column_count - 1)
    # Freeze the header band only; with data starting in column A there is no
    # gutter to hold open on the left.
    ws.freeze_panes = ws.cell(row=header_row + 1, column=_FIRST_COLUMN)
    ws.auto_filter.ref = f"{first}{header_row}:{last}{ws.max_row}"
    ws.sheet_view.showGridLines = False
