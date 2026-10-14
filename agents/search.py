"""Semantic tool search (F-040): embedded catalog + cosine ranking.

The catalog's tool descriptions are embedded once and persisted in
tool_embeddings (content-hash diffing keeps re-embeds minimal; a provider
model change re-embeds everything). Query-time ranking is pure-stdlib cosine
over float32 blobs. With no provider — or on any embedding failure — the
index degrades to a lexical token-overlap search and never blocks dispatch.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
from array import array
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from agents.embeddings import EmbeddingProvider, EmbeddingUnavailable
from core.db import Store
from core.logging import get_logger

log = get_logger("search")

_EMBED_BATCH = 32

# Blend weights for the lexical components in vector ranking (ISS-8): pure
# cosine lets generic tools (help_*, info_*) outrank the tool whose name
# actually contains the query's verb. The broad component covers
# name+description overlap; the name-only component gives an extra boost to
# tools whose *name* tokens match query terms.
_HYBRID_LEXICAL_WEIGHT = 0.25
_HYBRID_NAME_WEIGHT = 0.15


class SearchableEntry(Protocol):
    name: str
    description: str
    adapter_module: str
    state: str
    schema: dict[str, Any]


@dataclass
class SearchOutcome:
    results: list[dict[str, Any]]
    backend: str  # "tokenfactory" | "local" | "lexical"


@dataclass
class ToolSearchIndex:
    store: Store
    provider: EmbeddingProvider | None
    _dirty: bool = True
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @staticmethod
    def embed_text(entry: SearchableEntry) -> str:
        """Text that gets embedded: name + detailed description + module, plus
        the input-schema parameters (names and their descriptions) — queries
        often phrase intent in parameter language ("width", "delimiter")."""
        text = f"{entry.name}: {entry.description} ({entry.adapter_module})"
        props = (getattr(entry, "schema", None) or {}).get("properties") or {}
        if props:
            params = []
            for pname, pschema in list(props.items())[:12]:
                pdesc = pschema.get("description", "") if isinstance(pschema, dict) else ""
                params.append(f"{pname} — {pdesc}" if pdesc else pname)
            text += ". Parameters: " + ", ".join(params)
        return text[:1000]

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    def mark_dirty(self) -> None:
        self._dirty = True

    @property
    def backend_name(self) -> str:
        if self.provider is None:
            return "lexical"
        return self.provider.name.split(":", 1)[0]

    # -- sync ---------------------------------------------------------------

    def sync(self, entries: Sequence[SearchableEntry]) -> bool:
        """Embed new/changed tools, drop gone ones. False when the provider
        failed (stale rows kept, index stays dirty for a later retry)."""
        with self._lock:
            if self.provider is None:
                self._dirty = False  # lexical mode needs no vectors
                return True
            stored = {row["name"]: row for row in self.store.embeddings.all()}
            current = {e.name: e for e in entries}
            for name in stored:
                if name not in current:
                    self.store.embeddings.delete(name)
            todo: list[tuple[SearchableEntry, str, str]] = []
            for entry in entries:
                text = self.embed_text(entry)
                digest = self._hash(text)
                row = stored.get(entry.name)
                if row is None or row["content_hash"] != digest or row["model"] != self.provider.name:
                    todo.append((entry, text, digest))
            if not todo:
                self._dirty = False
                return True
            log.info("embedding sync: %d/%d tools need (re-)embedding", len(todo), len(entries))
            try:
                for i in range(0, len(todo), _EMBED_BATCH):
                    batch = todo[i : i + _EMBED_BATCH]
                    vectors = self.provider.embed([text for _, text, _ in batch])
                    if len(vectors) != len(batch):
                        raise EmbeddingUnavailable(
                            f"provider returned {len(vectors)} vectors for {len(batch)} texts"
                        )
                    for (entry, text, digest), vector in zip(batch, vectors, strict=True):
                        blob = array("f", vector).tobytes()
                        self.store.embeddings.upsert(
                            entry.name, entry.adapter_module, text, digest,
                            self.provider.name, blob, len(vector),
                        )
            except EmbeddingUnavailable as exc:
                log.warning("embedding sync failed, keeping stale rows: %s", exc)
                return False
            self._dirty = False
            return True

    def remove_module(self, adapter_module: str) -> None:
        self.store.embeddings.delete_module(adapter_module)

    # -- query ----------------------------------------------------------------

    def search(
        self,
        query: str,
        entries: Sequence[SearchableEntry],
        limit: int = 10,
        min_score: float = 0.0,
    ) -> SearchOutcome:
        if self._dirty:
            self.sync(entries)
        if self.provider is not None:
            try:
                qvec = self.provider.embed([query])[0]
            except EmbeddingUnavailable as exc:
                log.warning("query embedding failed, lexical fallback: %s", exc)
            else:
                return SearchOutcome(
                    self._vector_rank(query, qvec, entries, limit, min_score),
                    self.backend_name,
                )
        return SearchOutcome(self._lexical_rank(query, entries, limit), "lexical")

    def _vector_rank(
        self,
        query: str,
        qvec: list[float],
        entries: Sequence[SearchableEntry],
        limit: int,
        min_score: float,
    ) -> list[dict[str, Any]]:
        rows = {row["name"]: row for row in self.store.embeddings.all()}
        scored: list[dict[str, Any]] = []
        for entry in entries:
            row = rows.get(entry.name)
            if row is None:
                continue
            vec = array("f")
            vec.frombytes(row["embedding"])
            cosine = _cosine(qvec, vec)
            lexical = _lexical_score(query, f"{entry.name} {entry.description}")
            name_match = _lexical_score(query, entry.name)
            score = (
                (1 - _HYBRID_LEXICAL_WEIGHT - _HYBRID_NAME_WEIGHT) * cosine
                + _HYBRID_LEXICAL_WEIGHT * lexical
                + _HYBRID_NAME_WEIGHT * name_match
            )
            if score >= min_score:
                scored.append(_hit(entry, score))
        scored.sort(key=lambda hit: hit["score"], reverse=True)
        return scored[:limit]

    def _lexical_rank(
        self, query: str, entries: Sequence[SearchableEntry], limit: int
    ) -> list[dict[str, Any]]:
        scored = []
        for entry in entries:
            score = _lexical_score(query, f"{entry.name} {entry.description}")
            if score > 0:
                scored.append(_hit(entry, score))
        scored.sort(key=lambda hit: hit["score"], reverse=True)
        return scored[:limit]


def _hit(entry: SearchableEntry, score: float) -> dict[str, Any]:
    return {
        "name": entry.name,
        "description": entry.description,
        "adapter_module": entry.adapter_module,
        "state": entry.state,
        "score": round(score, 4),
    }


def _cosine(a: list[float] | array, b: list[float] | array) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _lexical_score(query: str, text: str) -> float:
    """Normalized bag-of-words overlap (cosine on term counts)."""
    qtokens = _TOKEN_RE.findall(query.lower())
    ttokens = _TOKEN_RE.findall(text.lower())
    if not qtokens or not ttokens:
        return 0.0
    tcounts: dict[str, int] = {}
    for tok in ttokens:
        tcounts[tok] = tcounts.get(tok, 0) + 1
    overlap = sum(1 for tok in qtokens if tok in tcounts)
    return overlap / math.sqrt(len(qtokens) * len(ttokens))
