"""Integration tests for the HTTP adapter (http_requests).

Local mock HTTP servers run in threads. The SSRF guard blocks loopback unless
the host is operator-trusted via ALLOWED_HOSTS; the worker environment is
scrubbed (env vars cannot reach it), so happy paths call the handlers
in-process with ALLOWED_HOSTS set, while dispatch-level behavior (schema
validation, guard blocking inside the sandbox) is covered here and in
tests/security/test_ssrf.py.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from adapters import _urlguard, http_requests
from adapters.base import RESULT_TOO_LARGE, VALIDATION_ERROR, ToolError
from core.workspace import Workspace

INLINE_CAP = 256 * 1024


class _MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *fmt_args):  # keep test output quiet
        pass

    def _dispatch(self, method: str) -> None:
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            self.server.last_post_body = self.rfile.read(length)
            self.server.last_post_headers = {k: v for k, v in self.headers.items()}
        route = getattr(self.server, "routes", {}).get((method, self.path))
        if route is None:
            self.send_error(404, "not found")
            return
        status, headers, payload = route
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if method != "HEAD" and payload:
            self.wfile.write(payload)

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")


@pytest.fixture()
def ws(tmp_path):
    return Workspace(tmp_path / "ws")


@pytest.fixture()
def mock_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
    server.routes = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _trust_localhost(mock_server, monkeypatch):
    """Operator opt-in: the mock server host is ALLOWED_HOSTS-trusted."""
    monkeypatch.setenv("ALLOWED_HOSTS", "127.0.0.1,localhost")


def _base(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


# -- happy paths ---------------------------------------------------------------


def test_get_returns_status_headers_body(ws, mock_server):
    mock_server.routes[("GET", "/hello?x=1")] = (
        200,
        {"Content-Type": "application/json"},
        b'{"ok": true}',
    )
    url = f"{_base(mock_server)}/hello?x=1"
    result = http_requests.http_get(ws, url=url)
    assert result["status"] == 200
    assert result["body"] == '{"ok": true}'
    assert result["headers"]["Content-Type"] == "application/json"
    assert result["url"] == url
    assert result["truncated"] is False
    assert result["hops"] == 0


def test_get_non_2xx_is_a_result_not_an_error(ws, mock_server):
    mock_server.routes[("GET", "/missing")] = (404, {"X-Reason": "gone"}, b"nope")
    result = http_requests.http_get(ws, url=f"{_base(mock_server)}/missing")
    assert result["status"] == 404
    assert result["body"] == "nope"
    assert result["headers"]["X-Reason"] == "gone"


def test_head_returns_headers_without_body(ws, mock_server):
    # Content-Length advertises a would-be body (like real servers do for HEAD)
    # but nothing is written; the handler must not wait for it.
    mock_server.routes[("HEAD", "/h")] = (200, {"X-Exists": "1"}, b"would-be-body")
    result = http_requests.http_head(ws, url=f"{_base(mock_server)}/h")
    assert result["status"] == 200
    assert result["body"] == ""
    assert result["headers"]["X-Exists"] == "1"


def test_post_json_body(ws, mock_server):
    mock_server.routes[("POST", "/submit")] = (200, {}, b"accepted")
    result = http_requests.http_post(ws, url=f"{_base(mock_server)}/submit", json_body={"a": 1})
    assert result["status"] == 200
    assert json.loads(mock_server.last_post_body) == {"a": 1}
    assert mock_server.last_post_headers["Content-Type"] == "application/json"


def test_post_raw_body_with_custom_headers(ws, mock_server):
    mock_server.routes[("POST", "/raw")] = (200, {}, b"ok")
    result = http_requests.http_post(
        ws,
        url=f"{_base(mock_server)}/raw",
        body="raw-payload",
        headers={"X-Custom": "yes"},
    )
    assert result["status"] == 200
    assert mock_server.last_post_body == b"raw-payload"
    assert mock_server.last_post_headers.get("X-Custom") == "yes"


def test_post_body_and_json_body_conflict(ws, mock_server):
    with pytest.raises(ToolError) as exc:
        http_requests.http_post(
            ws,
            url=f"{_base(mock_server)}/raw",
            body="x",
            json_body={"a": 1},
        )
    assert exc.value.code == VALIDATION_ERROR


def test_get_truncates_body_past_256kb(ws, mock_server):
    mock_server.routes[("GET", "/big")] = (200, {}, b"x" * 300_000)
    result = http_requests.http_get(ws, url=f"{_base(mock_server)}/big")
    assert result["truncated"] is True
    assert len(result["body"]) == INLINE_CAP
    assert result["body_actual_bytes"] == 300_000
    assert result["body_limit_bytes"] == INLINE_CAP


def test_get_exactly_at_inline_cap_is_not_truncated(ws, mock_server):
    # boundary-inclusive: exactly 256 KB fits
    mock_server.routes[("GET", "/edge")] = (200, {}, b"y" * INLINE_CAP)
    result = http_requests.http_get(ws, url=f"{_base(mock_server)}/edge")
    assert result["truncated"] is False
    assert len(result["body"]) == INLINE_CAP
    assert result["body_actual_bytes"] == INLINE_CAP


def test_redirect_followed_and_reported(ws, mock_server):
    mock_server.routes[("GET", "/start")] = (302, {"Location": "/final"}, b"")
    mock_server.routes[("GET", "/final")] = (200, {}, b"done")
    result = http_requests.http_get(ws, url=f"{_base(mock_server)}/start")
    assert result["status"] == 200
    assert result["body"] == "done"
    assert result["hops"] == 1
    assert result["url"].endswith("/final")


# -- download ------------------------------------------------------------------


def test_download_file_streams_to_workspace(ws, mock_server):
    payload = bytes(range(256)) * 4096  # 1 MB
    mock_server.routes[("GET", "/file.bin")] = (
        200,
        {"Content-Type": "application/octet-stream"},
        payload,
    )
    result = http_requests.http_download_file(ws, url=f"{_base(mock_server)}/file.bin")
    ref = result["output"]
    assert ref["mime"] == "application/octet-stream"
    assert result["size_bytes"] == len(payload)
    assert (ws.root / ref["path"]).read_bytes() == payload


def test_download_honors_caller_out_path(ws, mock_server):
    mock_server.routes[("GET", "/a.txt")] = (200, {"Content-Type": "text/plain"}, b"abc")
    result = http_requests.http_download_file(
        ws, url=f"{_base(mock_server)}/a.txt", out_path="custom/nested/a.txt"
    )
    ref = result["output"]
    assert ref["path"] == "custom/nested/a.txt"
    assert (ws.root / "custom/nested/a.txt").read_bytes() == b"abc"


def test_download_aborts_over_cap_and_deletes_partial(ws, mock_server, monkeypatch):
    monkeypatch.setattr(_urlguard, "MAX_DOWNLOAD_BYTES", 1024)  # shrink cap: same code path as 200 MB
    mock_server.routes[("GET", "/big.bin")] = (200, {}, b"z" * 4096)
    with pytest.raises(ToolError) as exc:
        http_requests.http_download_file(ws, url=f"{_base(mock_server)}/big.bin")
    assert exc.value.code == RESULT_TOO_LARGE
    out_dir = ws.root / "out"
    assert not out_dir.exists() or not any(out_dir.iterdir())  # partial file deleted


# -- schema + dispatch level ---------------------------------------------------


def test_extra_arg_rejected_by_schema(ctx):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("http_get", {"url": "http://127.0.0.1/", "bogus": 1})
    assert exc.value.code == VALIDATION_ERROR


def test_out_of_range_port_rejected_by_schema(ctx):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("http_get", {"url": "http://127.0.0.1:70000/"})
    assert exc.value.code == VALIDATION_ERROR


def test_bad_timeout_rejected_by_schema(ctx):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("http_get", {"url": "http://127.0.0.1/", "timeout_s": 0})
    assert exc.value.code == VALIDATION_ERROR


def test_tools_registered_with_network_flag(ctx):
    for name in ("http_get", "http_post", "http_head", "http_download_file"):
        entry = ctx.registry.catalog[name]
        assert entry.network is True
        assert entry.state == "live"
