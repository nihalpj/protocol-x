"""HTTP adapter (F-009): 4 network tools with an airtight SSRF guard.

Handlers run INSIDE the sandbox worker. All network I/O goes through
adapters/_urlguard.py: scheme allowlist, per-hop DNS+IP validation (private,
loopback, link-local, reserved, multicast and unspecified addresses are
refused — incl. 169.254.169.254 cloud metadata), connect-by-IP with Host/SNI
pinning, manual redirect following (max 5 hops, each re-validated before the
next connection is opened), and an optional env ALLOWED_HOSTS egress
allowlist (comma-separated hostnames; operator-trusted listed hosts bypass
the private-IP block, e.g. for intranet or test servers). Limits are stated
per tool and enforced boundary-inclusively (F-020).
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from adapters import _urlguard
from adapters.base import VALIDATION_ERROR, ToolError, ToolSpec

MAX_URL_CHARS = 4096
MAX_POST_BODY_CHARS = 1_048_576  # 1 MB raw/json body cap


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HttpGetIn(_In):
    url: str = Field(min_length=1, max_length=MAX_URL_CHARS, description="HTTP/HTTPS URL (1–4096 chars)")
    headers: dict[str, str] | None = Field(default=None, description="Optional request headers")
    timeout_s: int = Field(default=15, ge=1, le=60, description="Socket timeout, seconds (1–60)")
    max_redirects: int = Field(default=5, ge=0, le=5, description="Redirect hops allowed (0–5)")


class HttpPostIn(_In):
    url: str = Field(min_length=1, max_length=MAX_URL_CHARS, description="HTTP/HTTPS URL (1–4096 chars)")
    headers: dict[str, str] | None = Field(default=None, description="Optional request headers")
    body: str | None = Field(
        default=None,
        max_length=MAX_POST_BODY_CHARS,
        description=(
            f"Raw request body text (≤{MAX_POST_BODY_CHARS // (1024 * 1024)} MB chars); "
            "mutually exclusive with json_body"
        ),
    )
    json_body: Any | None = Field(
        default=None,
        description="JSON-serializable value sent as the request body; mutually exclusive with body",
    )
    timeout_s: int = Field(default=15, ge=1, le=60, description="Socket timeout, seconds (1–60)")
    max_redirects: int = Field(default=5, ge=0, le=5, description="Redirect hops allowed (0–5)")


class HttpHeadIn(_In):
    url: str = Field(min_length=1, max_length=MAX_URL_CHARS, description="HTTP/HTTPS URL (1–4096 chars)")
    headers: dict[str, str] | None = Field(default=None, description="Optional request headers")
    timeout_s: int = Field(default=15, ge=1, le=60, description="Socket timeout, seconds (1–60)")
    max_redirects: int = Field(default=5, ge=0, le=5, description="Redirect hops allowed (0–5)")


class HttpDownloadIn(_In):
    url: str = Field(min_length=1, max_length=MAX_URL_CHARS, description="HTTP/HTTPS URL (1–4096 chars)")
    out_path: str | None = Field(
        default=None, description="Optional output path in the workspace (default: auto-named file in out/)"
    )
    headers: dict[str, str] | None = Field(default=None, description="Optional request headers")
    timeout_s: int = Field(default=15, ge=1, le=60, description="Socket timeout, seconds (1–60)")
    max_redirects: int = Field(default=5, ge=0, le=5, description="Redirect hops allowed (0–5)")


def http_get(
    ws,
    url: str,
    headers: dict[str, str] | None = None,
    timeout_s: int = 15,
    max_redirects: int = 5,
) -> dict[str, Any]:
    return _urlguard.fetch(url, "GET", headers, None, timeout_s, max_redirects)


def http_post(
    ws,
    url: str,
    headers: dict[str, str] | None = None,
    body: str | None = None,
    json_body: Any | None = None,
    timeout_s: int = 15,
    max_redirects: int = 5,
) -> dict[str, Any]:
    req_headers = dict(headers or {})
    if json_body is not None:
        if body is not None:
            raise ToolError(
                VALIDATION_ERROR,
                "pass either body or json_body, not both",
                {"fields": ["body", "json_body"]},
            )
        body = json.dumps(json_body)
        if not any(k.lower() == "content-type" for k in req_headers):
            req_headers["Content-Type"] = "application/json"
    return _urlguard.fetch(url, "POST", req_headers, body, timeout_s, max_redirects)


def http_head(
    ws,
    url: str,
    headers: dict[str, str] | None = None,
    timeout_s: int = 15,
    max_redirects: int = 5,
) -> dict[str, Any]:
    return _urlguard.fetch(url, "HEAD", headers, None, timeout_s, max_redirects)


def _suggested_name(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        name = Path(urlsplit(url).path).name
    except ValueError:
        name = ""
    return name if name and name not in (".", "..") else "download.bin"


def http_download_file(
    ws,
    url: str,
    out_path: str | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: int = 15,
    max_redirects: int = 5,
) -> dict[str, Any]:
    suggested = _suggested_name(url)
    target = ws.resolve(out_path) if out_path else ws.new_output_path(suggested)
    target.parent.mkdir(parents=True, exist_ok=True)
    result = _urlguard.download(url, target, headers, timeout_s, max_redirects)
    content_type = None
    for key, value in result["headers"].items():
        if key.lower() == "content-type":
            content_type = value.split(";")[0].strip()
            break
    mime = content_type or mimetypes.guess_type(suggested)[0] or "application/octet-stream"
    return {
        "output": ws.resource_ref(target, mime),
        "size_bytes": result["size_bytes"],
        "status": result["status"],
        "url": result["url"],
        "hops": result["hops"],
    }


_GUARD_BLURB = (
    "SSRF-guarded: http/https only; host DNS-resolved and refused if any address is "
    "private/loopback/link-local/reserved/multicast/unspecified; connect-by-IP with Host "
    "pinning; every redirect hop re-validated (max 5); optional env ALLOWED_HOSTS "
    "(comma-separated) restricts egress to trusted hosts."
)

TOOLS = [
    ToolSpec(
        "http_get",
        f"Fetch an HTTP/HTTPS URL (GET); returns status, headers, and body text. Inline body "
        f"cap 256 KB — larger bodies are cut, with truncated:true, body_limit_bytes and "
        f"body_actual_bytes. {_GUARD_BLURB} url ≤{MAX_URL_CHARS} chars; timeout_s 1–60 (default 15).",
        HttpGetIn,
        http_get,
        timeout_s=30,
        network=True,
        limits={"inline_body_cap_kb": 256, "max_redirects": 5},
    ),
    ToolSpec(
        "http_post",
        f"POST to an HTTP/HTTPS URL with a raw body (≤1 MB chars) or a json_body (mutually "
        f"exclusive) plus optional custom headers; response shape matches http_get (inline body "
        f"cap 256 KB). {_GUARD_BLURB} url ≤{MAX_URL_CHARS} chars; timeout_s 1–60 (default 15).",
        HttpPostIn,
        http_post,
        timeout_s=30,
        network=True,
        limits={"post_body_cap_mb": 1, "inline_body_cap_kb": 256, "max_redirects": 5},
    ),
    ToolSpec(
        "http_head",
        f"Send an HTTP HEAD request; returns status and headers with an empty body. {_GUARD_BLURB} "
        f"url ≤{MAX_URL_CHARS} chars; timeout_s 1–60 (default 15).",
        HttpHeadIn,
        http_head,
        timeout_s=30,
        network=True,
        limits={"max_redirects": 5},
    ),
    ToolSpec(
        "http_download_file",
        f"Download a URL to a workspace file (streamed; auto-named in out/ or caller-chosen "
        f"out_path). Abort cap 200 MB — the partial file is deleted and RESULT_TOO_LARGE raised "
        f"past the cap. Returns a file resource ref plus size_bytes. {_GUARD_BLURB} "
        f"url ≤{MAX_URL_CHARS} chars; timeout_s 1–60 (default 15).",
        HttpDownloadIn,
        http_download_file,
        timeout_s=30,
        network=True,
        limits={"download_cap_mb": 200, "max_redirects": 5},
    ),
]
