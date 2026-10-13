"""Nebius Token Factory client (hackathon model dependency; INV-8 sanctioned egress).

OpenAI-compatible chat completions against a configurable endpoint. Typed
InferenceError drives the fail-closed governance paths (INV-6): judge outage
means nothing registers, ever.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx


class InferenceError(Exception):
    """Endpoint unreachable / non-200 / malformed response."""


@dataclass
class Completion:
    content: str
    model: str
    prompt_hash: str  # sha256 of the request payload, for the audit trail


class TokenFactoryClient:
    def __init__(
        self,
        api_key: str,
        endpoint: str = "https://api.tokenfactory.nebius.com/v1",
        timeout_s: float = 60.0,
        max_retries: int = 3,
        http_client: httpx.Client | None = None,
    ):
        if not api_key:
            raise InferenceError("no Token Factory API key configured (degraded mode, E-20)")
        self.endpoint = endpoint.rstrip("/")
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._client = http_client or httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout_s
        )

    def chat(
        self, model: str, messages: list[dict], temperature: float = 0.0, max_tokens: int = 4096
    ) -> Completion:
        import hashlib
        import json

        payload = {"model": model, "messages": messages, "temperature": temperature}
        prompt_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        body = {**payload, "max_tokens": max_tokens}
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self._client.post(f"{self.endpoint}/chat/completions", json=body)
                if resp.status_code != 200:
                    raise InferenceError(f"endpoint returned {resp.status_code}: {resp.text[:200]}")
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                if not content:
                    # Reasoning models can exhaust the token budget on
                    # reasoning_content and return empty content (finish_reason=
                    # "length") — retry with backoff rather than poisoning parsers.
                    finish = data["choices"][0].get("finish_reason")
                    raise InferenceError(f"empty completion content (finish_reason={finish})")
                return Completion(content=content, model=data.get("model", model), prompt_hash=prompt_hash)
            except (httpx.HTTPError, KeyError, IndexError, ValueError, InferenceError) as exc:
                last_exc = exc
                time.sleep(min(2**attempt, 8))  # exponential backoff
        raise InferenceError(f"inference failed after {self.max_retries} attempts: {last_exc}")
