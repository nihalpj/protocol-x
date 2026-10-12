"""PDF adapter (pypdf) integration tests.

Fixture PDFs are generated programmatically with pypdf: blank pages,
real text via a hand-built Helvetica content stream, encrypted writers,
and a raw RGB image XObject. No external files needed.
"""

from __future__ import annotations

import pytest

from adapters.base import (
    CORRUPT_FILE,
    PDF_AUTH_FAILED,
    RANGE_TOO_LARGE,
    VALIDATION_ERROR,
    ToolError,
)

# ---------------------------------------------------------------------------
# fixture builders (pypdf only)
# ---------------------------------------------------------------------------


def _write_blank_pdf(path, pages: int, metadata: dict | None = None) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    if metadata:
        writer.add_metadata(metadata)
    writer.write(str(path))


def _write_text_pdf(path, page_texts: list[str], metadata: dict | None = None) -> None:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    for text in page_texts:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject()
        font[NameObject("/Type")] = NameObject("/Font")
        font[NameObject("/Subtype")] = NameObject("/Type1")
        font[NameObject("/BaseFont")] = NameObject("/Helvetica")
        resources = DictionaryObject()
        resources[NameObject("/Font")] = DictionaryObject(
            {NameObject("/F1"): writer._add_object(font)}
        )
        page[NameObject("/Resources")] = resources
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 24 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(content)
    if metadata:
        writer.add_metadata(metadata)
    writer.write(str(path))


def _write_encrypted_pdf(path, password: str, pages: int = 1) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    writer.encrypt(password)
    writer.write(str(path))


def _write_image_pdf(path) -> None:
    from pypdf import PdfWriter
    from pypdf.generic import (
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
        NumberObject,
    )

    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    width, height = 8, 6
    raw = bytes(b for i in range(width * height) for b in (i % 256, (i * 5) % 256, (i * 11) % 256))
    image = DecodedStreamObject()
    image.set_data(raw)
    image[NameObject("/Type")] = NameObject("/XObject")
    image[NameObject("/Subtype")] = NameObject("/Image")
    image[NameObject("/Width")] = NumberObject(width)
    image[NameObject("/Height")] = NumberObject(height)
    image[NameObject("/ColorSpace")] = NameObject("/DeviceRGB")
    image[NameObject("/BitsPerComponent")] = NumberObject(8)
    resources = DictionaryObject()
    resources[NameObject("/XObject")] = DictionaryObject(
        {NameObject("/Im0"): writer._add_object(image)}
    )
    page[NameObject("/Resources")] = resources
    content = DecodedStreamObject()
    content.set_data(b"q 200 0 0 150 0 0 cm /Im0 Do Q")
    page[NameObject("/Contents")] = writer._add_object(content)
    writer.write(str(path))


def _ws_path(settings, name: str):
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    return settings.workspace_root / name


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def blank3(settings):
    path = _ws_path(settings, "blank3.pdf")
    _write_blank_pdf(path, 3, metadata={"/Title": "Blank Three", "/Author": "Protox Tests"})
    return "blank3.pdf"


@pytest.fixture()
def blank60(settings):
    path = _ws_path(settings, "blank60.pdf")
    _write_blank_pdf(path, 60)
    return "blank60.pdf"


@pytest.fixture()
def text2(settings):
    path = _ws_path(settings, "text2.pdf")
    _write_text_pdf(path, ["Hello Protocol X", "Second page text"])
    return "text2.pdf"


@pytest.fixture()
def encrypted(settings):
    path = _ws_path(settings, "encrypted.pdf")
    _write_encrypted_pdf(path, "s3cret", pages=2)
    return "encrypted.pdf"


@pytest.fixture()
def image_pdf(settings):
    path = _ws_path(settings, "image.pdf")
    _write_image_pdf(path)
    return "image.pdf"


@pytest.fixture()
def corrupt_pdf(settings):
    path = _ws_path(settings, "corrupt.pdf")
    path.write_bytes(b"%PDF-1.7\n" + bytes(range(256)) * 8 + b"\n%%EOF")
    return "corrupt.pdf"


def _data(dispatch_result) -> dict:
    return dispatch_result["result"]["data"]


# ---------------------------------------------------------------------------
# pdf_info
# ---------------------------------------------------------------------------


def test_pdf_info_blank(ctx, blank3):
    data = _data(ctx.dispatch("pdf_info", {"path": blank3}))
    assert data["pages"] == 3
    assert data["encrypted"] is False
    assert data["metadata"]["title"] == "Blank Three"
    assert data["metadata"]["author"] == "Protox Tests"
    assert data["size_bytes"] > 0


def test_pdf_info_encrypted_flag(ctx, encrypted):
    data = _data(ctx.dispatch("pdf_info", {"path": encrypted, "password": "s3cret"}))
    assert data["encrypted"] is True
    assert data["pages"] == 2


def test_pdf_info_wrong_password(ctx, encrypted):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("pdf_info", {"path": encrypted, "password": "nope"})
    assert exc.value.code == PDF_AUTH_FAILED


def test_pdf_info_missing_password(ctx, encrypted):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("pdf_info", {"path": encrypted})
    assert exc.value.code == PDF_AUTH_FAILED


# ---------------------------------------------------------------------------
# pdf_extract_text
# ---------------------------------------------------------------------------


def test_extract_text_two_pages(ctx, text2):
    data = _data(
        ctx.dispatch("pdf_extract_text", {"path": text2, "start_page": 1, "end_page": 2})
    )
    assert data["page_count"] == 2
    assert data["truncated"] is False
    assert [p["page"] for p in data["pages"]] == [1, 2]
    assert "Hello Protocol X" in data["pages"][0]["text"]
    assert "Second page text" in data["pages"][1]["text"]


def test_extract_text_defaults_to_single_page(ctx, text2):
    data = _data(ctx.dispatch("pdf_extract_text", {"path": text2}))
    assert data["start_page"] == 1
    assert data["end_page"] == 1
    assert len(data["pages"]) == 1
    assert "Hello Protocol X" in data["pages"][0]["text"]


def test_extract_range_clamped_with_truncated(ctx, blank3):
    data = _data(
        ctx.dispatch("pdf_extract_text", {"path": blank3, "start_page": 1, "end_page": 200})
    )
    assert data["truncated"] is True
    assert data["start_page"] == 1
    assert data["end_page"] == 3
    assert len(data["pages"]) == 3
    assert all(p["text"] == "" for p in data["pages"])  # blank pages -> empty text


def test_extract_range_boundary_exactly_50_ok(ctx, blank60):
    data = _data(
        ctx.dispatch("pdf_extract_text", {"path": blank60, "start_page": 1, "end_page": 50})
    )
    assert data["truncated"] is False
    assert len(data["pages"]) == 50


def test_extract_range_over_50_rejected(ctx, blank60):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("pdf_extract_text", {"path": blank60, "start_page": 5, "end_page": 60})
    assert exc.value.code == RANGE_TOO_LARGE
    assert exc.value.details["requested"] == 56
    assert exc.value.details["limit"] == 50


def test_extract_text_end_before_start(ctx, blank3):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("pdf_extract_text", {"path": blank3, "start_page": 3, "end_page": 1})
    assert exc.value.code == VALIDATION_ERROR


def test_extract_text_encrypted_with_password(ctx, encrypted):
    data = _data(
        ctx.dispatch(
            "pdf_extract_text",
            {"path": encrypted, "password": "s3cret", "start_page": 1, "end_page": 2},
        )
    )
    assert data["page_count"] == 2
    assert len(data["pages"]) == 2


# ---------------------------------------------------------------------------
# pdf_merge
# ---------------------------------------------------------------------------


def test_pdf_merge_three_files(ctx, settings):
    for name in ("m1.pdf", "m2.pdf", "m3.pdf"):
        _write_blank_pdf(_ws_path(settings, name), 2)
    data = _data(ctx.dispatch("pdf_merge", {"paths": ["m1.pdf", "m2.pdf", "m3.pdf"]}))
    assert data["pages"] == 6
    assert data["sources"] == 3
    merged_ref = data["output"]
    assert merged_ref["mime"] == "application/pdf"
    info = _data(ctx.dispatch("pdf_info", {"path": merged_ref["path"]}))
    assert info["pages"] == 6


def test_pdf_merge_single_file_rejected(ctx, blank3):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("pdf_merge", {"paths": ["blank3.pdf"]})
    assert exc.value.code == VALIDATION_ERROR


def test_pdf_merge_missing_source(ctx, blank3):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("pdf_merge", {"paths": ["blank3.pdf", "nope.pdf"]})
    assert exc.value.code == VALIDATION_ERROR


# ---------------------------------------------------------------------------
# pdf_split
# ---------------------------------------------------------------------------


def test_pdf_split_default_one_file_per_page(ctx, blank3):
    data = _data(ctx.dispatch("pdf_split", {"path": blank3}))
    assert data["count"] == 3
    assert len(data["outputs"]) == 3
    for ref in data["outputs"]:
        info = _data(ctx.dispatch("pdf_info", {"path": ref["path"]}))
        assert info["pages"] == 1


def test_pdf_split_explicit_ranges(ctx, blank3):
    data = _data(
        ctx.dispatch("pdf_split", {"path": blank3, "ranges": [[1, 2], [3, 3]]})
    )
    assert data["count"] == 2
    pages = [
        _data(ctx.dispatch("pdf_info", {"path": ref["path"]}))["pages"]
        for ref in data["outputs"]
    ]
    assert pages == [2, 1]


def test_pdf_split_range_out_of_bounds(ctx, blank3):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("pdf_split", {"path": blank3, "ranges": [[2, 9]]})
    assert exc.value.code == VALIDATION_ERROR


def test_pdf_split_malformed_range(ctx, blank3):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("pdf_split", {"path": blank3, "ranges": [[0, 1]]})
    assert exc.value.code == VALIDATION_ERROR
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("pdf_split", {"path": blank3, "ranges": [[2, 1]]})
    assert exc.value.code == VALIDATION_ERROR


# ---------------------------------------------------------------------------
# pdf_rotate_pages
# ---------------------------------------------------------------------------


def test_pdf_rotate_pages(ctx, settings, blank3):
    data = _data(
        ctx.dispatch("pdf_rotate_pages", {"path": blank3, "pages": [1, 3], "degrees": 90})
    )
    assert data["pages_rotated"] == 2
    assert data["degrees"] == 90
    out_abs = settings.workspace_root / data["output"]["path"]
    from pypdf import PdfReader

    rotations = [page.rotation for page in PdfReader(str(out_abs)).pages]
    assert rotations == [90, 0, 90]
    # input untouched
    assert [page.rotation for page in PdfReader(str(settings.workspace_root / blank3)).pages] == [0, 0, 0]


def test_pdf_rotate_invalid_degrees(ctx, blank3):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("pdf_rotate_pages", {"path": blank3, "pages": [1], "degrees": 45})
    assert exc.value.code == VALIDATION_ERROR


def test_pdf_rotate_page_out_of_range(ctx, blank3):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("pdf_rotate_pages", {"path": blank3, "pages": [9], "degrees": 90})
    assert exc.value.code == VALIDATION_ERROR


# ---------------------------------------------------------------------------
# pdf_extract_images
# ---------------------------------------------------------------------------


def test_pdf_extract_images(ctx, settings, image_pdf):
    data = _data(ctx.dispatch("pdf_extract_images", {"path": image_pdf}))
    assert data["count"] == 1
    entry = data["images"][0]
    assert entry["page"] == 1
    assert entry["name"] == "Im0.png"
    out_abs = settings.workspace_root / entry["output"]["path"]
    assert out_abs.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert entry["output"]["mime"] == "image/png"


def test_pdf_extract_images_blank_pdf_returns_empty(ctx, blank3):
    data = _data(ctx.dispatch("pdf_extract_images", {"path": blank3}))
    assert data["count"] == 0
    assert data["images"] == []


# ---------------------------------------------------------------------------
# corrupt files + schema hardening
# ---------------------------------------------------------------------------


def test_corrupt_file_info(ctx, corrupt_pdf):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("pdf_info", {"path": corrupt_pdf})
    assert exc.value.code == CORRUPT_FILE


def test_corrupt_file_extract(ctx, corrupt_pdf):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("pdf_extract_text", {"path": corrupt_pdf})
    assert exc.value.code == CORRUPT_FILE


def test_corrupt_file_merge(ctx, corrupt_pdf, blank3):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("pdf_merge", {"paths": [corrupt_pdf, blank3]})
    assert exc.value.code == CORRUPT_FILE


def test_wrong_password_extract(ctx, encrypted):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("pdf_extract_text", {"path": encrypted, "password": "bad"})
    assert exc.value.code == PDF_AUTH_FAILED


def test_extra_arg_rejected(ctx, blank3):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("pdf_info", {"path": blank3, "surprise": 1})
    assert exc.value.code == VALIDATION_ERROR
