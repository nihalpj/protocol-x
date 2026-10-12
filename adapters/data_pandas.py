"""Pandas adapter (F-010): 6 CSV tools.

Handlers run INSIDE the sandbox worker. Row filtering dispatches over an
explicitly whitelisted op table — pandas query/eval are never used. Limits
are declared in descriptions and enforced by the input models
(boundary-inclusive, F-020). The input size cap is checked before parsing
and can be overridden with the PROTOX_CSV_MAX_INPUT_BYTES env var (bytes).
"""

from __future__ import annotations

import math
import os
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from adapters._guards import check_input_size
from adapters.base import ADAPTER_ERROR, CORRUPT_FILE, VALIDATION_ERROR, ToolError, ToolSpec

MAX_INPUT_BYTES = 200 * 1024 * 1024  # 200 MB input cap (checked before parsing)
MAX_READ_ROWS = 5000  # csv_read rows per call, boundary-inclusive
_ENV_CAP = "PROTOX_CSV_MAX_INPUT_BYTES"

# OpenBLAS spawns threads when numpy loads; sandbox workers run with
# RLIMIT_NPROC=0, so BLAS must be single-threaded before pandas imports.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

FilterOp = Literal[
    "eq", "ne", "gt", "gte", "lt", "lte",
    "contains", "startswith", "endswith",
    "isnull", "notnull",
]
AggFunc = Literal["sum", "mean", "min", "max", "count"]


def _cap_bytes() -> int:
    raw = os.environ.get(_ENV_CAP)
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return MAX_INPUT_BYTES


def _read(ws, path: str):
    import pandas as pd

    resolved = ws.resolve(path, must_exist=True)
    check_input_size(resolved, _cap_bytes())
    try:
        df = pd.read_csv(resolved)
    except pd.errors.EmptyDataError as exc:
        raise ToolError(CORRUPT_FILE, "CSV is empty", {"path": path}) from exc
    except (pd.errors.ParserError, UnicodeDecodeError) as exc:
        raise ToolError(CORRUPT_FILE, f"cannot parse CSV ({type(exc).__name__})", {"path": path}) from exc
    return df, resolved


def _clean(value: Any) -> Any:
    """Make a pandas/numpy cell JSON-safe: NaN/inf -> None, scalars -> Python natives."""
    if value is None:
        return None
    item = getattr(value, "item", None)
    if callable(item):
        with suppress(TypeError, ValueError):
            value = item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _save_csv(ws, df, out_path: str | None, suggested: str) -> dict[str, Any]:
    target = ws.resolve(out_path) if out_path else ws.new_output_path(suggested)
    df.to_csv(target, index=False)
    return {
        "output": ws.resource_ref(target, "text/csv"),
        "rows": len(df),
        "columns": [str(c) for c in df.columns],
    }


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CsvInfoIn(_In):
    path: str = Field(description="CSV file path, relative to the workspace root (input ≤ 200 MB)")


class CsvReadIn(_In):
    path: str
    limit: int = Field(
        default=MAX_READ_ROWS,
        ge=1,
        le=MAX_READ_ROWS,
        description=f"Max rows to return, 1–{MAX_READ_ROWS} (boundary-inclusive)",
    )
    offset: int = Field(default=0, ge=0, description="Data rows to skip before reading (paging)")


class CsvFilterIn(_In):
    path: str
    column: str
    op: FilterOp = Field(
        description="Whitelisted op: eq, ne, gt, gte, lt, lte, contains, startswith, endswith, "
        "isnull, notnull"
    )
    value: str | int | float | None = Field(
        default=None, description="Comparison value (string or number); ignored for isnull/notnull"
    )
    out_path: str | None = Field(default=None, description="Optional output path in workspace")


class CsvAggregateIn(_In):
    path: str
    group_by: str
    aggregations: dict[str, list[AggFunc]] = Field(
        min_length=1,
        description="Per-column list of whitelisted aggregations: sum, mean, min, max, count",
    )


class CsvJoinIn(_In):
    left: str
    right: str
    on: str
    how: Literal["inner", "left"] = "inner"
    out_path: str | None = None


class CsvDescribeIn(_In):
    path: str


def csv_info(ws, path: str) -> dict[str, Any]:
    df, resolved = _read(ws, path)
    return {
        "rows": len(df),
        "columns": [str(c) for c in df.columns],
        "dtypes": {str(c): str(t) for c, t in df.dtypes.items()},
        "size_bytes": Path(resolved).stat().st_size,
    }


def csv_read(ws, path: str, limit: int = MAX_READ_ROWS, offset: int = 0) -> dict[str, Any]:
    import pandas as pd

    resolved = ws.resolve(path, must_exist=True)
    check_input_size(resolved, _cap_bytes())
    try:
        chunk = pd.read_csv(resolved, skiprows=range(1, offset + 1), nrows=limit + 1)
    except pd.errors.EmptyDataError as exc:
        raise ToolError(CORRUPT_FILE, "CSV is empty", {"path": path}) from exc
    except (pd.errors.ParserError, UnicodeDecodeError) as exc:
        raise ToolError(CORRUPT_FILE, f"cannot parse CSV ({type(exc).__name__})", {"path": path}) from exc
    truncated = len(chunk) > limit
    if truncated:
        chunk = chunk.head(limit)
    return {
        "columns": [str(c) for c in chunk.columns],
        "rows": [[_clean(v) for v in row] for row in chunk.itertuples(index=False, name=None)],
        "row_count": len(chunk),
        "offset": offset,
        "truncated": truncated,
    }


def _apply_filter(df, column: str, op: str, value: Any):
    if column not in df.columns:
        raise ToolError(
            VALIDATION_ERROR, "unknown column", {"column": column, "available": [str(c) for c in df.columns]}
        )
    series = df[column]
    try:
        if op == "eq":
            return df[series == value]
        if op == "ne":
            return df[series != value]
        if op == "gt":
            return df[series > value]
        if op == "gte":
            return df[series >= value]
        if op == "lt":
            return df[series < value]
        if op == "lte":
            return df[series <= value]
        if op == "contains":
            return df[series.astype("string").str.contains(str(value), regex=False, na=False)]
        if op == "startswith":
            return df[series.astype("string").str.startswith(str(value), na=False)]
        if op == "endswith":
            return df[series.astype("string").str.endswith(str(value), na=False)]
        if op == "isnull":
            return df[series.isna()]
        if op == "notnull":
            return df[series.notna()]
    except (TypeError, ValueError) as exc:
        raise ToolError(
            VALIDATION_ERROR,
            f"cannot apply op {op!r} to column {column!r} ({type(exc).__name__})",
            {"column": column, "op": op},
        ) from exc
    raise ToolError(VALIDATION_ERROR, "unsupported filter op", {"op": op})


def csv_filter(ws, path: str, column: str, op: str, value: Any = None, out_path: str | None = None) -> dict:
    df, _ = _read(ws, path)
    filtered = _apply_filter(df, column, op, value)
    return _save_csv(ws, filtered, out_path, "filtered.csv")


def csv_aggregate(ws, path: str, group_by: str, aggregations: dict[str, list[str]]) -> dict[str, Any]:
    df, _ = _read(ws, path)
    if group_by not in df.columns:
        raise ToolError(
            VALIDATION_ERROR,
            "unknown group_by column",
            {"group_by": group_by, "available": [str(c) for c in df.columns]},
        )
    missing = [c for c in aggregations if c not in df.columns]
    if missing:
        raise ToolError(VALIDATION_ERROR, "unknown aggregation columns", {"missing": missing})
    if any(not funcs for funcs in aggregations.values()):
        raise ToolError(VALIDATION_ERROR, "aggregation lists must be non-empty")
    try:
        grouped = df.groupby(group_by, dropna=False).agg(aggregations).reset_index()
    except (TypeError, ValueError) as exc:
        raise ToolError(ADAPTER_ERROR, f"aggregation failed ({type(exc).__name__})", {}) from exc
    grouped.columns = [
        c if isinstance(c, str) else "_".join(str(p) for p in c if p) for c in grouped.columns
    ]
    groups = [{str(k): _clean(v) for k, v in row.items()} for row in grouped.to_dict("records")]
    return {"group_by": group_by, "group_count": len(groups), "groups": groups}


def csv_join(ws, left: str, right: str, on: str, how: str = "inner", out_path: str | None = None) -> dict:
    import pandas as pd

    df_left, _ = _read(ws, left)
    df_right, _ = _read(ws, right)
    if on not in df_left.columns or on not in df_right.columns:
        raise ToolError(
            VALIDATION_ERROR,
            "join key must exist in both CSVs",
            {
                "on": on,
                "left_columns": [str(c) for c in df_left.columns],
                "right_columns": [str(c) for c in df_right.columns],
            },
        )
    merged = pd.merge(df_left, df_right, on=on, how=how)
    return _save_csv(ws, merged, out_path, "joined.csv")


def csv_describe(ws, path: str) -> dict[str, Any]:
    df, _ = _read(ws, path)
    try:
        stats = df.describe().to_dict()
    except ValueError as exc:
        raise ToolError(ADAPTER_ERROR, f"describe failed ({type(exc).__name__})", {}) from exc
    return {
        "rows": len(df),
        "numeric_columns": [str(c) for c in stats],
        "stats": {
            str(col): {str(k): _clean(v) for k, v in series.items()} for col, series in stats.items()
        },
    }


_LIMITS = {"rss_mb": 1024}

TOOLS = [
    ToolSpec(
        "csv_info",
        "Inspect a CSV: row count, column names, pandas dtypes, file size bytes. Reads the whole "
        "file; input ≤ 200 MB.",
        CsvInfoIn,
        csv_info,
        limits=_LIMITS,
    ),
    ToolSpec(
        "csv_read",
        f"Read CSV rows as JSON (values as list-of-lists, NaN → null). At most {MAX_READ_ROWS} rows per "
        "call (boundary-inclusive); use offset to page. truncated:true signals more rows exist.",
        CsvReadIn,
        csv_read,
        limits={**_LIMITS, "max_rows": MAX_READ_ROWS},
    ),
    ToolSpec(
        "csv_filter",
        "Filter rows with a whitelisted op (eq, ne, gt, gte, lt, lte, contains, startswith, endswith, "
        "isnull, notnull) on one column — never query/eval. Writes the filtered CSV (≤ input rows) to the "
        "workspace. Input ≤ 200 MB.",
        CsvFilterIn,
        csv_filter,
        limits=_LIMITS,
    ),
    ToolSpec(
        "csv_aggregate",
        "Group by one column and compute whitelisted aggregations (sum, mean, min, max, count) per "
        "requested column; returns JSON groups. Numeric aggregations require numeric columns. "
        "Input ≤ 200 MB.",
        CsvAggregateIn,
        csv_aggregate,
        limits=_LIMITS,
    ),
    ToolSpec(
        "csv_join",
        "Join two workspace CSVs on a shared column (how: inner or left); writes the merged CSV "
        "(≤ left+right rows) to the workspace. Input ≤ 200 MB each.",
        CsvJoinIn,
        csv_join,
        limits=_LIMITS,
    ),
    ToolSpec(
        "csv_describe",
        "Summary statistics (count, mean, std, min, 25/50/75% quartiles, max) for numeric columns as "
        "JSON. Input ≤ 200 MB.",
        CsvDescribeIn,
        csv_describe,
        limits=_LIMITS,
    ),
]
