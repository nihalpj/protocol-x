"""openpyxl adapter (F-011): 5 spreadsheet tools for .xlsx workbooks.

Handlers run INSIDE the sandbox worker. `ws` is the workspace confinement
object passed by the worker; all paths resolve under the workspace root.
Limits are declared in tool descriptions and enforced by the input models
(boundary-inclusive, F-020).

Formula strings written as cell values are stored as formulas and are NEVER
evaluated: workbooks are always loaded with data_only=False, so no computed
or cached results are produced by any tool in this adapter.
"""

from __future__ import annotations

import builtins
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from adapters._guards import check_magic, check_range
from adapters.base import (
    ADAPTER_ERROR,
    CORRUPT_FILE,
    VALIDATION_ERROR,
    ToolError,
    ToolSpec,
)


# openpyxl optionally imports numpy (openpyxl.compat.numbers). In the sandbox
# worker numpy's OpenBLAS thread pool cannot start under RLIMIT_NPROC=0 and the
# worker is killed, so block the import and let openpyxl take its documented
# ImportError fallback path. This adapter never needs numpy. The block is
# conditional so importing this module in the unsandboxed server/test process
# (registry discovery) leaves numpy untouched.
def _block_numpy_in_sandbox() -> None:
    import resource

    nproc = getattr(resource, "RLIMIT_NPROC", None)
    if nproc is not None:
        try:
            soft, _hard = resource.getrlimit(nproc)
        except (OSError, ValueError):
            return
        if soft == 0:
            # A None sys.modules entry makes `import numpy` raise ImportError,
            # which openpyxl takes as its documented no-numpy fallback path.
            modules: dict[str, Any] = sys.modules
            modules["numpy"] = None


_block_numpy_in_sandbox()

MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_CELLS = 10_000  # per read/write range call (boundary-inclusive)
MAX_CREATE_CELLS = 100_000  # per xlsx_create call
MAX_SHEETS = 20
_INVALID_SHEET_CHARS = frozenset(":/\\?*[]")


def _load(ws, path: str):
    """Resolve + magic-check + parse a workbook (formulas kept, never evaluated)."""
    from openpyxl import load_workbook

    resolved = ws.resolve(path, must_exist=True)
    check_magic(resolved, "zip")
    try:
        wb = load_workbook(resolved, data_only=False)
    except Exception as exc:
        raise ToolError(
            CORRUPT_FILE,
            f"cannot parse workbook: {type(exc).__name__}",
            {"path": path},
        ) from exc
    return wb, resolved


def _get_sheet(wb, sheet: str):
    if sheet not in wb.sheetnames:
        raise ToolError(
            VALIDATION_ERROR,
            "sheet not found in workbook",
            {"sheet": sheet, "available": wb.sheetnames},
        )
    return wb[sheet]


def _check_sheet_name(name: str) -> None:
    bad = sorted(set(name) & _INVALID_SHEET_CHARS)
    if bad:
        raise ToolError(
            VALIDATION_ERROR,
            f"invalid characters in sheet name: {''.join(bad)}",
            {"sheet": name},
        )


def _parse_range(range_str: str) -> tuple[int, int, int, int]:
    """Parse "A1:C10" -> (min_col, min_row, max_col, max_row)."""
    from openpyxl.utils import range_boundaries

    try:
        return cast(tuple[int, int, int, int], range_boundaries(range_str))
    except Exception as exc:
        raise ToolError(
            VALIDATION_ERROR,
            f"invalid cell range {range_str!r} (expected e.g. \"A1:C10\")",
            {"range": range_str},
        ) from exc


def _parse_cell(cell: str) -> tuple[int, int]:
    """Parse a single cell coordinate "B2" -> (row, col)."""
    from openpyxl.utils.cell import coordinate_to_tuple

    try:
        return cast(tuple[int, int], coordinate_to_tuple(cell))
    except Exception as exc:
        raise ToolError(
            VALIDATION_ERROR,
            f"invalid start_cell {cell!r} (expected a single cell like \"B2\")",
            {"start_cell": cell},
        ) from exc


def _check_scalar(value: Any, where: str) -> None:
    if value is None or isinstance(value, str | int | float | bool):
        return
    raise ToolError(
        VALIDATION_ERROR,
        f"cell value at {where} must be a scalar (string, number, bool, or null)",
        {"type": type(value).__name__},
    )


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    return str(value)


def _save_result(ws, wb, source: Path, in_place: bool, suggested: str) -> dict[str, Any]:
    out = source if in_place else ws.new_output_path(suggested)
    try:
        wb.save(out)
    except (OSError, ValueError) as exc:
        raise ToolError(
            ADAPTER_ERROR,
            f"cannot save workbook: {type(exc).__name__}",
            {"path": str(out)},
        ) from exc
    return {"output": ws.resource_ref(out, MIME_XLSX)}


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class XlsxInfoIn(_In):
    path: str = Field(description="Workbook path, relative to the workspace root")


class XlsxReadRangeIn(_In):
    path: str
    sheet: str = Field(min_length=1, max_length=31)
    range: str = Field(min_length=1, max_length=30, description='Cell range like "A1:C10"')


class XlsxWriteRangeIn(_In):
    path: str
    sheet: str = Field(min_length=1, max_length=31, description="Sheet name; created if missing")
    start_cell: str = Field(min_length=1, max_length=10, description='Anchor cell like "B2"')
    values: list[list[Any]] = Field(min_length=1, description="2D list of scalar cell values (row-major)")
    in_place: bool = Field(
        default=False, description="Modify the source file instead of writing a new output file"
    )


class XlsxSheetIn(_In):
    name: str = Field(min_length=1, max_length=31)
    rows: list[list[Any]] = Field(default_factory=list, description="Row-major scalar cell values")


class XlsxCreateIn(_In):
    sheets: list[XlsxSheetIn] = Field(min_length=1, max_length=MAX_SHEETS)
    out_path: str | None = Field(default=None, description="Optional output path in the workspace")


class XlsxAddChartIn(_In):
    path: str
    sheet: str = Field(min_length=1, max_length=31)
    data_range: str = Field(min_length=1, max_length=30, description='Source data range like "A1:C10"')
    chart_type: Literal["bar", "line", "pie"]
    title: str = Field(min_length=1, max_length=200)
    in_place: bool = Field(
        default=False, description="Modify the source file instead of writing a new output file"
    )


def xlsx_info(ws, path: str) -> dict[str, Any]:
    wb, resolved = _load(ws, path)
    sheets = []
    for name in wb.sheetnames:
        sh = wb[name]
        rows, cols = sh.max_row, sh.max_column
        sheets.append(
            {
                "name": name,
                "dimension": sh.calculate_dimension(),
                "rows": rows,
                "cols": cols,
                "cells": rows * cols,
            }
        )
    return {
        "sheets": sheets,
        "sheet_count": len(sheets),
        "total_cells": sum(s["cells"] for s in sheets),
        "size_bytes": Path(resolved).stat().st_size,
    }


def xlsx_read_range(ws, path: str, sheet: str, range: str) -> dict[str, Any]:
    wb, _ = _load(ws, path)
    sh = _get_sheet(wb, sheet)
    min_col, min_row, max_col, max_row = _parse_range(range)
    count = (max_row - min_row + 1) * (max_col - min_col + 1)
    check_range(count, MAX_CELLS, "cells")
    # Read the exact requested rectangle: cells outside the used range read as
    # null. The workbook is never saved from this tool, so probing empty cells
    # has no persistent effect.
    values = [
        [_jsonable(sh.cell(row=r, column=c).value) for c in builtins.range(min_col, max_col + 1)]
        for r in builtins.range(min_row, max_row + 1)
    ]
    return {
        "sheet": sheet,
        "range": range,
        "rows": max_row - min_row + 1,
        "cols": max_col - min_col + 1,
        "cells": count,
        "values": values,
    }


def xlsx_write_range(
    ws,
    path: str,
    sheet: str,
    start_cell: str,
    values: list[list[Any]],
    in_place: bool = False,
) -> dict[str, Any]:
    wb, resolved = _load(ws, path)
    _check_sheet_name(sheet)
    start_row, start_col = _parse_cell(start_cell)
    for i, row in enumerate(values):
        if not row:
            raise ToolError(VALIDATION_ERROR, f"row {i} of values is empty", {"row": i})
        for j, value in enumerate(row):
            _check_scalar(value, f"values[{i}][{j}]")
    total = sum(len(r) for r in values)
    check_range(total, MAX_CELLS, "cells")
    sh = wb[sheet] if sheet in wb.sheetnames else wb.create_sheet(sheet)
    for i, row in enumerate(values):
        for j, value in enumerate(row):
            sh.cell(row=start_row + i, column=start_col + j, value=value)
    result = _save_result(ws, wb, resolved, in_place, f"edited-{resolved.name}")
    result.update(
        {
            "sheet": sheet,
            "rows_written": len(values),
            "cols_written": max(len(r) for r in values),
            "in_place": in_place,
        }
    )
    return result


def xlsx_create(ws, sheets: list[dict[str, Any]], out_path: str | None = None) -> dict[str, Any]:
    from openpyxl import Workbook

    names = [spec["name"] for spec in sheets]
    if len(set(names)) != len(names):
        raise ToolError(VALIDATION_ERROR, "duplicate sheet names", {"names": names})
    for spec in sheets:
        _check_sheet_name(spec["name"])
        for i, row in enumerate(spec["rows"]):
            if not row:
                raise ToolError(
                    VALIDATION_ERROR,
                    f"row {i} of sheet {spec['name']!r} is empty",
                    {"sheet": spec["name"], "row": i},
                )
            for j, value in enumerate(row):
                _check_scalar(value, f"sheet {spec['name']!r} values[{i}][{j}]")
    total = sum(len(row) for spec in sheets for row in spec["rows"])
    check_range(total, MAX_CREATE_CELLS, "cells")
    wb = Workbook()
    wb.remove(wb.active)
    for spec in sheets:
        sh = wb.create_sheet(spec["name"])
        for row in spec["rows"]:
            sh.append(list(row))
    target = ws.resolve(out_path) if out_path else ws.new_output_path("workbook.xlsx")
    try:
        wb.save(target)
    except (OSError, ValueError) as exc:
        raise ToolError(
            ADAPTER_ERROR,
            f"cannot save workbook: {type(exc).__name__}",
            {"path": str(target)},
        ) from exc
    return {
        "output": ws.resource_ref(target, MIME_XLSX),
        "sheet_count": len(sheets),
        "cells_written": total,
    }


def xlsx_add_chart(
    ws,
    path: str,
    sheet: str,
    data_range: str,
    chart_type: str,
    title: str,
    in_place: bool = False,
) -> dict[str, Any]:
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference
    from openpyxl.utils import get_column_letter

    wb, resolved = _load(ws, path)
    sh = _get_sheet(wb, sheet)
    min_col, min_row, max_col, max_row = _parse_range(data_range)
    count = (max_row - min_row + 1) * (max_col - min_col + 1)
    check_range(count, MAX_CELLS, "cells")
    if min_row > sh.max_row or min_col > sh.max_column:
        raise ToolError(
            VALIDATION_ERROR,
            "data_range starts outside the used range of the sheet",
            {"data_range": data_range, "sheet_dimension": sh.calculate_dimension()},
        )
    max_row = min(max_row, sh.max_row)
    max_col = min(max_col, sh.max_column)
    if chart_type == "pie":
        max_col = min_col  # pie charts render a single series (first data column)
    cls = {"bar": BarChart, "line": LineChart, "pie": PieChart}[chart_type]
    chart = cls()
    chart.title = title
    data = Reference(sh, min_col=min_col, min_row=min_row, max_col=max_col, max_row=max_row)
    chart.add_data(data, titles_from_data=True)
    anchor = f"{get_column_letter(min(max_col + 2, 16384))}{min_row}"
    sh.add_chart(chart, anchor)
    result = _save_result(ws, wb, resolved, in_place, f"charted-{resolved.name}")
    result.update({"sheet": sheet, "chart_type": chart_type, "in_place": in_place})
    return result


TOOLS = [
    ToolSpec(
        "xlsx_info",
        "Read .xlsx structure: sheet names, used-range dimensions and cell counts "
        "(used range = bounding box of cells with content). Input: workspace .xlsx path.",
        XlsxInfoIn,
        xlsx_info,
    ),
    ToolSpec(
        "xlsx_read_range",
        f"Read a rectangular cell range (e.g. \"A1:C10\") from one sheet. "
        f"Max {MAX_CELLS:,} cells per call, boundary-inclusive: exactly {MAX_CELLS:,} succeeds, "
        "more → RANGE_TOO_LARGE with the computed cell count. The exact requested rectangle is "
        "returned; cells without content (including cells outside the used range) read as null. "
        "Formula cells are returned as stored formula strings — formulas are never evaluated.",
        XlsxReadRangeIn,
        xlsx_read_range,
        limits={"max_cells": MAX_CELLS},
    ),
    ToolSpec(
        "xlsx_write_range",
        f"Write a 2D block of scalar values (strings, numbers, bools, nulls) starting at start_cell "
        f"(e.g. \"B2\"); the sheet is created if missing. Max {MAX_CELLS:,} cells per call, "
        "boundary-inclusive. Strings beginning with \"=\" are written as formulas and are NEVER "
        "evaluated (no cached results are produced). By default saves to a new output file and leaves "
        "the source untouched; set in_place: true to modify the source file.",
        XlsxWriteRangeIn,
        xlsx_write_range,
        limits={"max_cells": MAX_CELLS},
    ),
    ToolSpec(
        "xlsx_create",
        f"Create a new .xlsx workbook from sheets: a list of {{name, rows}} objects, where rows is a "
        f"2D list of scalar values. Max {MAX_SHEETS} sheets and {MAX_CREATE_CELLS:,} total cells per "
        "call. Sheet names: 1–31 chars, must not contain : / \\ ? * [ ], must be unique.",
        XlsxCreateIn,
        xlsx_create,
        limits={"max_sheets": MAX_SHEETS, "max_cells": MAX_CREATE_CELLS},
    ),
    ToolSpec(
        "xlsx_add_chart",
        f"Embed a native bar/line/pie chart into a sheet. Series data comes from data_range "
        f"(e.g. \"A1:C10\"); the first row of the range is used as series titles, pie charts use the "
        f"first data column only. Max {MAX_CELLS:,} cells of source data per call. By default saves to "
        "a new output file and leaves the source untouched; set in_place: true to modify the source file.",
        XlsxAddChartIn,
        xlsx_add_chart,
        limits={"max_cells": MAX_CELLS},
    ),
]
