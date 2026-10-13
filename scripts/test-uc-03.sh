#!/usr/bin/env bash
# test-uc-03.sh — UC-03: Dana, data analyst CSV flow (F-010, F-005, F-020).
# Seeds a fresh workspace with a deterministic 20k-row CSV (via the benchmark
# fixture generator) plus the bundled sample image, then answers analyst
# questions through AppContext.dispatch: inspect, page, filter (result saved
# as a workspace resource), aggregate.
set -euo pipefail
cd "$(dirname "$0")/.."

PROTOX_DATA_DIR="${PROTOX_DATA_DIR:-$(mktemp -d /tmp/protox-uc03-data.XXXXXX)}"
PROTOX_WORKSPACE="${PROTOX_WORKSPACE:-$(mktemp -d /tmp/protox-uc03-ws.XXXXXX)}"
export PROTOX_DATA_DIR PROTOX_WORKSPACE PROTOX_TF_API_KEY=""

pass() { printf 'PASS: %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

echo "== UC-03: analyst CSV flow (csv_info / csv_read / csv_filter / csv_aggregate)"

uv run python - "$PROTOX_WORKSPACE" <<'EOF'
import os
import shutil
import sys

sys.path.insert(0, os.getcwd())
from tests.fixtures.make_big_csv import write_big_csv  # noqa: E402

ws = sys.argv[1]
csv_path = write_big_csv(os.path.join(ws, "sales.csv"), rows=20_000, cols=10, seed=42)
shutil.copy2("samples/sample.png", os.path.join(ws, "sample.png"))
print(f"workspace seeded: sales.csv ({csv_path.stat().st_size} bytes) + sample.png")
EOF

uv run python - <<'EOF'
import os

from server.app import AppContext
from server.config import Settings


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"PASS: {msg}")


ctx = AppContext(Settings())
ws = os.environ["PROTOX_WORKSPACE"]
try:
    info = ctx.dispatch("csv_info", {"path": "sales.csv"})["result"]["data"]
    check(info["rows"] == 20_000, f"csv_info rows=20000 (got {info['rows']})")
    check(info["columns"] == [f"col{i}" for i in range(10)], "csv_info reports columns col0..col9")

    page = ctx.dispatch("csv_read", {"path": "sales.csv", "limit": 5})["result"]["data"]
    check(page["row_count"] == 5 and page["truncated"] is True,
          "csv_read limit=5 pages the rows and reports truncated:true (F-020 boundary)")

    filtered = ctx.dispatch(
        "csv_filter",
        {"path": "sales.csv", "column": "col0", "op": "eq", "value": "g3"},
    )["result"]["data"]
    check(filtered["rows"] == 2_000,
          f"csv_filter col0==g3 keeps 2000 rows (got {filtered['rows']})")
    out_rel = filtered["output"]["path"]
    check(out_rel.startswith("out/") and out_rel.endswith("-filtered.csv"),
          f"filter result saved into the workspace (out/…-filtered.csv, got {out_rel})")
    check(os.path.exists(os.path.join(ws, out_rel)), "filtered CSV exists on disk")
    check(filtered["output"]["uri"].startswith("protox-workspace:///"),
          "result carries a protox-workspace:// resource ref (F-005)")

    agg = ctx.dispatch(
        "csv_aggregate",
        {"path": "sales.csv", "group_by": "col0", "aggregations": {"col1": ["sum", "count"]}},
    )["result"]["data"]
    check(agg["group_count"] == 10, f"csv_aggregate spans 10 groups (got {agg['group_count']})")
    counts = {g["col0"]: g["col1_count"] for g in agg["groups"]}
    check(len(counts) == 10 and all(v == 2_000 for v in counts.values()),
          "every group aggregates exactly 2000 rows")

    img = ctx.dispatch("image_info", {"path": "sample.png"})["result"]["data"]
    check(img["width"] == 640 and img["height"] == 480,
          f"sample.png answers via image_info ({img['width']}x{img['height']})")
finally:
    ctx.store.close()
EOF

echo "UC-03 PASS"
