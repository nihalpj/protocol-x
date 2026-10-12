"""SSRF guard for the HTTP adapter (F-009 seam).

Every request — including every redirect hop — is validated BEFORE any bytes
of that hop's body are read:

1. Scheme allowlist: http/https only (file://, gopher://, ... -> SSRF_BLOCKED).
2. Optional egress allowlist: env ALLOWED_HOSTS (comma-separated hostnames),
   read at call time inside the worker. When set, only listed hosts may be
   fetched (SSRF_BLOCKED otherwise); listed hosts are operator-trusted and
   bypass stage 3 so intranet/dev hosts (e.g. localhost test servers) can be
   reached deliberately. When unset, stage 3 applies to every host.
3. DNS resolution via socket.getaddrinfo; the request is refused if ANY
   resolved address is private/loopback/link-local/reserved/multicast/
   unspecified (ipaddress) — incl. 169.254.169.254 cloud metadata.
4. Connect-by-IP with Host pinning: the TCP/TLS connection goes to the
   validated IP while the Host header carries the original hostname. HTTPS
   uses a pinned-connection subclass that passes the original hostname as the
   SNI server_hostname for certificate verification.
5. Redirects are followed manually (max 5 hops) and each hop restarts at
   stage 1 — a redirect to an internal address is blocked at the hop, before
   its body is read.

The network path is stdlib http.client only (no requests/httpx).
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from adapters.base import ADAPTER_ERROR, RESULT_TOO_LARGE, SSRF_BLOCKED, VALIDATION_ERROR, ToolError

INLINE_BODY_CAP = 256 * 1024  # 256 KB — inline text cap for GET/POST/HEAD
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024  # 200 MB — download abort cap
DEFAULT_TIMEOUT_S = 15
MAX_REDIRECTS = 5
MAX_URL_CHARS = 4096
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_NO_BODY_STATUSES = {204, 304}
_BLOCKED_IP_FLAGS = (
    ("private", "is_private"),
    ("loopback", "is_loopback"),
    ("link-local", "is_link_local"),
    ("reserved", "is_reserved"),
    ("multicast", "is_multicast"),
    ("unspecified", "is_unspecified"),
)


@dataclass
class _Target:
    scheme: str
    host: str
    port: int
    path: str
    connect_ip: str


def allowed_hosts() -> frozenset[str] | None:
    """Env ALLOWED_HOSTS (comma-separated), or None when unset/empty."""
    raw = os.environ.get("ALLOWED_HOSTS", "").strip()
    if not raw:
        return None
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def _validate_url(url: str) -> _Target:
    if not url or len(url) > MAX_URL_CHARS:
        raise ToolError(
            VALIDATION_ERROR,
            f"url must be 1–{MAX_URL_CHARS} characters",
            {"url_length": len(url) if url else 0, "limit": MAX_URL_CHARS},
        )
    try:
        split = urlsplit(url)
    except ValueError as exc:
        raise ToolError(VALIDATION_ERROR, f"cannot parse url: {exc}", {}) from exc
    scheme = split.scheme.lower()
    if scheme not in ("http", "https"):
        raise ToolError(
            SSRF_BLOCKED,
            f"scheme {scheme!r} is not allowed (http/https only)",
            {"scheme": scheme, "url": url},
        )
    host = split.hostname
    if not host:
        raise ToolError(VALIDATION_ERROR, "url has no hostname", {"url": url})
    if split.username or split.password:
        raise ToolError(
            VALIDATION_ERROR,
            "credentials in url are not allowed; pass auth via headers",
            {"url": url},
        )
    try:
        port = split.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise ToolError(VALIDATION_ERROR, f"invalid port in url: {exc}", {"url": url}) from exc

    allow = allowed_hosts()
    if allow is not None and host.lower() not in allow:
        raise ToolError(
            SSRF_BLOCKED,
            f"host {host!r} is not in ALLOWED_HOSTS",
            {"host": host, "allowed_hosts": sorted(allow)},
        )

    ips = _resolve_ips(host)
    if allow is None or host.lower() not in allow:
        # allowlisted hosts are operator-trusted: they bypass the private-IP block
        for ip in ips:
            _check_ip(ip, host)

    path = split.path or "/"
    if split.query:
        path = f"{path}?{split.query}"
    return _Target(scheme=scheme, host=host, port=port, path=path, connect_ip=ips[0])


def _resolve_ips(host: str) -> list[str]:
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass  # not an IP literal -> DNS
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ToolError(
            ADAPTER_ERROR,
            f"DNS resolution failed for {host!r} ({type(exc).__name__})",
            {"host": host, "exception": type(exc).__name__},
        ) from exc
    ips = sorted({info[4][0] for info in infos})
    if not ips:
        raise ToolError(ADAPTER_ERROR, f"DNS resolution returned no addresses for {host!r}", {"host": host})
    return ips


def _check_ip(ip_str: str, host: str) -> None:
    ip = ipaddress.ip_address(ip_str)
    reasons = [label for label, attr in _BLOCKED_IP_FLAGS if getattr(ip, attr)]
    if reasons:
        raise ToolError(
            SSRF_BLOCKED,
            f"host {host!r} resolves to blocked {'/'.join(reasons)} address {ip}",
            {"host": host, "ip": str(ip), "reasons": reasons},
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connects to a pre-validated IP but verifies TLS against the original hostname."""

    def __init__(self, host: str, port: int, *, pinned_host: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._pinned_host = pinned_host

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), self.timeout)
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(sock, server_hostname=self._pinned_host)


def _connect(target: _Target, timeout_s: float) -> http.client.HTTPConnection:
    if target.scheme == "https":
        return _PinnedHTTPSConnection(
            target.connect_ip, target.port, pinned_host=target.host, timeout=timeout_s
        )
    return http.client.HTTPConnection(target.connect_ip, target.port, timeout=timeout_s)


def _prepare_headers(
    target: _Target, user_headers: dict[str, str] | None
) -> dict[str, str]:
    hdrs: dict[str, str] = {}
    for name, value in (user_headers or {}).items():
        s_name, s_value = str(name), str(value)
        if "\r" in s_name or "\n" in s_name or "\r" in s_value or "\n" in s_value:
            raise ToolError(
                VALIDATION_ERROR,
                "header names and values must not contain CR/LF",
                {"header": s_name},
            )
        if s_name.lower() in ("host", "content-length"):
            continue  # Host is pinned; Content-Length is derived from the body
        hdrs[s_name] = s_value
    default_port = 443 if target.scheme == "https" else 80
    hdrs["Host"] = target.host if target.port == default_port else f"{target.host}:{target.port}"
    if not any(k.lower() == "accept-encoding" for k in hdrs):
        hdrs["Accept-Encoding"] = "identity"
    hdrs["Connection"] = "close"
    return hdrs


def _header_value(headers: dict[str, str], name: str) -> str | None:
    lname = name.lower()
    for key, value in headers.items():
        if key.lower() == lname:
            return value
    return None


def _read_capped(resp: http.client.HTTPResponse, cap: int) -> tuple[bytes, bool, int | None]:
    """Read at most cap bytes; boundary-inclusive (exactly cap is not truncated)."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > cap:
            break
    data = b"".join(chunks)
    truncated = total > cap
    if truncated:
        data = data[:cap]
    actual: int | None
    if not truncated:
        actual = total
    elif resp.length is not None:  # Content-Length known: remaining bytes
        actual = total + resp.length
    else:
        actual = None
    return data, truncated, actual


def _perform(
    url: str,
    method: str,
    headers: dict[str, str] | None,
    body: str | bytes | None,
    timeout_s: float,
    max_redirects: int,
    consume: Callable[[http.client.HTTPResponse, dict[str, str], str, int], dict[str, Any]],
) -> dict[str, Any]:
    current, hops = url, 0
    req_method, req_body = method, body
    while True:
        target = _validate_url(current)  # scheme + allowlist + DNS + IP, before any connect
        req_headers = _prepare_headers(target, headers)
        conn = _connect(target, timeout_s)
        try:
            conn.request(req_method, target.path, body=req_body, headers=req_headers)
            resp = conn.getresponse()
            resp_headers = dict(resp.getheaders())
            location = _header_value(resp_headers, "location")
            if resp.status in _REDIRECT_STATUSES and location:
                conn.close()  # hop body is never read
                if hops >= max_redirects:
                    raise ToolError(
                        ADAPTER_ERROR,
                        f"too many redirects (max {max_redirects})",
                        {"url": current, "max_redirects": max_redirects},
                    )
                current = urljoin(current, location)
                hops += 1
                if resp.status == 303 or (req_method == "POST" and resp.status in (301, 302)):
                    req_method, req_body = "GET", None
                continue  # loop top re-validates the new url before connecting
            return consume(resp, resp_headers, current, hops)
        except ToolError:
            conn.close()
            raise
        except (http.client.HTTPException, ssl.SSLError, TimeoutError, OSError, UnicodeError) as exc:
            conn.close()
            raise ToolError(
                ADAPTER_ERROR,
                f"HTTP request failed ({type(exc).__name__})",
                {"url": current, "exception": type(exc).__name__},
            ) from exc


def fetch(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | bytes | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_redirects: int = MAX_REDIRECTS,
) -> dict[str, Any]:
    """Fetch url and return status/headers/inline body (text, capped at 256 KB)."""
    if method not in ("GET", "POST", "HEAD"):
        raise ToolError(VALIDATION_ERROR, f"method {method!r} is not supported", {})

    def consume(
        resp: http.client.HTTPResponse, resp_headers: dict[str, str], final_url: str, hops: int
    ) -> dict[str, Any]:
        text, truncated, actual = "", False, None
        if method != "HEAD" and resp.status not in _NO_BODY_STATUSES:
            data, truncated, actual = _read_capped(resp, INLINE_BODY_CAP)
            text = data.decode("utf-8", errors="replace")
        return {
            "status": resp.status,
            "headers": resp_headers,
            "body": text,
            "truncated": truncated,
            "body_limit_bytes": INLINE_BODY_CAP,
            "body_actual_bytes": actual,
            "url": final_url,
            "hops": hops,
        }

    return _perform(url, method, headers, body, timeout_s, max_redirects, consume)


def download(
    url: str,
    dest: str | Path,
    headers: dict[str, str] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_redirects: int = MAX_REDIRECTS,
) -> dict[str, Any]:
    """Stream url to dest; abort (RESULT_TOO_LARGE) and delete the partial file past 200 MB."""

    def consume(
        resp: http.client.HTTPResponse, resp_headers: dict[str, str], final_url: str, hops: int
    ) -> dict[str, Any]:
        size = 0
        fh = open(dest, "wb")  # noqa: SIM115 (closed on both paths below)
        try:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                fh.write(chunk)
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise ToolError(
                        RESULT_TOO_LARGE,
                        f"download exceeds the {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB cap",
                        {
                            "size_bytes": size,
                            "cap_bytes": MAX_DOWNLOAD_BYTES,
                            "path": str(dest),
                        },
                    )
        except BaseException:
            fh.close()
            Path(dest).unlink(missing_ok=True)  # never keep a partial file
            raise
        fh.close()
        return {
            "status": resp.status,
            "headers": resp_headers,
            "path": str(dest),
            "size_bytes": size,
            "url": final_url,
            "hops": hops,
        }

    return _perform(url, "GET", headers, None, timeout_s, max_redirects, consume)
