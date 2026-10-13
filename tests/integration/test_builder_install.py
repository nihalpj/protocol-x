"""Server-side package install: validation, runner, endpoint, audit trail."""

from __future__ import annotations

import importlib.metadata
import subprocess

import pytest

from server.installer import InstallError, install_package


def test_rejects_invalid_names():
    for bad in ["", "http://evil", "foo;rm -rf /", "-e .", "foo bar", "../x", "a" * 300]:
        with pytest.raises(InstallError, match="invalid package name"):
            install_package(bad)


def test_already_installed_short_circuits():
    result = install_package("pip")  # always present in the dev venv
    assert result.already_installed is True
    assert result.version == importlib.metadata.version("pip")


def test_failed_install_surfaces_output(monkeypatch):
    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "line1\nline2\nerror: no matching distribution"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProc())
    with pytest.raises(InstallError) as exc:
        install_package("definitely-not-a-real-pkg-xyz")
    assert "no matching distribution" in exc.value.detail


def test_timeout_maps_to_install_error(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="uv", timeout=1)

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(InstallError, match="timed out"):
        install_package("somepkg", timeout_s=1)


async def test_install_endpoint_audit_and_validation(ctx):
    """POST /api/builder/install: bad name 400; success path audit-logged."""
    from server.api import register_api_routes
    from server.app import create_mcp

    mcp = create_mcp(ctx)
    register_api_routes(mcp, ctx)
    from starlette.testclient import TestClient

    with TestClient(mcp.http_app()) as client:
        bad = client.post("/api/builder/install", json={"package": "foo;evil"})
        assert bad.status_code == 400
        assert "invalid package name" in bad.json()["message"]

        ok = client.post("/api/builder/install", json={"package": "pip"})
        assert ok.status_code == 200
        body = ok.json()
        assert body["state"] == "installed"
        assert body["already_installed"] is True

    events = ctx.store.audit.query(kind="install")
    assert len(events) == 1
    assert events[0]["payload"]["package"] == "pip"
    assert events[0]["actor"] == "operator"
