"""Shared size guards + magic-byte validation (F-020).

Declarative per-tool limits live in ToolSpec.limits and descriptions; these
helpers enforce them inside handlers. Clamps return `truncated: true` (with
applied-vs-actual values); hard violations raise typed *_TOO_LARGE errors.
Boundary-inclusive: exactly-at-limit inputs succeed (AC-F020-03).
"""

from __future__ import annotations

from pathlib import Path

from adapters.base import (
    CORRUPT_FILE,
    INPUT_TOO_LARGE,
    RANGE_TOO_LARGE,
    ToolError,
)

# Magic bytes per format family (corrupt fixtures must fail BEFORE parsing).
MAGICS: dict[str, tuple[bytes, ...]] = {
    "pdf": (b"%PDF",),
    "zip": (b"PK\x03\x04", b"PK\x05\x06"),  # xlsx / docx are ZIP containers
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpeg": (b"\xff\xd8\xff",),
}


def check_magic(path: str | Path, family: str) -> None:
    with open(path, "rb") as fh:
        head = fh.read(8)
    if not any(head.startswith(m) for m in MAGICS[family]):
        raise ToolError(
            CORRUPT_FILE,
            f"file does not match {family} magic bytes",
            {"path": str(path), "expected": family},
        )


def check_input_size(path: str | Path, cap_bytes: int) -> None:
    """Reject oversize inputs BEFORE parsing (AC-F010-02)."""
    size = Path(path).stat().st_size
    if size > cap_bytes:
        raise ToolError(
            INPUT_TOO_LARGE,
            "input file exceeds size cap",
            {"size_bytes": size, "cap_bytes": cap_bytes},
        )


def check_range(count: int, cap: int, what: str) -> None:
    """Reject an over-cap range, reporting the computed count (AC-F011-01)."""
    if count > cap:
        raise ToolError(
            RANGE_TOO_LARGE,
            f"{what} exceeds limit",
            {"requested": count, "limit": cap, "unit": what},
        )


def clamp_page_range(start: int, end: int, total: int) -> tuple[int, int, bool]:
    """Clamp a 1-based inclusive page range to the document; boundary-inclusive."""
    actual_end = min(end, total)
    truncated = actual_end < end or start > total
    if start > total:
        return 1, total, True
    return start, actual_end, truncated
