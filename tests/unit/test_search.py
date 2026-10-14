"""F-040 unit tests: cosine math, lexical fallback, sync diffing, repo roundtrip."""

from __future__ import annotations

import math
from array import array
from types import SimpleNamespace

import pytest

from agents.embeddings import EmbeddingUnavailable
from agents.search import ToolSearchIndex, _cosine, _lexical_score
from core.db import Store

VOCAB = ["image", "resize", "json", "parse", "date", "audio"]


class FakeProvider:
    """Keyword-bag embeddings: vec[i] = count of VOCAB[i] in the text."""

    def __init__(self, name: str = "fake:v1", fail: bool = False):
        self.name = name
        self.dims = len(VOCAB)
        self.fail = fail
        self.batches: list[list[str]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise EmbeddingUnavailable("fake outage")
        self.batches.append(list(texts))
        return [[float(t.lower().count(w)) for w in VOCAB] for t in texts]


def entry(name: str, description: str, module: str = "demo", state: str = "live"):
    return SimpleNamespace(
        name=name, description=description, adapter_module=module, state=state,
        schema={"properties": {}},
    )


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "protox.db")
    yield s
    s.close()


@pytest.fixture()
def entries():
    return [
        entry("image_resize", "Resize an image to a width/height", "imaging"),
        entry("json_parse", "Parse JSON text into an object", "jsonx"),
        entry("date_format", "Format a date as text", "dates"),
    ]


# -- embed_text ----------------------------------------------------------------


def test_embed_text_includes_parameter_details():
    e = SimpleNamespace(
        name="image_resize",
        description="Resize an image to a width/height",
        adapter_module="imaging",
        state="live",
        schema={
            "properties": {
                "path": {"description": "input image file"},
                "width": {"description": "target width in pixels"},
            }
        },
    )
    text = ToolSearchIndex.embed_text(e)
    assert text.startswith("image_resize: Resize an image to a width/height (imaging)")
    assert "width — target width in pixels" in text
    assert "path — input image file" in text


def test_embed_text_without_schema():
    e = entry("t", "does things", "mod")
    assert ToolSearchIndex.embed_text(e) == "t: does things (mod)"


# -- pure helpers ------------------------------------------------------------


def test_cosine_identical_is_one():
    assert _cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero():
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_zero_norm_is_zero():
    assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_known_value():
    assert _cosine([1.0, 1.0], [1.0, 0.0]) == pytest.approx(1 / math.sqrt(2))


def test_lexical_score_overlap():
    assert _lexical_score("resize image", "image_resize resize an image") > 0


def test_lexical_score_disjoint_is_zero():
    assert _lexical_score("audio transcode", "parse json text") == 0.0


def test_lexical_score_full_overlap_beats_partial():
    full = _lexical_score("resize image", "resize image")
    partial = _lexical_score("resize image", "resize something else entirely")
    assert full > partial


# -- EmbeddingRepo roundtrip ---------------------------------------------------


def test_embedding_repo_roundtrip(store):
    blob = array("f", [0.5, -1.25, 3.0]).tobytes()
    store.embeddings.upsert("tool_a", "demo", "tool_a: does a (demo)", "hash1", "fake:v1", blob, 3)
    row = store.embeddings.get("tool_a")
    assert row is not None
    assert row["adapter_module"] == "demo"
    assert row["model"] == "fake:v1"
    assert row["dims"] == 3
    vec = array("f")
    vec.frombytes(row["embedding"])
    assert list(vec) == pytest.approx([0.5, -1.25, 3.0])
    # upsert replaces
    store.embeddings.upsert("tool_a", "demo", "changed", "hash2", "fake:v1", blob, 3)
    assert store.embeddings.get("tool_a")["content_hash"] == "hash2"
    assert [r["name"] for r in store.embeddings.all()] == ["tool_a"]
    store.embeddings.delete("tool_a")
    assert store.embeddings.get("tool_a") is None


def test_embedding_repo_delete_module(store):
    blob = array("f", [1.0]).tobytes()
    store.embeddings.upsert("t1", "mod_a", "t1", "h1", "m", blob, 1)
    store.embeddings.upsert("t2", "mod_a", "t2", "h2", "m", blob, 1)
    store.embeddings.upsert("t3", "mod_b", "t3", "h3", "m", blob, 1)
    store.embeddings.delete_module("mod_a")
    assert [r["name"] for r in store.embeddings.all()] == ["t3"]


# -- sync diffing ---------------------------------------------------------------


def test_sync_embeds_new_tools_once(store, entries):
    provider = FakeProvider()
    index = ToolSearchIndex(store, provider)
    assert index.sync(entries) is True
    assert sum(len(b) for b in provider.batches) == 3
    assert len(store.embeddings.all()) == 3
    # unchanged -> no re-embed
    assert index.sync(entries) is True
    assert sum(len(b) for b in provider.batches) == 3


def test_sync_reembeds_only_changed(store, entries):
    provider = FakeProvider()
    index = ToolSearchIndex(store, provider)
    index.sync(entries)
    changed = [
        entry("image_resize", "Resize an image (edited description)", "imaging"),
        entries[1],
        entries[2],
    ]
    index.mark_dirty()
    index.sync(changed)
    last_batch = provider.batches[-1]
    assert len(last_batch) == 1
    assert "edited description" in last_batch[0]


def test_sync_deletes_gone_tools(store, entries):
    provider = FakeProvider()
    index = ToolSearchIndex(store, provider)
    index.sync(entries)
    index.mark_dirty()
    index.sync(entries[1:])
    assert {r["name"] for r in store.embeddings.all()} == {"json_parse", "date_format"}


def test_sync_model_change_reembeds_everything(store, entries):
    index = ToolSearchIndex(store, FakeProvider(name="fake:v1"))
    index.sync(entries)
    provider_v2 = FakeProvider(name="fake:v2")
    index.provider = provider_v2
    index.mark_dirty()
    index.sync(entries)
    assert sum(len(b) for b in provider_v2.batches) == 3
    assert {r["model"] for r in store.embeddings.all()} == {"fake:v2"}


def test_sync_provider_failure_keeps_stale_rows_and_stays_dirty(store, entries):
    index = ToolSearchIndex(store, FakeProvider())
    index.sync(entries)
    index.provider = FakeProvider(name="fake:v2", fail=True)
    index.mark_dirty()
    assert index.sync(entries) is False
    assert len(store.embeddings.all()) == 3  # stale rows survive
    assert index._dirty is True


# -- search ----------------------------------------------------------------------


def test_vector_search_ranks_by_similarity(store, entries):
    index = ToolSearchIndex(store, FakeProvider())
    outcome = index.search("resize image", entries, limit=10)
    assert outcome.backend == "fake"
    assert outcome.results[0]["name"] == "image_resize"
    # hybrid scoring: 0.6*cosine(1.0) + 0.25*lexical(~0.47) + 0.15*name(1.0) ≈ 0.87
    assert outcome.results[0]["score"] > 0.8
    result_names = [r["name"] for r in outcome.results]
    assert result_names.index("image_resize") < result_names.index("json_parse")


def test_vector_search_result_shape(store, entries):
    index = ToolSearchIndex(store, FakeProvider())
    outcome = index.search("parse json", entries)
    hit = outcome.results[0]
    assert hit["name"] == "json_parse"
    assert hit["description"] == entries[1].description
    assert hit["adapter_module"] == "jsonx"
    assert hit["state"] == "live"
    assert 0 < hit["score"] <= 1


def test_search_lexical_when_no_provider(store, entries):
    index = ToolSearchIndex(store, None)
    outcome = index.search("resize image", entries)
    assert outcome.backend == "lexical"
    assert outcome.results[0]["name"] == "image_resize"


def test_search_lexical_fallback_on_provider_failure(store, entries):
    index = ToolSearchIndex(store, FakeProvider(fail=True))
    outcome = index.search("resize image", entries)
    assert outcome.backend == "lexical"
    assert outcome.results[0]["name"] == "image_resize"


def test_search_limit(store, entries):
    index = ToolSearchIndex(store, None)
    outcome = index.search("image resize parse json date", entries, limit=2)
    assert len(outcome.results) == 2


def test_search_no_match_returns_empty(store, entries):
    index = ToolSearchIndex(store, None)
    outcome = index.search("quantum entanglement", entries)
    assert outcome.results == []


# -- hybrid ranking (ISS-8) ------------------------------------------------------


class StaticProvider:
    """Fixed vectors: pure cosine prefers the generic help tool over the
    encoder whose *name* contains the query verb."""

    name = "static:v1"
    dims = 2

    def embed(self, texts: list[str]) -> list[list[float]]:
        vecs = []
        for t in texts:
            if t.startswith("pyjwt_help_main"):
                vecs.append([1.0, 0.0])  # perfect cosine match for the query
            elif t.startswith("pyjwt_utils_base64url_encode"):
                vecs.append([0.95, math.sqrt(1 - 0.95**2)])  # cosine 0.95
            else:  # the query itself
                vecs.append([1.0, 0.0])
        return vecs


def test_vector_search_blends_lexical_name_match(store):
    tools = [
        entry("pyjwt_help_main", "Pretty-print the bug information as JSON", "pyjwt"),
        entry(
            "pyjwt_utils_base64url_encode",
            "utils.base64url_encode() from pyjwt",
            "pyjwt",
        ),
    ]
    index = ToolSearchIndex(store, StaticProvider())
    outcome = index.search("encode or decode a jwt token", tools, limit=5)
    assert outcome.backend == "static"
    assert outcome.results[0]["name"] == "pyjwt_utils_base64url_encode"
