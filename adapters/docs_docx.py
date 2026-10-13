"""python-docx adapter (F-012): 5 Word document tools.

Handlers run INSIDE the sandbox worker. `ws` is the workspace confinement
object passed by the worker; all paths resolve under the workspace root.
.docx files are ZIP containers: read tools verify ZIP magic bytes before
parsing (CORRUPT_FILE on mismatch). Mutating tools never overwrite the input
unless `in_place: true` (AC-F012-03); by default they write a new output file
and leave the source unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from adapters._guards import check_magic
from adapters.base import CORRUPT_FILE, ToolError, ToolSpec

MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

MAX_TITLE_CHARS = 512
MAX_PARAGRAPHS = 10000
MAX_TEXT_CHARS = 65536  # per paragraph / cell / replacement string
MAX_TABLE_ROWS = 10000
MAX_TABLE_COLS = 64
MAX_FIND_CHARS = 4096

_Text = Annotated[str, Field(max_length=MAX_TEXT_CHARS)]


def _load(ws, path: str):
    """Open a .docx from the workspace; magic-check then parse."""
    from docx import Document

    resolved = ws.resolve(path, must_exist=True)
    check_magic(resolved, "zip")
    try:
        doc = Document(str(resolved))
    except Exception as exc:
        raise ToolError(
            CORRUPT_FILE,
            f"cannot open docx: {type(exc).__name__}: {exc}",
            {"path": path},
        ) from exc
    return doc, resolved


def _target(ws, in_place: bool, out_path: str | None, source: Path, suggested: str) -> Path:
    if in_place:
        return source
    if out_path:
        target: Path = ws.resolve(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target
    new_path: Path = ws.new_output_path(suggested)
    return new_path


def _all_paragraphs(doc):
    """Body paragraphs plus table-cell paragraphs (one nesting level)."""
    yield from doc.paragraphs
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from cell.paragraphs


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocxCreateIn(_In):
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS, description="Document title (1–512 chars)")
    paragraphs: list[_Text] = Field(
        default_factory=list,
        max_length=MAX_PARAGRAPHS,
        description=f"Paragraph texts (0–{MAX_PARAGRAPHS} items, each ≤ {MAX_TEXT_CHARS} chars)",
    )


_IN_PLACE_DESC = "Overwrite the input file instead of writing a new output"
_OUT_PATH_DESC = "Optional output path in workspace (ignored when in_place)"


class DocxAppendParagraphIn(_In):
    path: str = Field(description="Existing .docx path, relative to the workspace root")
    text: str = Field(
        min_length=1,
        max_length=MAX_TEXT_CHARS,
        description=f"Paragraph text (1–{MAX_TEXT_CHARS} chars)",
    )
    in_place: bool = Field(default=False, description=_IN_PLACE_DESC)
    out_path: str | None = Field(default=None, description=_OUT_PATH_DESC)


class DocxExtractTextIn(_In):
    path: str = Field(description="Existing .docx path, relative to the workspace root")


class DocxReplaceTextIn(_In):
    path: str = Field(description="Existing .docx path, relative to the workspace root")
    find: str = Field(
        min_length=1,
        max_length=MAX_FIND_CHARS,
        description=f"LITERAL string to find (1–{MAX_FIND_CHARS} chars); never treated as a regex",
    )
    replace: str = Field(
        max_length=MAX_TEXT_CHARS,
        description=f"Replacement string (≤ {MAX_TEXT_CHARS} chars), inserted literally",
    )
    in_place: bool = Field(default=False, description=_IN_PLACE_DESC)
    out_path: str | None = Field(default=None, description=_OUT_PATH_DESC)


class DocxAddTableIn(_In):
    path: str = Field(description="Existing .docx path, relative to the workspace root")
    rows: list[list[_Text]] = Field(
        min_length=1,
        max_length=MAX_TABLE_ROWS,
        description=(
            f"Table rows (1–{MAX_TABLE_ROWS} rows, 1–{MAX_TABLE_COLS} cols each, "
            f"cells ≤ {MAX_TEXT_CHARS} chars)"
        ),
    )
    in_place: bool = Field(default=False, description=_IN_PLACE_DESC)
    out_path: str | None = Field(default=None, description=_OUT_PATH_DESC)

    @model_validator(mode="after")
    def _uniform_columns(self):
        widths = {len(row) for row in self.rows}
        if len(widths) != 1:
            raise ValueError("all rows must have the same number of columns")
        cols = widths.pop()
        if cols < 1 or cols > MAX_TABLE_COLS:
            raise ValueError(f"tables need 1–{MAX_TABLE_COLS} columns, got {cols}")
        return self


def docx_create(ws, title: str, paragraphs: list[str]) -> dict[str, Any]:
    from docx import Document

    doc = Document()
    doc.add_heading(title, level=0)
    for text in paragraphs:
        doc.add_paragraph(text)
    target = ws.new_output_path("document.docx")
    doc.save(str(target))
    return {
        "output": ws.resource_ref(target, MIME),
        "paragraph_count": len(doc.paragraphs),
    }


def docx_append_paragraph(
    ws, path: str, text: str, in_place: bool = False, out_path: str | None = None
) -> dict[str, Any]:
    doc, resolved = _load(ws, path)
    doc.add_paragraph(text)
    target = _target(ws, in_place, out_path, resolved, "appended.docx")
    doc.save(str(target))
    return {
        "output": ws.resource_ref(target, MIME),
        "paragraph_count": len(doc.paragraphs),
        "in_place": in_place,
    }


def docx_extract_text(ws, path: str) -> dict[str, Any]:
    doc, resolved = _load(ws, path)
    texts = [p.text for p in doc.paragraphs]
    return {
        "paragraphs": texts,
        "paragraph_count": len(texts),
        "size_bytes": Path(resolved).stat().st_size,
    }


def docx_replace_text(
    ws, path: str, find: str, replace: str, in_place: bool = False, out_path: str | None = None
) -> dict[str, Any]:
    doc, resolved = _load(ws, path)
    replacements = 0
    for para in _all_paragraphs(doc):
        if find in para.text:
            replacements += para.text.count(find)
            merged = para.text.replace(find, replace)
            para.clear()
            para.add_run(merged)
    target = _target(ws, in_place, out_path, resolved, "replaced.docx")
    doc.save(str(target))
    return {
        "output": ws.resource_ref(target, MIME),
        "replacements": replacements,
        "in_place": in_place,
    }


def docx_add_table(
    ws, path: str, rows: list[list[str]], in_place: bool = False, out_path: str | None = None
) -> dict[str, Any]:
    doc, resolved = _load(ws, path)
    table = doc.add_table(rows=len(rows), cols=len(rows[0]))
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            table.cell(i, j).text = value
    target = _target(ws, in_place, out_path, resolved, "table.docx")
    doc.save(str(target))
    return {
        "output": ws.resource_ref(target, MIME),
        "rows": len(rows),
        "cols": len(rows[0]),
        "in_place": in_place,
    }


TOOLS = [
    ToolSpec(
        "docx_create",
        f"Create a new Word document from a title (1–{MAX_TITLE_CHARS} chars) and a paragraph list "
        f"(0–{MAX_PARAGRAPHS} paragraphs, each ≤ {MAX_TEXT_CHARS} chars). Output: workspace .docx file.",
        DocxCreateIn,
        docx_create,
        limits={
            "max_title_chars": MAX_TITLE_CHARS,
            "max_paragraphs": MAX_PARAGRAPHS,
            "max_text_chars": MAX_TEXT_CHARS,
        },
    ),
    ToolSpec(
        "docx_append_paragraph",
        f"Append one paragraph (1–{MAX_TEXT_CHARS} chars) to a .docx. Never overwrites the input unless "
        "in_place: true (default: writes a new output file, source unchanged).",
        DocxAppendParagraphIn,
        docx_append_paragraph,
        limits={"max_text_chars": MAX_TEXT_CHARS},
    ),
    ToolSpec(
        "docx_extract_text",
        "Extract all body paragraph texts from a .docx as a list, with the paragraph count and file size. "
        "Limit: the whole document is read; results above 20 MB are rejected (RESULT_TOO_LARGE).",
        DocxExtractTextIn,
        docx_extract_text,
    ),
    ToolSpec(
        "docx_replace_text",
        f"Replace a LITERAL string (never a regex; 1–{MAX_FIND_CHARS} chars) with a literal replacement "
        f"(≤ {MAX_TEXT_CHARS} chars) across body and table paragraphs; reports the replacement count. "
        "Never overwrites the input unless in_place: true.",
        DocxReplaceTextIn,
        docx_replace_text,
        limits={"max_find_chars": MAX_FIND_CHARS, "max_text_chars": MAX_TEXT_CHARS},
    ),
    ToolSpec(
        "docx_add_table",
        f"Append a table (1–{MAX_TABLE_ROWS} rows × 1–{MAX_TABLE_COLS} columns, uniform row widths, "
        f"cells ≤ {MAX_TEXT_CHARS} chars) to a .docx. Never overwrites the input unless in_place: true.",
        DocxAddTableIn,
        docx_add_table,
        limits={"max_rows": MAX_TABLE_ROWS, "max_cols": MAX_TABLE_COLS, "max_text_chars": MAX_TEXT_CHARS},
    ),
]
