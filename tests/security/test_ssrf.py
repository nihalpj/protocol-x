"""SSRF battery for the HTTP adapter guard (F-009 / INV-6).

The guard must block before any connection is opened. Blocked cases raise
ToolError(SSRF_BLOCKED) both in-process and inside the sandboxed worker
(env vars cannot reach the scrubbed worker, so allowlist-trusted cases are
exercised in-process only; every blocking case here needs no env at all,
except the redirect-hop tests which trust the mock origin via ALLOWED_HOSTS
and prove the hop target is re-validated independently).
"""

from __future__ import annotations

import ast
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from adapters import http_requests
from adapters.base import ADAPTER_ERROR, SSRF_BLOCKED, VALIDATION_ERROR, ToolError
from core.workspace import Workspace


@pytest.fixture()
def ws(tmp_path):
    return Workspace(tmp_path / "ws")


# -- private address battery ---------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.1:8080/admin",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://0.0.0.0/",
        "http://[::1]/",
        "http://[fe80::1]/",
    ],
)
def test_private_addresses_blocked(ws, url):
    with pytest.raises(ToolError) as exc:
        http_requests.http_get(ws, url=url, timeout_s=2)
    assert exc.value.code == SSRF_BLOCKED


def test_private_address_details_report_ip_and_reasons(ws):
    with pytest.raises(ToolError) as exc:
        http_requests.http_get(ws, url="http://169.254.169.254/latest/meta-data/", timeout_s=2)
    assert exc.value.code == SSRF_BLOCKED
    assert exc.value.details["ip"] == "169.254.169.254"
    assert "link-local" in exc.value.details["reasons"]


@pytest.mark.parametrize("tool", ["http_get", "http_post", "http_head", "http_download_file"])
def test_all_tools_enforce_the_guard(ws, tool):
    with pytest.raises(ToolError) as exc:
        getattr(http_requests, tool)(ws, url="http://192.168.1.1/", timeout_s=2)
    assert exc.value.code == SSRF_BLOCKED


def test_dispatch_blocks_loopback_inside_worker(ctx):
    # no env needed: loopback is refused by the IP policy itself
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("http_get", {"url": "http://127.0.0.1:8080/", "timeout_s": 2})
    assert exc.value.code == SSRF_BLOCKED


# -- scheme rejection ------------------------------------------------------------


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://127.0.0.1/", "ftp://example.com/x"])
def test_non_http_schemes_blocked(ws, url):
    with pytest.raises(ToolError) as exc:
        http_requests.http_get(ws, url=url, timeout_s=2)
    assert exc.value.code == SSRF_BLOCKED


# -- ALLOWED_HOSTS egress allowlist ----------------------------------------------


def test_allowlist_rejects_unlisted_host(ws, monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "example.com")
    with pytest.raises(ToolError) as exc:
        http_requests.http_get(ws, url="http://127.0.0.1:8080/", timeout_s=2)
    assert exc.value.code == SSRF_BLOCKED
    assert "ALLOWED_HOSTS" in exc.value.message


def test_allowlist_permits_listed_host(ws, monkeypatch):
    # operator-trusted host bypasses the private-IP block (localhost mock origin)
    monkeypatch.setenv("ALLOWED_HOSTS", "127.0.0.1")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    server.routes = {("GET", "/ok"): (200, {}, b"fine")}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/ok"
        result = http_requests.http_get(ws, url=url, timeout_s=5)
        assert result["status"] == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# -- redirect hop revalidation ---------------------------------------------------


class _RedirectHandler(BaseHTTPRequestHandler):
    def log_message(self, *fmt_args):
        pass

    def do_GET(self):
        route = getattr(self.server, "routes", {}).get(("GET", self.path))
        if route is None:
            self.send_error(404, "not found")
            return
        status, headers, payload = route
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)


@pytest.fixture()
def redirect_server(monkeypatch):
    """Mock origin on loopback, operator-trusted; redirect targets are not."""
    monkeypatch.setenv("ALLOWED_HOSTS", "127.0.0.1")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    server.routes = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _base(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def test_redirect_to_private_ip_literal_blocked_at_hop(ws, redirect_server):
    redirect_server.routes[("GET", "/redir")] = (302, {"Location": "http://10.0.0.5/internal"}, b"")
    with pytest.raises(ToolError) as exc:
        http_requests.http_get(ws, url=f"{_base(redirect_server)}/redir", timeout_s=5)
    assert exc.value.code == SSRF_BLOCKED
    assert exc.value.details["host"] == "10.0.0.5"


def test_redirect_to_loopback_hostname_blocked_at_hop(ws, redirect_server):
    # origin host is allowlist-trusted; the hop target hostname is not
    redirect_server.routes[("GET", "/redir")] = (
        302,
        {"Location": f"http://localhost:{redirect_server.server_address[1]}/final"},
        b"",
    )
    redirect_server.routes[("GET", "/final")] = (200, {}, b"should never be fetched")
    with pytest.raises(ToolError) as exc:
        http_requests.http_get(ws, url=f"{_base(redirect_server)}/redir", timeout_s=5)
    assert exc.value.code == SSRF_BLOCKED


def test_redirect_to_metadata_ip_blocked_at_hop(ws, redirect_server):
    redirect_server.routes[("GET", "/redir")] = (
        302,
        {"Location": "http://169.254.169.254/latest/meta-data/"},
        b"",
    )
    with pytest.raises(ToolError) as exc:
        http_requests.http_get(ws, url=f"{_base(redirect_server)}/redir", timeout_s=5)
    assert exc.value.code == SSRF_BLOCKED
    # blocked at the hop (allowlist stage: fixture trusts only the origin host)
    assert exc.value.details["host"] == "169.254.169.254"


def test_redirect_to_file_scheme_blocked_at_hop(ws, redirect_server):
    redirect_server.routes[("GET", "/redir")] = (302, {"Location": "file:///etc/passwd"}, b"")
    with pytest.raises(ToolError) as exc:
        http_requests.http_get(ws, url=f"{_base(redirect_server)}/redir", timeout_s=5)
    assert exc.value.code == SSRF_BLOCKED


def test_too_many_redirects(ws, redirect_server):
    for i in range(6):
        redirect_server.routes[("GET", f"/r{i}")] = (302, {"Location": f"/r{i + 1}"}, b"")
    redirect_server.routes[("GET", "/r6")] = (200, {}, b"end")
    with pytest.raises(ToolError) as exc:
        http_requests.http_get(ws, url=f"{_base(redirect_server)}/r0", timeout_s=5)
    assert exc.value.code == ADAPTER_ERROR
    assert "redirect" in exc.value.message


# -- clean error mapping ---------------------------------------------------------


def test_dns_failure_is_a_clean_adapter_error(ws):
    with pytest.raises(ToolError) as exc:
        http_requests.http_get(ws, url="http://protox-nonexistent-8f3a2.invalid/", timeout_s=2)
    assert exc.value.code == ADAPTER_ERROR
    assert "Traceback" not in exc.value.message


def test_crlf_in_header_rejected(ws, monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "127.0.0.1")  # let url validation pass to header validation
    with pytest.raises(ToolError) as exc:
        http_requests.http_get(
            ws,
            url="http://127.0.0.1/",
            headers={"X-Evil": "a\r\nHost: other"},
        )
    assert exc.value.code == VALIDATION_ERROR


# -- CI import audit -------------------------------------------------------------


_NETWORK_MODULES = {"socket", "http", "ssl", "urllib", "requests", "httpx"}
_ALLOWED_OFFENDERS = {"http_requests.py", "_urlguard.py"}


def test_network_imports_confined_to_http_adapter():
    """Only the HTTP adapter + its guard may import network modules.

    Scans exactly the modules the registry would load: `_`-prefixed helpers
    and `*.draft.py` (unmerged, unjudged, never registered) are excluded.
    """
    adapters_dir = Path(__file__).resolve().parents[2] / "adapters"
    offenders = []
    for path in sorted(adapters_dir.glob("*.py")):
        if path.name.startswith("_") or path.name.endswith(".draft.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.update(node.module.split(".")[0].split())
        bad = imported & _NETWORK_MODULES
        if bad and path.name not in _ALLOWED_OFFENDERS:
            offenders.append((path.name, sorted(bad)))
    assert offenders == []
