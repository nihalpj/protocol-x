"""AC-P1-04 / AC-F004: workspace confinement."""

from __future__ import annotations

import pytest

from adapters.base import ToolError
from core.workspace import Workspace


def test_parent_escape_rejected(tmp_path):
    ws = Workspace(tmp_path / "ws")
    with pytest.raises(ToolError) as exc:
        ws.resolve("../../etc/passwd")
    assert exc.value.code == "VALIDATION_ERROR"


def test_foreign_absolute_rejected(tmp_path):
    ws = Workspace(tmp_path / "ws")
    with pytest.raises(ToolError) as exc:
        ws.resolve("/etc/passwd")
    assert exc.value.code == "VALIDATION_ERROR"


def test_symlink_escape_rejected(tmp_path):
    ws = Workspace(tmp_path / "ws")
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    (ws.root / "link.txt").symlink_to(outside)
    with pytest.raises(ToolError):
        ws.resolve("link.txt")


def test_valid_relative_and_write(tmp_path):
    ws = Workspace(tmp_path / "ws")
    target = ws.resolve("a/b.txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hi")
    assert target.read_text() == "hi"
    ref = ws.resource_ref(target, "text/plain")
    assert ref["uri"].startswith("protox-workspace:///")
    assert ref["size_bytes"] == 2


def test_temp_dir_lifecycle(tmp_path):
    ws = Workspace(tmp_path / "ws")
    call_dir = ws.new_temp_dir()
    assert call_dir.is_dir()
    ws.cleanup_call(call_dir)
    assert not call_dir.exists()
