"""Deterministic big-CSV generator for benchmarks.

Writes a 100k-row × 10-col CSV (default) seeded for reproducibility:
col0 is a 10-valued group label (g0..g9), col1..col9 are random ints.
Used by the csv_aggregate timing test; usable standalone via __main__.
"""

from __future__ import annotations

import csv
import random
from pathlib import Path

GROUPS = 10


def write_big_csv(path: str | Path, rows: int = 100_000, cols: int = 10, seed: int = 42) -> Path:
    path = Path(path)
    rng = random.Random(seed)
    header = [f"col{i}" for i in range(cols)]
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for i in range(rows):
            writer.writerow([f"g{i % GROUPS}", *[rng.randint(0, 1_000_000) for _ in range(cols - 1)]])
    return path


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "big.csv"
    out = write_big_csv(target)
    print(f"wrote {out} ({out.stat().st_size} bytes)")
