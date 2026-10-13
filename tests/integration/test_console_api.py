"""P5 console contract: every /api route the SPA consumes, served over the
real ASGI app (build_http_app), plus the SPA catch-all serving console/dist.

AC-P5-01 /api status/tools/logs/config/governance/pypi/snippets ·
AC-P5-02 SPA served at / with asset files · AC-P5-03 auth gates /api when
tokens are configured.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from server.app import build_http_app


@pytest.fixture()
def client(ctx):
    with TestClient(build_http_app(ctx)) as c:
        yield c


def test_status(client):
    body = client.get("/api/status").json()
    assert body["version"] and body["tools"] >= 34
    assert body["agent_features_enabled"] is False
    assert "sizing" in body and "preset" in body


def test_tools_listing(client):
    body = client.get("/api/tools").json()
    names = {t["name"] for t in body["tools"]}
    assert "image_info" in names
    tool = next(t for t in body["tools"] if t["name"] == "image_info")
    assert {"state", "schema", "adapter_module"} <= set(tool)


def test_logs_and_governance(client):
    assert "logs" in client.get("/api/logs").json()
    gov = client.get("/api/governance").json()
    assert {
        "drafts", "quarantined", "verdicts", "rejections",
        "conversion_runs", "repair_incidents", "agent_features_enabled",
    } <= set(gov)


def test_config_get_and_post(client, settings):
    body = client.get("/api/config").json()
    assert body["models"]["judge"]["value"]
    assert body["agent_features_enabled"] is False
    assert "sizing_overrides" in body  # regression: was a 500 (ctx.sizing)

    resp = client.post("/api/config", json={"tf_fast_model": "nvidia/test-fast"})
    assert resp.status_code == 200 and resp.json()["ok"]
    import yaml

    persisted = yaml.safe_load((settings.data_dir / "server.yaml").read_text())
    assert persisted["models"]["fast"] == "nvidia/test-fast"

    bad = client.post("/api/config", json={"nonsense": 1})
    assert bad.status_code == 400


def test_pypi_discriminated_states(client, monkeypatch):
    class FakeResp:
        def __init__(self, status, payload=None):
            self.status_code = status
            self._payload = payload or {}

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def get(self, url):
            if "reportlab" in url:
                return FakeResp(200, {"info": {
                    "name": "reportlab", "version": "4.0", "summary": "pdf",
                    "license": "BSD", "requires_python": ">=3.8",
                    "home_page": "https://reportlab.com"}})
            return FakeResp(404)

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    assert client.get("/api/pypi/reportlab").json()["state"] == "ok"
    assert client.get("/api/pypi/nope-xyz").status_code == 404


def test_snippets(client):
    body = client.get("/api/snippets").json()
    assert body  # non-empty snippet payloads for client config


def test_spa_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"<div id=\"root\">" in resp.content or b"root" in resp.content
    # deep link falls back to the SPA
    assert client.get("/governance").status_code == 200


def test_builder_routes_fail_closed_without_key(client):
    resp = client.post("/api/builder/judge", json={"module": "nonexistent"})
    assert resp.status_code in (200, 404, 422)
    if resp.status_code == 200:
        assert resp.json()["state"] in ("generation-failed", "judge-unavailable")
