"""AC-F005: result serialization caps and persistence."""

from __future__ import annotations

import base64

import pytest

from adapters.base import RESULT_TOO_LARGE, ToolError
from core import serialization
from core.serialization import binary_to_wire, normalize_result
from core.workspace import Workspace
from adapters.base import BinaryResult


def test_json_inline(tmp_path):
    ws = Workspace(tmp_path / "ws")
    out = normalize_result({"a": 1}, ws)
    assert out == {"data": {"a": 1}}


def test_binary_base64_inline(tmp_path):
    ws = Workspace(tmp_path / "ws")
    wire = binary_to_wire(BinaryResult(b"png-bytes", "image/png", "x.png"))
    out = normalize_result(wire, ws)
    assert out["mime"] == "image/png"
    assert base64.b64decode(out["data_b64"]) == b"png-bytes"


def test_over_1mb_persisted(tmp_path):
    ws = Workspace(tmp_path / "ws")
    big = {"blob": "x" * (2 * 1024 * 1024)}
    out = normalize_result(big, ws)
    assert out["persisted"] is True
    assert out["resource"]["size_bytes"] > 1024 * 1024


def test_binary_over_1mb_persisted(tmp_path):
    ws = Workspace(tmp_path / "ws")
    raw = b"\x01" * (2 * 1024 * 1024)
    wire = binary_to_wire(BinaryResult(raw, "image/png", "big.png"))
    out = normalize_result(wire, ws)
    assert out["persisted"] is True
    assert out["resource"]["mime"] == "image/png"


def test_over_20mb_rejected(tmp_path, monkeypatch):
    ws = Workspace(tmp_path / "ws")
    monkeypatch.setattr(serialization, "HARD_CAP_BYTES", 1024)
    with pytest.raises(ToolError) as exc:
        normalize_result({"blob": "x" * 4096}, ws)
    assert exc.value.code == RESULT_TOO_LARGE
