"""AC-F010: pandas/CSV adapter — schema limits (boundary-inclusive), whitelisted
filter ops (no query/eval, regression-tested), input size cap, aggregates, joins."""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from adapters.base import CORRUPT_FILE, INPUT_TOO_LARGE, VALIDATION_ERROR, ToolError
from core.workspace import Workspace

ROW_COUNT = 6_005  # straddles the 5000-row csv_read cap


@pytest.fixture(autouse=True)
def _restore_numpy():
    """adapters/excel_openpyxl.py sets sys.modules["numpy"] = None at import time
    (intended only for sandbox workers). The registry imports every adapter in-process
    when building the catalog, which poisons numpy in this test process. Rebind the
    already-initialized module so in-process pandas baselines keep working."""
    _ensure_numpy()
    yield
    _ensure_numpy()


def _ensure_numpy() -> None:
    if sys.modules.get("numpy") is None:
        sys.modules["numpy"] = np


def _sample_df(rows: int = ROW_COUNT) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    return pd.DataFrame(
        {
            "id": np.arange(rows),
            "group": [f"g{i % 5}" for i in range(rows)],
            "score": rng.integers(0, 100, rows),
            "ratio": rng.random(rows).round(4),
            "label": [f"item-{i}" for i in range(rows)],
            "optional": [str(i) if i % 3 else None for i in range(rows)],
        }
    )


@pytest.fixture()
def sample_csv(settings, _restore_numpy) -> str:
    _ensure_numpy()
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    _sample_df().to_csv(settings.workspace_root / "sample.csv", index=False)
    return "sample.csv"


@pytest.fixture()
def right_csv(settings) -> str:
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    right = pd.DataFrame(
        {
            "group": [f"g{i}" for i in range(5)],
            "owner": ["alice", "bob", "carol", "dave", "erin"],
            "quota": [10, 20, 30, 40, 50],
        }
    )
    right.to_csv(settings.workspace_root / "right.csv", index=False)
    return "right.csv"


def _data(ctx_out: dict) -> dict:
    return ctx_out["result"]["data"]


def _read_output(settings, ctx_out: dict) -> pd.DataFrame:
    _ensure_numpy()
    rel = _data(ctx_out)["output"]["path"]
    return pd.read_csv(settings.workspace_root / rel)


def _csv_df(settings) -> pd.DataFrame:
    """The sample CSV as pandas re-reads it (dtypes follow the CSV round trip)."""
    _ensure_numpy()
    return pd.read_csv(settings.workspace_root / "sample.csv")


# -- csv_info / csv_read -------------------------------------------------------


def test_csv_info(ctx, sample_csv):
    out = _data(ctx.dispatch("csv_info", {"path": sample_csv}))
    assert out["rows"] == ROW_COUNT
    assert out["columns"] == ["id", "group", "score", "ratio", "label", "optional"]
    assert out["dtypes"]["id"] == "int64"
    assert out["dtypes"]["score"] == "int64"
    assert out["dtypes"]["label"] == "object"
    assert out["size_bytes"] > 0


def test_csv_read_limit_is_boundary_inclusive(ctx, settings):
    _sample_df(rows=5000).to_csv(settings.workspace_root / "exact.csv", index=False)
    out = _data(ctx.dispatch("csv_read", {"path": "exact.csv", "limit": 5000}))
    assert out["row_count"] == 5000
    assert out["truncated"] is False


def test_csv_read_truncates_and_pages(ctx, sample_csv):
    out = _data(ctx.dispatch("csv_read", {"path": sample_csv, "limit": 5000}))
    assert out["row_count"] == 5000
    assert out["truncated"] is True
    assert out["rows"][0][0] == 0  # first row is id 0
    tail = _data(ctx.dispatch("csv_read", {"path": sample_csv, "limit": 5000, "offset": 5000}))
    assert tail["row_count"] == ROW_COUNT - 5000
    assert tail["truncated"] is False
    assert tail["rows"][0][0] == 5000
    assert tail["rows"][-1][0] == ROW_COUNT - 1
    # NaN in the optional column serializes as null
    row_with_null = next(r for r in out["rows"] if r[0] == 0)
    assert row_with_null[5] is None


def test_csv_read_limit_above_cap_rejected(ctx, sample_csv):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("csv_read", {"path": sample_csv, "limit": 5001})
    assert exc.value.code == VALIDATION_ERROR


def test_csv_extra_arg_forbidden(ctx, sample_csv):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("csv_info", {"path": sample_csv, "unexpected": 1})
    assert exc.value.code == VALIDATION_ERROR


# -- csv_filter ----------------------------------------------------------------


def _baseline(df: pd.DataFrame, op: str, value):
    if op == "isnull":
        return df[df["optional"].isna()]
    if op == "notnull":
        return df[df["optional"].notna()]
    col = {"eq": "group", "ne": "group", "gt": "score", "gte": "score", "lt": "score", "lte": "score",
           "contains": "label", "startswith": "label", "endswith": "label"}[op]
    series = df[col]
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
    raise AssertionError(op)


@pytest.mark.parametrize(
    "op,value",
    [
        ("eq", "g2"),
        ("ne", "g0"),
        ("gt", 90),
        ("gte", 99),
        ("lt", 1),
        ("lte", 0),
        ("contains", "item-1"),
        ("startswith", "item-10"),
        ("endswith", "3"),
        ("isnull", None),
        ("notnull", None),
    ],
)
def test_csv_filter_matches_pandas_baseline(ctx, settings, sample_csv, op, value):
    args = {"path": sample_csv, "column": _baseline_column(op), "op": op}
    if value is not None:
        args["value"] = value
    out = _data(ctx.dispatch("csv_filter", args))
    got = _read_output(settings, {"result": {"data": out}})
    expected = _baseline(_csv_df(settings), op, value).reset_index(drop=True)
    pd.testing.assert_frame_equal(got, expected)


def _baseline_column(op: str) -> str:
    if op in ("eq", "ne"):
        return "group"
    if op in ("gt", "gte", "lt", "lte"):
        return "score"
    if op in ("contains", "startswith", "endswith"):
        return "label"
    return "optional"  # isnull / notnull


@pytest.mark.parametrize("op", ["query", "eval", "exec", "where"])
def test_csv_filter_non_whitelisted_op_rejected_by_schema(ctx, sample_csv, op):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("csv_filter", {"path": sample_csv, "column": "score", "op": op, "value": 1})
    assert exc.value.code == VALIDATION_ERROR


def test_csv_filter_rejects_unknown_column(ctx, sample_csv):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("csv_filter", {"path": sample_csv, "column": "nope", "op": "eq", "value": 1})
    assert exc.value.code == VALIDATION_ERROR


def test_csv_filter_rejects_incomparable_value(ctx, sample_csv):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("csv_filter", {"path": sample_csv, "column": "score", "op": "gt", "value": "abc"})
    assert exc.value.code == VALIDATION_ERROR


def test_no_query_or_eval_in_adapter_source():
    src = Path(__file__).resolve().parents[2] / "adapters" / "data_pandas.py"
    text = src.read_text()
    assert not re.search(r"\.query\s*\(", text), "adapter must not use pandas query()"
    assert not re.search(r"\.eval\s*\(", text), "adapter must not use pandas eval()"


# -- csv_aggregate -------------------------------------------------------------


def test_csv_aggregate_matches_pandas_baseline(ctx, settings, sample_csv):
    aggs = {"score": ["sum", "mean", "min", "max", "count"], "ratio": ["mean"]}
    args = {"path": sample_csv, "group_by": "group", "aggregations": aggs}
    out = _data(ctx.dispatch("csv_aggregate", args))
    assert out["group_count"] == 5
    _ensure_numpy()
    baseline = _csv_df(settings).groupby("group").agg(aggs).reset_index()
    baseline.columns = [
        c if isinstance(c, str) else "_".join(str(p) for p in c if p) for c in baseline.columns
    ]
    got = pd.DataFrame(out["groups"]).sort_values("group").reset_index(drop=True)
    expected = baseline.sort_values("group").reset_index(drop=True)
    pd.testing.assert_frame_equal(got[expected.columns], expected)


def test_csv_aggregate_unknown_column(ctx, sample_csv):
    with pytest.raises(ToolError) as exc:
        args = {"path": sample_csv, "group_by": "group", "aggregations": {"nope": ["sum"]}}
        ctx.dispatch("csv_aggregate", args)
    assert exc.value.code == VALIDATION_ERROR


def test_csv_aggregate_non_numeric_mean_is_adapter_error(ctx, sample_csv):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch(
            "csv_aggregate", {"path": sample_csv, "group_by": "group", "aggregations": {"label": ["mean"]}}
        )
    assert exc.value.code == "ADAPTER_ERROR"


# -- csv_join ------------------------------------------------------------------


@pytest.mark.parametrize("how", ["inner", "left"])
def test_csv_join_matches_pandas_baseline(ctx, settings, sample_csv, right_csv, how):
    out = _data(ctx.dispatch("csv_join", {"left": sample_csv, "right": right_csv, "on": "group", "how": how}))
    got = _read_output(settings, {"result": {"data": out}})
    expected = pd.merge(_csv_df(settings), pd.DataFrame({
        "group": [f"g{i}" for i in range(5)],
        "owner": ["alice", "bob", "carol", "dave", "erin"],
        "quota": [10, 20, 30, 40, 50],
    }), on="group", how=how)
    pd.testing.assert_frame_equal(got, expected)


def test_csv_join_missing_key_rejected(ctx, sample_csv, right_csv):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("csv_join", {"left": sample_csv, "right": right_csv, "on": "id"})
    assert exc.value.code == VALIDATION_ERROR


# -- csv_describe ----------------------------------------------------------------


def test_csv_describe(ctx, settings, sample_csv):
    out = _data(ctx.dispatch("csv_describe", {"path": sample_csv}))
    baseline = _csv_df(settings).describe()
    assert out["rows"] == ROW_COUNT
    assert set(out["numeric_columns"]) == set(baseline.columns)
    score = out["stats"]["score"]
    assert score["count"] == ROW_COUNT
    assert score["max"] == float(baseline["score"]["max"])


# -- size cap / corrupt files ----------------------------------------------------


def test_input_cap_rejects_oversize_file_direct_call(monkeypatch, tmp_path):
    _ensure_numpy()
    ws = Workspace(tmp_path)
    (tmp_path / "tiny.csv").write_text("a,b\n1,2\n")
    monkeypatch.setenv("PROTOX_CSV_MAX_INPUT_BYTES", "4")
    from adapters import data_pandas

    with pytest.raises(ToolError) as exc:
        data_pandas.csv_info(ws, path="tiny.csv")
    assert exc.value.code == INPUT_TOO_LARGE
    assert exc.value.details["cap_bytes"] == 4


def test_input_cap_default_allows_normal_files(ctx, sample_csv):
    # boundary check of the other side: a valid file under the default cap parses fine
    out = _data(ctx.dispatch("csv_info", {"path": sample_csv}))
    assert out["rows"] == ROW_COUNT


def test_corrupt_file_invalid_utf8(ctx, settings):
    (settings.workspace_root / "bad.csv").write_bytes(b"\xff\xfe\xfd\xfc not a csv \x00\x01")
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("csv_info", {"path": "bad.csv"})
    assert exc.value.code == CORRUPT_FILE


def test_corrupt_file_empty(ctx, settings):
    (settings.workspace_root / "empty.csv").write_bytes(b"")
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("csv_read", {"path": "empty.csv"})
    assert exc.value.code == CORRUPT_FILE


# -- benchmark -------------------------------------------------------------------


@pytest.mark.slow
def test_csv_aggregate_100k_under_4s(ctx, settings):
    from tests.fixtures.make_big_csv import write_big_csv

    target = settings.workspace_root / "big.csv"
    write_big_csv(target, rows=100_000, cols=10)
    start = time.monotonic()
    aggs = {"col1": ["sum", "mean", "min", "max", "count"]}
    out = _data(ctx.dispatch("csv_aggregate", {"path": "big.csv", "group_by": "col0", "aggregations": aggs}))
    elapsed = time.monotonic() - start
    assert out["group_count"] == 10
    assert out["groups"][0]["col1_count"] == 10_000
    assert elapsed <= 4, f"csv_aggregate on 100k×10 took {elapsed:.2f}s"
