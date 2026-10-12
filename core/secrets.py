"""Credential-injection boundary (F-033, INV-7).

Workers always spawn with a scrubbed environment. When a tool declares a
credential need (`secret_params` on its ToolSpec: param -> secret name), the
dispatcher injects the server-side value into that single call's args.
Secrets are never ambient env, never logged, never in results or audit.
"""

from __future__ import annotations

import re
from typing import Any

from adapters.base import ToolSpec


class SecretsBroker:
    def __init__(self, secrets: dict[str, str]):
        self.secrets = dict(secrets)

    def inject(self, spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
        """Fill declared secret params for one call (dispatcher-side only)."""
        declared = getattr(spec, "secret_params", None) or {}
        if not declared:
            return args
        merged = dict(args)
        for param, secret_name in declared.items():
            if merged.get(param) in (None, ""):
                value = self.secrets.get(secret_name)
                if value is not None:
                    merged[param] = value
        return merged

    def scrub_text(self, text: str) -> str:
        """Defensive: redact any secret value from a log/result string."""
        for value in self.secrets.values():
            if value:
                text = text.replace(value, "***REDACTED***")
        return text

    def env_leak_check(self, env: dict[str, str]) -> list[str]:
        """Return names of configured secrets present in a worker env dump."""
        leaked = []
        values = {v for v in self.secrets.values() if v}
        for key, val in env.items():
            if val in values:
                leaked.append(key)
        return leaked


def contains_secret(text: str, secrets: dict[str, str]) -> bool:
    return any(v and v in text for v in secrets.values())


_SECRETISH = re.compile(r"(?i)(secret|token|password|api[_-]?key)")


def no_secret_columns_guard(column_names: list[str]) -> list[str]:
    """CI helper: flag DB columns whose names suggest secret storage (INV-7)."""
    return [c for c in column_names if _SECRETISH.search(c)]
