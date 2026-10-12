"""pypdf adapter (F-007): 6 PDF tools.

Handlers run INSIDE the sandbox worker. `ws` is the workspace confinement
object passed by the worker; all paths resolve under the workspace root.
Limits are declared in tool descriptions and enforced by the input models
(boundary-inclusive, F-020). PDF magic bytes are checked before parsing;
encrypted inputs need the correct `password` (wrong/missing -> PDF_AUTH_FAILED).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from adapters._guards import check_magic, check_range, clamp_page_range
from adapters.base import (
    CORRUPT_FILE,
    PDF_AUTH_FAILED,
    VALIDATION_ERROR,
    ToolError,
    ToolSpec,
)

MAX_TEXT_PAGES = 50
MIME_PDF = "application/pdf"
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jp2": "image/jp2",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
}


def _open_reader(ws, path: str, password: str | None = None):
    """Magic-check + open a PdfReader, mapping auth/parse failures to typed errors."""
    from pypdf import PdfReader
    from pypdf.errors import FileNotDecryptedError, PyPdfError, WrongPasswordError

    resolved = ws.resolve(path, must_exist=True)
    check_magic(resolved, "pdf")
    try:
        reader = PdfReader(str(resolved))
        if reader.is_encrypted:
            reader.decrypt(password or "")
        total = len(reader.pages)  # force page-tree parse so failures surface here
        return reader, resolved, total
    except (WrongPasswordError, FileNotDecryptedError) as exc:
        raise ToolError(PDF_AUTH_FAILED, "wrong or missing PDF password", {"path": path}) from exc
    except PyPdfError as exc:
        raise ToolError(
            CORRUPT_FILE,
            f"cannot parse PDF ({type(exc).__name__}): {exc}",
            {"path": path},
        ) from exc


def _metadata_dict(reader) -> dict[str, Any] | None:
    meta = reader.metadata
    if meta is None:
        return None
    return {
        "title": meta.title,
        "author": meta.author,
        "subject": meta.subject,
        "creator": meta.creator,
        "producer": meta.producer,
        "creation_date": meta.creation_date.isoformat() if meta.creation_date else None,
        "modification_date": meta.modification_date.isoformat() if meta.modification_date else None,
    }


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PdfInfoIn(_In):
    path: str = Field(description="PDF file path, relative to the workspace root")
    password: str | None = Field(default=None, description="Password for encrypted PDFs")


class PdfExtractTextIn(_In):
    path: str
    start_page: int = Field(default=1, ge=1, description="First page to read, 1-based (≥1)")
    end_page: int | None = Field(
        default=None,
        ge=1,
        description="Last page, 1-based inclusive; defaults to start_page (single page)",
    )
    password: str | None = None


class PdfMergeIn(_In):
    paths: list[str] = Field(min_length=2, description="PDF paths to merge, in argument order")
    password: str | None = Field(default=None, description="Password applied to every source")
    out_path: str | None = Field(default=None, description="Optional output path in workspace")


class PdfSplitIn(_In):
    path: str
    ranges: list[list[int]] | None = Field(
        default=None,
        description="1-based inclusive [start, end] page ranges; default splits one file per page",
    )
    password: str | None = None

    @field_validator("ranges")
    @classmethod
    def _ranges_wellformed(cls, value: list[list[int]] | None) -> list[list[int]] | None:
        if value is None:
            return value
        for rng in value:
            if len(rng) != 2 or rng[0] < 1 or rng[1] < rng[0]:
                raise ValueError("each range must be [start, end] with 1 ≤ start ≤ end")
        return value


class PdfRotatePagesIn(_In):
    path: str
    pages: list[int] = Field(min_length=1, description="1-based page numbers to rotate")
    degrees: Literal[90, 180, 270] = Field(description="Clockwise rotation angle (90, 180 or 270)")
    password: str | None = None
    out_path: str | None = Field(default=None, description="Optional output path in workspace")


class PdfExtractImagesIn(_In):
    path: str
    password: str | None = None


def pdf_info(ws, path: str, password: str | None = None) -> dict[str, Any]:
    reader, resolved, total = _open_reader(ws, path, password)
    return {
        "path": path,
        "pages": total,
        "encrypted": bool(reader.is_encrypted),
        "metadata": _metadata_dict(reader),
        "size_bytes": Path(resolved).stat().st_size,
    }


def pdf_extract_text(
    ws,
    path: str,
    start_page: int = 1,
    end_page: int | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    reader, _, total = _open_reader(ws, path, password)
    end = end_page if end_page is not None else start_page
    if end < start_page:
        raise ToolError(
            VALIDATION_ERROR,
            "end_page must be greater than or equal to start_page",
            {"start_page": start_page, "end_page": end},
        )
    start, end, truncated = clamp_page_range(start_page, end, total)
    check_range(end - start + 1, MAX_TEXT_PAGES, "pages")
    pages = []
    for idx in range(start - 1, end):
        pages.append({"page": idx + 1, "text": reader.pages[idx].extract_text() or ""})
    return {
        "path": path,
        "page_count": total,
        "start_page": start,
        "end_page": end,
        "truncated": truncated,
        "pages": pages,
    }


def pdf_merge(
    ws,
    paths: list[str],
    password: str | None = None,
    out_path: str | None = None,
) -> dict[str, Any]:
    from pypdf import PdfWriter

    readers = [_open_reader(ws, p, password)[0] for p in paths]
    writer = PdfWriter()
    total = 0
    for reader in readers:
        for page in reader.pages:
            writer.add_page(page)
            total += 1
    target = ws.resolve(out_path) if out_path else ws.new_output_path("merged.pdf")
    writer.write(str(target))
    return {
        "output": ws.resource_ref(target, MIME_PDF),
        "pages": total,
        "sources": len(readers),
    }


def pdf_split(
    ws,
    path: str,
    ranges: list[list[int]] | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    from pypdf import PdfWriter

    reader, _, total = _open_reader(ws, path, password)
    ranges = ranges if ranges is not None else [[i, i] for i in range(1, total + 1)]
    for rng in ranges:
        if rng[1] > total:
            raise ToolError(
                VALIDATION_ERROR,
                "split range exceeds document page count",
                {"range": rng, "pages": total},
            )
    outputs = []
    for start, end in ranges:
        writer = PdfWriter()
        for idx in range(start - 1, end):
            writer.add_page(reader.pages[idx])
        suggested = f"page-{start}.pdf" if start == end else f"pages-{start}-{end}.pdf"
        target = ws.new_output_path(suggested)
        writer.write(str(target))
        outputs.append(ws.resource_ref(target, MIME_PDF))
    return {"outputs": outputs, "count": len(outputs)}


def pdf_rotate_pages(
    ws,
    path: str,
    pages: list[int],
    degrees: Literal[90, 180, 270],
    password: str | None = None,
    out_path: str | None = None,
) -> dict[str, Any]:
    from pypdf import PdfWriter

    reader, _, total = _open_reader(ws, path, password)
    targets = sorted({p for p in pages})
    for p in targets:
        if p < 1 or p > total:
            raise ToolError(
                VALIDATION_ERROR,
                "page number out of range",
                {"page": p, "pages": total},
            )
    writer = PdfWriter()
    for idx, page in enumerate(reader.pages, start=1):
        if idx in targets:
            page.rotate(degrees)
        writer.add_page(page)
    target = ws.resolve(out_path) if out_path else ws.new_output_path(f"rotated-{degrees}.pdf")
    writer.write(str(target))
    return {
        "output": ws.resource_ref(target, MIME_PDF),
        "pages_rotated": len(targets),
        "degrees": degrees,
    }


def pdf_extract_images(ws, path: str, password: str | None = None) -> dict[str, Any]:
    reader, _, _ = _open_reader(ws, path, password)
    images = []
    for page_no, page in enumerate(reader.pages, start=1):
        for image in page.images:
            target = ws.new_output_path(image.name)
            target.write_bytes(image.data)
            mime = _IMAGE_MIME.get(target.suffix.lower(), "application/octet-stream")
            images.append(
                {
                    "page": page_no,
                    "name": image.name,
                    "output": ws.resource_ref(target, mime),
                }
            )
    return {"images": images, "count": len(images)}


TOOLS = [
    ToolSpec(
        "pdf_info",
        "Read PDF info: page count, encryption flag, document metadata (title/author/dates). "
        "Input: workspace PDF path; optional password for encrypted files.",
        PdfInfoIn,
        pdf_info,
    ),
    ToolSpec(
        "pdf_extract_text",
        f"Extract text from a page range (start_page–end_page, 1-based inclusive; "
        f"default end_page=start_page). At most {MAX_TEXT_PAGES} pages per call, boundary-inclusive; "
        f"ranges beyond the document are clamped and reported with truncated:true.",
        PdfExtractTextIn,
        pdf_extract_text,
        limits={"max_pages": MAX_TEXT_PAGES},
    ),
    ToolSpec(
        "pdf_merge",
        "Merge 2+ PDFs into one output file, in argument order. Never modifies the sources.",
        PdfMergeIn,
        pdf_merge,
    ),
    ToolSpec(
        "pdf_split",
        "Split a PDF into per-page files (default) or explicit 1-based inclusive [start, end] "
        "page ranges; every range must lie inside the document.",
        PdfSplitIn,
        pdf_split,
    ),
    ToolSpec(
        "pdf_rotate_pages",
        "Rotate selected 1-based pages clockwise by 90, 180 or 270 degrees into a new output "
        "file; the input file is never modified.",
        PdfRotatePagesIn,
        pdf_rotate_pages,
    ),
    ToolSpec(
        "pdf_extract_images",
        "Extract embedded images from every page to workspace files; returns one resource "
        "reference per image.",
        PdfExtractImagesIn,
        pdf_extract_images,
    ),
]
