"""Bearer auth + per-tool ACL (F-014) for the HTTP transport.

Tokens come from env/config (PROTOX_TOKENS), stored/compared as SHA-256
hashes. The authenticated token travels to the dispatcher via a contextvar;
the dispatcher enforces the per-tool ACL (PROTOX_TOOL_ACL) and rate limit.
Pure-localhost use may run tokenless (PRD §7.1): no tokens configured ->
auth disabled.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
from urllib.parse import unquote_plus

from starlette.types import ASGIApp, Receive, Scope, Send

from adapters.base import TOOL_NOT_PERMITTED, ToolError

# The bearer token for the in-flight request (None = stdio / tokenless).
current_token: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "protox_token", default=None
)

OPEN_PATHS = ("/health", "/metrics")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _query_token(scope: Scope) -> str:
    """Browser bootstrap credential: /?token=... for the console SPA, which
    cannot set Authorization headers on navigation. The SPA moves it to
    localStorage and strips it from the URL on load."""
    qs = scope.get("query_string", b"").decode(errors="replace")
    for part in qs.split("&"):
        if part.startswith("token="):
            return unquote_plus(part[len("token="):]).strip()
    return ""


class TokenStore:
    def __init__(self, tokens: list[str], tool_acl: dict[str, list[str]]):
        self.hashes = {hash_token(t) for t in tokens}
        # ACL keyed by token hash; values are allowed tool-name sets
        self.acl = {hash_token(t): set(names) for t, names in tool_acl.items()}

    @property
    def enabled(self) -> bool:
        return bool(self.hashes)

    def validate(self, token: str) -> bool:
        return hash_token(token) in self.hashes

    def check_acl(self, token: str | None, tool_name: str) -> None:
        """Raise TOOL_NOT_PERMITTED when the token's ACL excludes the tool."""
        if token is None:
            return  # stdio/direct dispatch is local-trusted
        allowed = self.acl.get(hash_token(token))
        if allowed is not None and tool_name not in allowed:
            raise ToolError(
                TOOL_NOT_PERMITTED,
                f"token is not permitted to call {tool_name}",
                {"tool": tool_name},
            )


class BearerAuthMiddleware:
    """Pure-ASGI middleware: 401 without a valid bearer token when tokens are
    configured. /health and /metrics stay open (doctor + scrape targets)."""

    def __init__(self, app: ASGIApp, store: TokenStore):
        self.app = app
        self.store = store

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.store.enabled:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode(errors="replace")
        token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        if not token:
            token = _query_token(scope)
        if not token or not self.store.validate(token):
            body = json.dumps(
                {"error": {"code": "UNAUTHORIZED", "message": "valid bearer token required"}}
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        reset = current_token.set(token)
        try:
            await self.app(scope, receive, send)
        finally:
            current_token.reset(reset)
