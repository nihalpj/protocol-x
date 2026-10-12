"""AC-F012: docx tools — create, append, extract, literal replace, table add,
non-destructive defaults, corrupt-file rejection, boundary-inclusive limits."""

from __future__ import annotations

import pytest

from adapters.base import CORRUPT_FILE, VALIDATION_ERROR, ToolError
from adapters.docs_docx import MAX_PARAGRAPHS, MAX_TEXT_CHARS, MAX_TITLE_CHARS

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _write_docx(settings, name: str, paragraphs: list[str]) -> str:
    from docx import Document

    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    doc.save(str(settings.workspace_root / name))
    return name


def _data(ctx, tool: str, args: dict) -> dict:
    return ctx.dispatch(tool, args)["result"]["data"]


def test_create_then_extract_round_trip(ctx):
    created = _data(
        ctx,
        "docx_create",
        {"title": "Quarterly Report", "paragraphs": ["alpha body", "beta body"]},
    )
    out = created["output"]
    assert out["mime"] == DOCX_MIME
    assert out["uri"].startswith("protox-workspace:///")
    assert out["size_bytes"] > 0

    extracted = _data(ctx, "docx_extract_text", {"path": out["path"]})
    assert extracted["paragraphs"] == ["Quarterly Report", "alpha body", "beta body"]
    assert extracted["paragraph_count"] == 3


def test_create_empty_paragraph_list(ctx):
    created = _data(ctx, "docx_create", {"title": "Skeleton"})
    assert created["paragraph_count"] == 1  # title heading only
    extracted = _data(ctx, "docx_extract_text", {"path": created["output"]["path"]})
    assert extracted["paragraphs"] == ["Skeleton"]


def test_extract_text_from_generated_file(ctx, settings):
    name = _write_docx(settings, "plain.docx", ["one", "", "three"])
    extracted = _data(ctx, "docx_extract_text", {"path": name})
    assert extracted["paragraphs"] == ["one", "", "three"]
    assert extracted["paragraph_count"] == 3
    assert extracted["size_bytes"] > 0


def test_append_default_writes_new_file_and_keeps_source(ctx, settings):
    name = _write_docx(settings, "append-src.docx", ["original"])
    source_bytes_before = (settings.workspace_root / name).read_bytes()

    result = _data(ctx, "docx_append_paragraph", {"path": name, "text": "added"})

    out = result["output"]
    assert out["path"] != name
    assert result["in_place"] is False
    # Source file byte-identical after the call (AC-F012-03).
    assert (settings.workspace_root / name).read_bytes() == source_bytes_before
    assert _data(ctx, "docx_extract_text", {"path": name})["paragraphs"] == ["original"]
    assert _data(ctx, "docx_extract_text", {"path": out["path"]})["paragraphs"] == ["original", "added"]


def test_append_in_place_modifies_source(ctx, settings):
    name = _write_docx(settings, "append-inplace.docx", ["original"])
    result = _data(
        ctx, "docx_append_paragraph", {"path": name, "text": "added", "in_place": True}
    )
    assert result["output"]["path"] == name
    assert result["in_place"] is True
    assert _data(ctx, "docx_extract_text", {"path": name})["paragraphs"] == ["original", "added"]


def test_append_with_explicit_out_path(ctx, settings):
    name = _write_docx(settings, "append-out.docx", ["original"])
    result = _data(
        ctx,
        "docx_append_paragraph",
        {"path": name, "text": "added", "out_path": "custom/moved.docx"},
    )
    assert result["output"]["path"] == "custom/moved.docx"
    assert _data(ctx, "docx_extract_text", {"path": name})["paragraphs"] == ["original"]


def test_replace_text_is_literal_and_reports_count(ctx, settings):
    # ".*" must be matched as a literal string, never as a regex (AC-P2-05).
    name = _write_docx(settings, "replace.docx", ["price is 5.* today", "no match here"])
    result = _data(
        ctx,
        "docx_replace_text",
        {"path": name, "find": ".*", "replace": "X", "out_path": "replaced.docx"},
    )
    assert result["replacements"] == 1
    assert _data(ctx, "docx_extract_text", {"path": "replaced.docx"})["paragraphs"] == [
        "price is 5X today",
        "no match here",
    ]
    # Source untouched (default non-destructive).
    assert _data(ctx, "docx_extract_text", {"path": name})["paragraphs"] == [
        "price is 5.* today",
        "no match here",
    ]


def test_replace_text_counts_all_occurrences(ctx, settings):
    name = _write_docx(settings, "replace-count.docx", ["aaa", "a tip a"])
    result = _data(
        ctx, "docx_replace_text", {"path": name, "find": "a", "replace": "bb", "in_place": True}
    )
    assert result["replacements"] == 5
    assert _data(ctx, "docx_extract_text", {"path": name})["paragraphs"] == ["bbbbbb", "bb tip bb"]


def test_replace_text_no_match_returns_zero(ctx, settings):
    name = _write_docx(settings, "replace-none.docx", ["hello"])
    result = _data(
        ctx, "docx_replace_text", {"path": name, "find": "zzz", "replace": "X"}
    )
    assert result["replacements"] == 0
    assert _data(ctx, "docx_extract_text", {"path": result["output"]["path"]})["paragraphs"] == ["hello"]


def test_replace_text_walks_table_cells(ctx, settings):
    from docx import Document

    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    doc = Document()
    table = doc.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "cell needle value"
    doc.save(str(settings.workspace_root / "table-replace.docx"))

    result = _data(
        ctx,
        "docx_replace_text",
        {"path": "table-replace.docx", "find": "needle", "replace": "pin", "in_place": True},
    )
    assert result["replacements"] == 1

    check = Document(str(settings.workspace_root / "table-replace.docx"))
    assert check.tables[0].cell(0, 0).text == "cell pin value"


def test_add_table_default_writes_new_file(ctx, settings):
    name = _write_docx(settings, "table-src.docx", ["intro"])
    result = _data(
        ctx,
        "docx_add_table",
        {"path": name, "rows": [["a", "b"], ["c", "d"]]},
    )
    assert result["output"]["path"] != name
    assert (result["rows"], result["cols"]) == (2, 2)
    assert _data(ctx, "docx_extract_text", {"path": name})["paragraphs"] == ["intro"]

    from docx import Document

    doc = Document(str(settings.workspace_root / result["output"]["path"]))
    assert len(doc.tables) == 1
    assert doc.tables[0].cell(0, 0).text == "a"
    assert doc.tables[0].cell(1, 1).text == "d"


def test_add_table_in_place(ctx, settings):
    name = _write_docx(settings, "table-inplace.docx", ["intro"])
    result = _data(
        ctx,
        "docx_add_table",
        {"path": name, "rows": [["x"]], "in_place": True},
    )
    assert result["output"]["path"] == name

    from docx import Document

    doc = Document(str(settings.workspace_root / name))
    assert len(doc.tables) == 1
    assert doc.tables[0].cell(0, 0).text == "x"


def test_ragged_table_rows_rejected(ctx, settings):
    name = _write_docx(settings, "ragged.docx", ["x"])
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("docx_add_table", {"path": name, "rows": [["a", "b"], ["c"]]})
    assert exc.value.code == VALIDATION_ERROR


def test_corrupt_file_not_zip_magic(ctx, settings):
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    (settings.workspace_root / "fake.docx").write_bytes(b"this is definitely not a docx")
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("docx_extract_text", {"path": "fake.docx"})
    assert exc.value.code == CORRUPT_FILE


def test_corrupt_file_zip_magic_but_garbage(ctx, settings):
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    (settings.workspace_root / "broken.docx").write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("docx_extract_text", {"path": "broken.docx"})
    assert exc.value.code == CORRUPT_FILE


def test_corrupt_file_detected_by_append(ctx, settings):
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    (settings.workspace_root / "junk.docx").write_bytes(b"garbage bytes for append test")
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("docx_append_paragraph", {"path": "junk.docx", "text": "hi"})
    assert exc.value.code == CORRUPT_FILE


def test_extra_arg_forbidden(ctx):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("docx_create", {"title": "T", "paragraphs": [], "surprise": 1})
    assert exc.value.code == VALIDATION_ERROR


def test_missing_file_is_validation_error(ctx):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("docx_extract_text", {"path": "nope.docx"})
    assert exc.value.code == VALIDATION_ERROR


def test_empty_find_rejected(ctx, settings):
    name = _write_docx(settings, "empty-find.docx", ["x"])
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("docx_replace_text", {"path": name, "find": "", "replace": "y"})
    assert exc.value.code == VALIDATION_ERROR


def test_boundary_title_at_max_succeeds(ctx):
    title = "T" * MAX_TITLE_CHARS
    result = _data(ctx, "docx_create", {"title": title})
    assert _data(ctx, "docx_extract_text", {"path": result["output"]["path"]})["paragraphs"] == [title]


def test_boundary_title_over_max_rejected(ctx):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("docx_create", {"title": "T" * (MAX_TITLE_CHARS + 1)})
    assert exc.value.code == VALIDATION_ERROR


def test_boundary_paragraph_text_at_max_succeeds(ctx, settings):
    name = _write_docx(settings, "boundary.docx", ["x"])
    big = "w" * MAX_TEXT_CHARS
    result = _data(ctx, "docx_append_paragraph", {"path": name, "text": big})
    assert _data(ctx, "docx_extract_text", {"path": result["output"]["path"]})["paragraphs"] == ["x", big]


def test_boundary_paragraph_text_over_max_rejected(ctx, settings):
    name = _write_docx(settings, "boundary-over.docx", ["x"])
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("docx_append_paragraph", {"path": name, "text": "w" * (MAX_TEXT_CHARS + 1)})
    assert exc.value.code == VALIDATION_ERROR


def test_boundary_paragraph_count_at_max_succeeds(ctx):
    paragraphs = [f"p{i}" for i in range(MAX_PARAGRAPHS)]
    result = _data(ctx, "docx_create", {"title": "Bulk", "paragraphs": paragraphs})
    assert result["paragraph_count"] == MAX_PARAGRAPHS + 1  # + title heading


def test_boundary_paragraph_count_over_max_rejected(ctx):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("docx_create", {"title": "T", "paragraphs": ["x"] * (MAX_PARAGRAPHS + 1)})
    assert exc.value.code == VALIDATION_ERROR
