"""Embedding providers for semantic tool search (F-040).

Two backends behind one protocol: Token Factory `/v1/embeddings` (default when
an API key is configured) and a local ONNX model via `fastembed` (fallback,
downloads weights once to the HF cache on first use). `select_provider` maps
the `search.backend` setting to a provider; `off` or a missing key yields
None, in which case callers must degrade to lexical search.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from agents.tokenfactory import InferenceError, TokenFactoryClient
from core.logging import get_logger

log = get_logger("embeddings")


class EmbeddingUnavailable(Exception):
    """Provider cannot produce embeddings (missing package/model/endpoint)."""


@runtime_checkable
class EmbeddingProvider(Protocol):
    @property
    def name(self) -> str:
        """Stable identifier stored with each row; a change forces re-embed."""
        ...

    @property
    def dims(self) -> int:
        """Vector size; 0 until the first successful embed call."""
        ...

    def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector per input text, in order."""
        ...


class TokenFactoryEmbeddings:
    def __init__(self, client: TokenFactoryClient, model: str):
        self._client = client
        self.model = model
        self._dims = 0

    @property
    def name(self) -> str:
        return f"tokenfactory:{self.model}"

    @property
    def dims(self) -> int:
        return self._dims

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            vectors = self._client.embed(texts, self.model)
        except InferenceError as exc:
            raise EmbeddingUnavailable(str(exc)) from exc
        if vectors and not self._dims:
            self._dims = len(vectors[0])
        return vectors


class FastEmbedLocal:
    def __init__(self, model: str):
        self.model = model
        self._dims = 0
        self._model_obj: Any = None

    @property
    def name(self) -> str:
        return f"local:{self.model}"

    @property
    def dims(self) -> int:
        return self._dims

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._model_obj is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:
                raise EmbeddingUnavailable(
                    "fastembed is not installed; cannot use the local search backend"
                ) from exc
            try:
                self._model_obj = TextEmbedding(model_name=self.model)
            except Exception as exc:  # model download/load failure
                raise EmbeddingUnavailable(f"local embedding model unavailable: {exc}") from exc
        vectors = [list(map(float, vec)) for vec in self._model_obj.embed(texts)]
        if vectors and not self._dims:
            self._dims = len(vectors[0])
        return vectors


class _SearchSettings(Protocol):
    """Structural view of server.config.Settings (agents may not import server)."""

    search_backend: str
    tf_api_key: str
    tf_endpoint: str
    tf_timeout_s: float
    tf_embed_model: str
    local_embed_model: str


def select_provider(settings: _SearchSettings) -> EmbeddingProvider | None:
    """Map settings.search_backend to a provider; None means lexical-only mode."""
    backend = settings.search_backend
    if backend == "off":
        return None
    if backend in ("auto", "tokenfactory") and settings.tf_api_key:
        client = TokenFactoryClient(
            settings.tf_api_key,
            endpoint=settings.tf_endpoint,
            timeout_s=settings.tf_timeout_s,
        )
        return TokenFactoryEmbeddings(client, settings.tf_embed_model)
    if backend == "tokenfactory":
        log.warning("search.backend=tokenfactory but no Token Factory API key; lexical-only search")
        return None
    return FastEmbedLocal(settings.local_embed_model)
