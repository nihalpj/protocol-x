"""Regression tests for the live-usage report bug batch (protocol-x-mcp-usage-report.md §3/§4).

ISS-1  bytes-typed arguments must survive the dispatcher -> sandbox-worker transport.
ISS-2  protobuf_duration_from_seconds must accept fractional seconds.
ISS-3  protobuf_duration_to_json_string must construct a Duration from JSON input.
ISS-4  pydantic_cor_to_json: value is Any, indent is int | None.
ISS-5  charset_norm_is_binary resolves paths against the workspace root only.
ISS-6  tool results keep structure (no repr() fallback strings with addresses).
"""

from __future__ import annotations

import base64
import json

import pytest
from pydantic import BaseModel

from adapters.base import ToolError


# ---------------------------------------------------------------- ISS-1
class _BytesIn(BaseModel):
    input: bytes


def test_bytes_args_roundtrip_through_transport():
    from core.serialization import decode_bytes_args, encode_bytes_args

    validated = _BytesIn.model_validate({"input": "hello"})
    payload = json.dumps({"args": encode_bytes_args(validated.model_dump())})
    worker_args = decode_bytes_args(json.loads(payload)["args"])
    assert worker_args == {"input": b"hello"}


def test_bytes_args_roundtrip_arbitrary_binary():
    from core.serialization import decode_bytes_args, encode_bytes_args

    raw = bytes(range(256))  # not UTF-8 decodable
    payload = json.dumps({"args": encode_bytes_args({"key": raw})})
    assert decode_bytes_args(json.loads(payload)["args"]) == {"key": raw}


def test_bytes_args_roundtrip_nested():
    from core.serialization import decode_bytes_args, encode_bytes_args

    args = {"items": [b"a", {"k": b"b"}], "plain": "text"}
    payload = json.dumps({"args": encode_bytes_args(args)})
    assert decode_bytes_args(json.loads(payload)["args"]) == args


# ---------------------------------------------------------------- ISS-2
def test_duration_from_seconds_fractional():
    from adapters.protobuf import protobuf_duration_from_seconds

    out = protobuf_duration_from_seconds(None, seconds=3661.5)
    assert out == {"result": "3661.500s"}


def test_duration_from_seconds_whole_number_float():
    from adapters.protobuf import protobuf_duration_from_seconds

    out = protobuf_duration_from_seconds(None, seconds=3661.0)
    assert out == {"result": "3661s"}


# ---------------------------------------------------------------- ISS-3
def test_duration_to_json_string_from_dict():
    from adapters.protobuf import protobuf_duration_to_json_string

    out = protobuf_duration_to_json_string(None, duration={"seconds": 90061, "nanos": 250000000})
    assert out == {"result": "90061.250s"}


def test_duration_to_json_string_from_json_duration_string():
    from adapters.protobuf import protobuf_duration_to_json_string

    out = protobuf_duration_to_json_string(None, duration="3.000000001s")
    assert out == {"result": "3.000000001s"}


def test_duration_to_json_string_rejects_unusable_input():
    from adapters.protobuf import protobuf_duration_to_json_string

    with pytest.raises(ToolError):
        protobuf_duration_to_json_string(None, duration=12345)


# ---------------------------------------------------------------- ISS-4
def test_to_json_schema_accepts_int_indent_and_any_value():
    from adapters.pydantic_core import PydanticCorToJsonIn

    m = PydanticCorToJsonIn.model_validate({"value": {"a": 1}, "indent": 2})
    assert m.value == {"a": 1}
    assert m.indent == 2


def test_to_json_does_not_double_encode():
    from adapters.pydantic_core import pydantic_cor_to_json

    out = pydantic_cor_to_json(
        None,
        value={"service": "protocol-x", "ok": True},
        indent=None,
        ensure_ascii=False,
        include=None,
        exclude=None,
        by_alias=True,
    )
    assert json.loads(out["result"]) == {"service": "protocol-x", "ok": True}


def test_to_json_pretty_prints_with_int_indent():
    from adapters.pydantic_core import pydantic_cor_to_json

    out = pydantic_cor_to_json(
        None, value={"a": 1}, indent=2, ensure_ascii=False,
        include=None, exclude=None, by_alias=True,
    )
    assert out["result"] == '{\n  "a": 1\n}'


# ---------------------------------------------------------------- ISS-5
_IS_BINARY_DEFAULTS = dict(steps=5, chunk_size=512, threshold=0.2, cp_isolation=None, cp_exclusion=None)


def test_is_binary_resolves_workspace_relative_path(tmp_path):
    from adapters.charset_normalizer import charset_norm_is_binary
    from core.workspace import Workspace

    ws = Workspace(tmp_path / "ws")
    (ws.root / "notes.txt").write_text("plain utf-8 text, definitely not binary")
    out = charset_norm_is_binary(ws, fp_or_path_or_payload="notes.txt", **_IS_BINARY_DEFAULTS)
    assert out == {"result": False}


def test_is_binary_accepts_absolute_path_inside_workspace(tmp_path):
    from adapters.charset_normalizer import charset_norm_is_binary
    from core.workspace import Workspace

    ws = Workspace(tmp_path / "ws")
    target = ws.root / "notes.txt"
    target.write_text("plain utf-8 text")
    out = charset_norm_is_binary(ws, fp_or_path_or_payload=str(target), **_IS_BINARY_DEFAULTS)
    assert out == {"result": False}


def test_is_binary_rejects_absolute_path_outside_workspace(tmp_path):
    from adapters.charset_normalizer import charset_norm_is_binary
    from core.workspace import Workspace

    ws = Workspace(tmp_path / "ws")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside the workspace")
    with pytest.raises(ToolError) as exc:
        charset_norm_is_binary(ws, fp_or_path_or_payload=str(outside), **_IS_BINARY_DEFAULTS)
    assert exc.value.code == "VALIDATION_ERROR"


def test_is_binary_rejects_dotdot_escape(tmp_path):
    from adapters.charset_normalizer import charset_norm_is_binary
    from core.workspace import Workspace

    ws = Workspace(tmp_path / "ws")
    with pytest.raises(ToolError) as exc:
        charset_norm_is_binary(ws, fp_or_path_or_payload="../outside.txt", **_IS_BINARY_DEFAULTS)
    assert exc.value.code == "VALIDATION_ERROR"


# ---------------------------------------------------------------- ISS-6
def _wrap(result):
    from adapters.base import wrap_tool_result

    return wrap_tool_result(result)


def test_result_primitives_pass_through():
    assert _wrap({"x": [1, "a", None, True]}) == {"result": {"x": [1, "a", None, True]}}


def test_result_bytes_utf8_decoded():
    assert _wrap(b"xn--mnchen-3ya") == {"result": "xn--mnchen-3ya"}


def test_result_bytes_non_utf8_base64_tagged():
    raw = b"\xff\xfe\x81\x80"  # invalid UTF-8 start bytes
    assert _wrap(raw) == {"result": {"_bytes_b64": base64.b64encode(raw).decode()}}


def test_result_tuple_and_frozenset_become_lists():
    out = _wrap(("requests", frozenset({"py3-none-any"})))
    assert out == {"result": ["requests", ["py3-none-any"]]}


def test_result_nested_bytes_inside_containers():
    out = _wrap((b"body", "multipart/form-data"))
    assert out == {"result": ["body", "multipart/form-data"]}


def test_result_object_repr_is_stable_and_typed():
    class Thing:
        pass

    out = _wrap({"alg": Thing()})
    entry = out["result"]["alg"]
    assert entry["_type"].endswith("Thing")
    assert "0x" not in entry["_repr"]


def test_result_http_response_like_objects_keep_special_case():
    class Resp:
        status_code = 200
        headers = {"content-type": "text/plain"}
        text = "hello"

    assert _wrap(Resp()) == {
        "result": {"status_code": 200, "headers": {"content-type": "text/plain"}, "text": "hello"}
    }
