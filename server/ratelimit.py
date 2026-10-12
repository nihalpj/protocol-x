"""Rate limiting (F-015): per-token sliding window, single-instance.

60 calls/min/token by default (configurable). Applied at dispatch time so it
covers every MCP method path uniformly; stdio dispatch (no token) is exempt.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from adapters.base import RATE_LIMITED, ToolError
from server.auth import hash_token


class RateLimiter:
    def __init__(self, per_minute: int = 60, enabled: bool = True):
        self.per_minute = per_minute
        self.enabled = enabled and per_minute > 0
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, token: str | None) -> None:
        """Raise RATE_LIMITED when the token exceeds the window budget."""
        if not self.enabled or token is None:
            return
        key = hash_token(token)
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= 60.0:
                hits.popleft()
            if len(hits) >= self.per_minute:
                raise ToolError(
                    RATE_LIMITED,
                    f"rate limit exceeded: {self.per_minute} calls/min",
                    {"limit_per_min": self.per_minute},
                )
            hits.append(now)
