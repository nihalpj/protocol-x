"""ISS-7: converter must not ship tools that need non-JSON-native live objects.

A function whose *required* parameter cannot cross the JSON boundary (callable,
type object, protobuf message, ...) can never be invoked successfully — the
converter must reject the function instead of silently dropping the parameter
and shipping a tool that always fails (or worse, returns an empty-but-"ok"
answer). Optional such params may still be dropped, since the package default
applies.
"""

from __future__ import annotations

import sys
import types
from typing import Callable

import pytest

from agents import codegen


def _fake_module(monkeypatch: pytest.MonkeyPatch, name: str, **members) -> None:
    mod = types.ModuleType(name)
    for key, value in members.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, name, mod)


def test_required_callable_param_rejects_function(monkeypatch):
    def needs_callback(data: str, cb: Callable) -> str:
        return cb(data)

    _fake_module(monkeypatch, "protox_fake_cb", needs_callback=needs_callback)
    names = [c.name for c in codegen.introspect_package("protox_fake_cb")]
    assert "needs_callback" not in names


def test_optional_callable_param_is_dropped_not_rejected(monkeypatch):
    def optional_callback(data: str, cb: Callable = None) -> str:  # noqa: ANN001
        return data

    _fake_module(monkeypatch, "protox_fake_opt", optional_callback=optional_callback)
    candidates = {c.name: c for c in codegen.introspect_package("protox_fake_opt")}
    assert "optional_callback" in candidates
    cand = candidates["optional_callback"]
    assert [a["name"] for a in cand.args] == ["data"]
    assert "cb" in cand.skipped


def test_required_type_object_param_rejects_function(monkeypatch):
    def get_args(tp: type) -> str:
        return str(tp)

    _fake_module(monkeypatch, "protox_fake_type", get_args=get_args)
    names = [c.name for c in codegen.introspect_package("protox_fake_type")]
    assert "get_args" not in names


def test_denylisted_target_is_not_a_candidate(monkeypatch):
    def replace_params(path: str, param_convertors: dict, path_params: dict) -> str:
        return path

    _fake_module(monkeypatch, "protox_fake_deny", replace_params=replace_params)
    monkeypatch.setitem(
        codegen._NOT_CONVERTIBLE_TARGETS, "protox_fake_deny", {"replace_params"}
    )
    names = [c.name for c in codegen.introspect_package("protox_fake_deny")]
    assert "replace_params" not in names
