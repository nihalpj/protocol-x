"""Content hashing shared across layers (registry, builder, converter, repair).

Lives in core so agents/ never needs to import server/ (import-linter
layering contract: server -> agents -> core -> adapters).
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def module_source_hash(adapters_dir: Path, module: str) -> str:
    """sha256 of adapters/<module>.py — the version identity (F-030 chain)."""
    path = adapters_dir / f"{module}.py"
    return hashlib.sha256(path.read_bytes()).hexdigest()
