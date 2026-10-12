"""Workspace confinement (F-004, INV-5).

All tool file I/O resolves under a single declared root. `..` escapes,
foreign absolute paths, and symlinks pointing outside the root are rejected.
Instances are created by the dispatcher/worker and passed into handlers;
adapters use the object without importing this module (layer rule).
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path

from adapters.base import VALIDATION_ERROR, ToolError


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, rel: str, *, must_exist: bool = False) -> Path:
        """Resolve a caller-supplied path under the root, rejecting escapes."""
        if not rel or rel.strip("\x00") != rel:
            raise ToolError(VALIDATION_ERROR, "invalid path", {"path": rel})
        candidate = Path(rel)
        if candidate.is_absolute():
            # Absolute paths are only acceptable if already under the root.
            try:
                candidate.resolve().relative_to(self.root)
            except ValueError:
                raise ToolError(
                    VALIDATION_ERROR, "absolute path outside workspace", {"path": rel}
                ) from None
            resolved = candidate.resolve()
        else:
            resolved = (self.root / candidate).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise ToolError(VALIDATION_ERROR, "path escapes workspace", {"path": rel}) from None
        if must_exist and not resolved.exists():
            raise ToolError(VALIDATION_ERROR, "file not found in workspace", {"path": rel})
        return resolved

    def new_temp_dir(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="protox-", dir=self.root))

    def new_output_path(self, suggested_name: str) -> Path:
        """Unique output path under the root for a tool result file."""
        name = Path(suggested_name).name or "output.bin"
        out_dir = self.root / "out"
        out_dir.mkdir(exist_ok=True)
        return out_dir / f"{uuid.uuid4().hex[:12]}-{name}"

    def cleanup_call(self, call_dir: str | Path) -> None:
        """Per-call temp lifecycle (INV-5): remove a temp dir created for one call."""
        path = Path(call_dir)
        try:
            path.resolve().relative_to(self.root)
        except ValueError:
            return
        if path.is_dir() and path.name.startswith("protox-"):
            shutil.rmtree(path, ignore_errors=True)

    def resource_ref(self, path: str | Path, mime: str | None = None) -> dict:
        """Resource reference for a persisted workspace file (F-005)."""
        p = Path(path).resolve()
        rel = p.relative_to(self.root)
        ref: dict = {
            "type": "resource",
            "uri": f"protox-workspace:///{rel.as_posix()}",
            "path": rel.as_posix(),
            "size_bytes": p.stat().st_size,
        }
        if mime:
            ref["mime"] = mime
        return ref
