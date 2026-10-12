"""AC-F011: excel_openpyxl tools — range caps, corrupt magic, charts, round-trips."""

from __future__ import annotations

import pytest

from adapters.base import CORRUPT_FILE, RANGE_TOO_LARGE, VALIDATION_ERROR, ToolError

MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture()
def book(settings):
    """5x3 two-sheet workbook in the workspace."""
    from openpyxl import Workbook

    root = settings.workspace_root
    root.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    data = wb.active
    data.title = "Data"
    for r in range(1, 6):
        data.append([r, r * 10, f"row{r}"])
    wb.create_sheet("Empty")
    wb.save(root / "book.xlsx")
    return "book.xlsx"


@pytest.fixture()
def big_book(settings):
    """2000x5 workbook: exactly 10,000 cells (boundary case)."""
    from openpyxl import Workbook

    root = settings.workspace_root
    root.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    big = wb.active
    big.title = "Big"
    for r in range(1, 2001):
        big.append([r, r + 1, r + 2, r + 3, r + 4])
    wb.save(root / "big.xlsx")
    return "big.xlsx"


def _data(res):
    return res["result"]["data"]


# ---------------------------------------------------------------- xlsx_info

def test_xlsx_info_lists_sheets(ctx, book):
    data = _data(ctx.dispatch("xlsx_info", {"path": book}))
    assert data["sheet_count"] == 2
    assert {s["name"] for s in data["sheets"]} == {"Data", "Empty"}
    d = next(s for s in data["sheets"] if s["name"] == "Data")
    assert d["dimension"] == "A1:C5"
    assert d["rows"] == 5 and d["cols"] == 3 and d["cells"] == 15
    assert data["total_cells"] == 15 + 1  # fresh sheet reports A1
    assert data["size_bytes"] > 0


# -------------------------------------------------------- xlsx_read_range

def test_read_range_values(ctx, book):
    data = _data(ctx.dispatch("xlsx_read_range", {"path": book, "sheet": "Data", "range": "A1:C3"}))
    assert data["values"] == [[1, 10, "row1"], [2, 20, "row2"], [3, 30, "row3"]]
    assert data["rows"] == 3 and data["cols"] == 3 and data["cells"] == 9


def test_read_range_single_cell_and_nulls(ctx, book):
    data = _data(ctx.dispatch("xlsx_read_range", {"path": book, "sheet": "Data", "range": "E2"}))
    assert data["values"] == [[None]]
    data = _data(ctx.dispatch("xlsx_read_range", {"path": book, "sheet": "Data", "range": "A1:E2"}))
    assert data["values"][0][3:] == [None, None]  # D1:E1 have no content -> null
    assert data["values"][1][3:] == [None, None]


def test_read_range_past_used_range_returns_nulls(ctx, book):
    # The exact requested rectangle comes back; cells outside the used range are null.
    data = _data(ctx.dispatch("xlsx_read_range", {"path": book, "sheet": "Data", "range": "A1:Z100"}))
    assert data["rows"] == 100 and data["cols"] == 26 and data["cells"] == 2600
    assert data["values"][0][:3] == [1, 10, "row1"]
    assert data["values"][5] == [None] * 26
    assert data["values"][99] == [None] * 26


def test_read_range_exactly_10k_cells_ok(ctx, big_book):
    # AC-F020-03 boundary-inclusive: exactly 10,000 cells must succeed.
    data = _data(
        ctx.dispatch("xlsx_read_range", {"path": big_book, "sheet": "Big", "range": "A1:E2000"})
    )
    assert data["cells"] == 10_000
    assert data["values"][-1] == [2000, 2001, 2002, 2003, 2004]


def test_read_range_10001_cells_rejected(ctx, book):
    # AC-F011-01: over-cap range -> RANGE_TOO_LARGE with the computed cell count.
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("xlsx_read_range", {"path": book, "sheet": "Data", "range": "A1:BU137"})
    assert exc.value.code == RANGE_TOO_LARGE
    assert exc.value.details["requested"] == 10_001  # 137 rows x 73 cols
    assert exc.value.details["limit"] == 10_000
    assert exc.value.details["unit"] == "cells"


def test_read_range_sheet_not_found(ctx, book):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("xlsx_read_range", {"path": book, "sheet": "Nope", "range": "A1:B2"})
    assert exc.value.code == VALIDATION_ERROR


def test_read_range_invalid_range_string(ctx, book):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("xlsx_read_range", {"path": book, "sheet": "Data", "range": "not-a-range"})
    assert exc.value.code == VALIDATION_ERROR


# --------------------------------------------------------- corrupt magic

def test_non_zip_magic_rejected_before_parsing(ctx, settings):
    # AC-F011-03: a .xlsx with non-ZIP magic -> CORRUPT_FILE on every read tool.
    root = settings.workspace_root
    root.mkdir(parents=True, exist_ok=True)
    (root / "fake.xlsx").write_bytes(b"this is definitely not a zip archive, trust me")
    for tool, args in (
        ("xlsx_info", {"path": "fake.xlsx"}),
        ("xlsx_read_range", {"path": "fake.xlsx", "sheet": "Data", "range": "A1:B2"}),
        ("xlsx_write_range", {"path": "fake.xlsx", "sheet": "Data", "start_cell": "A1", "values": [[1]]}),
        ("xlsx_add_chart", {"path": "fake.xlsx", "sheet": "Data", "data_range": "A1:B2",
                            "chart_type": "bar", "title": "t"}),
    ):
        with pytest.raises(ToolError) as exc:
            ctx.dispatch(tool, args)
        assert exc.value.code == CORRUPT_FILE, tool


# --------------------------------------------------------- xlsx_create

def test_create_then_read_roundtrip(ctx):
    res = _data(
        ctx.dispatch(
            "xlsx_create",
            {"sheets": [{"name": "Summary", "rows": [["k", "v"], [1, 2], [True, None]]}]},
        )
    )
    out = res["output"]
    assert out["mime"] == MIME_XLSX
    assert out["size_bytes"] > 0
    read = _data(ctx.dispatch("xlsx_read_range", {"path": out["path"], "sheet": "Summary", "range": "A1:B3"}))
    assert read["values"] == [["k", "v"], [1, 2], [True, None]]


def test_create_multiple_sheets_and_info(ctx):
    res = _data(
        ctx.dispatch(
            "xlsx_create",
            {"sheets": [{"name": "A", "rows": [[1]]}, {"name": "B", "rows": []}]},
        )
    )
    info = _data(ctx.dispatch("xlsx_info", {"path": res["output"]["path"]}))
    assert info["sheet_count"] == 2
    assert [s["name"] for s in info["sheets"]] == ["A", "B"]


def test_create_rejects_duplicate_and_bad_names(ctx):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("xlsx_create", {"sheets": [{"name": "A", "rows": [[1]]}, {"name": "A", "rows": [[2]]}]})
    assert exc.value.code == VALIDATION_ERROR
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("xlsx_create", {"sheets": [{"name": "a/b", "rows": [[1]]}]})
    assert exc.value.code == VALIDATION_ERROR


def test_create_schema_forbids_extra_keys_and_empty(ctx):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("xlsx_create", {"sheets": [{"name": "A", "rows": [[1]], "surprise": 1}]})
    assert exc.value.code == VALIDATION_ERROR
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("xlsx_create", {"sheets": []})
    assert exc.value.code == VALIDATION_ERROR


def test_create_over_cap_cells_rejected(ctx):
    rows = [[i, i + 1] for i in range(50_001)]  # 100,002 cells > 100,000
    with pytest.raises(ToolError) as exc:
        ctx.dispatch("xlsx_create", {"sheets": [{"name": "Big", "rows": rows}]})
    assert exc.value.code == RANGE_TOO_LARGE
    assert exc.value.details["requested"] == 100_002


# ------------------------------------------------------ xlsx_write_range

def test_write_range_default_writes_new_file_and_keeps_source(ctx, book):
    res = _data(
        ctx.dispatch(
            "xlsx_write_range",
            {"path": book, "sheet": "Data", "start_cell": "A1", "values": [["changed", 99]]},
        )
    )
    out_path = res["output"]["path"]
    assert out_path != book
    assert res["output"]["mime"] == MIME_XLSX
    src = _data(ctx.dispatch("xlsx_read_range", {"path": book, "sheet": "Data", "range": "A1:B1"}))
    assert src["values"] == [[1, 10]]  # source untouched
    new = _data(ctx.dispatch("xlsx_read_range", {"path": out_path, "sheet": "Data", "range": "A1:B1"}))
    assert new["values"] == [["changed", 99]]


def test_write_range_in_place_modifies_source(ctx, book):
    _data(
        ctx.dispatch(
            "xlsx_write_range",
            {"path": book, "sheet": "Data", "start_cell": "B2", "values": [[4242]], "in_place": True},
        )
    )
    src = _data(ctx.dispatch("xlsx_read_range", {"path": book, "sheet": "Data", "range": "B2:B2"}))
    assert src["values"] == [[4242]]


def test_write_range_creates_missing_sheet(ctx, book):
    res = _data(
        ctx.dispatch(
            "xlsx_write_range",
            {"path": book, "sheet": "Fresh", "start_cell": "B2", "values": [[1, 2], [3, 4]]},
        )
    )
    read = _data(
        ctx.dispatch("xlsx_read_range", {"path": res["output"]["path"], "sheet": "Fresh", "range": "A1:C3"})
    )
    assert read["values"] == [[None, None, None], [None, 1, 2], [None, 3, 4]]


def test_write_range_over_cap_rejected(ctx, book):
    values = [[i] for i in range(10_001)]  # 10,001 cells > 10,000
    with pytest.raises(ToolError) as exc:
        ctx.dispatch(
            "xlsx_write_range", {"path": book, "sheet": "Data", "start_cell": "A1", "values": values}
        )
    assert exc.value.code == RANGE_TOO_LARGE
    assert exc.value.details["requested"] == 10_001


def test_write_range_rejects_non_scalar_and_empty_row(ctx, book):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch(
            "xlsx_write_range",
            {"path": book, "sheet": "Data", "start_cell": "A1", "values": [[{"nested": 1}]]},
        )
    assert exc.value.code == VALIDATION_ERROR
    with pytest.raises(ToolError) as exc:
        ctx.dispatch(
            "xlsx_write_range", {"path": book, "sheet": "Data", "start_cell": "A1", "values": [[], [1]]}
        )
    assert exc.value.code == VALIDATION_ERROR


def test_write_range_bad_start_cell(ctx, book):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch(
            "xlsx_write_range",
            {"path": book, "sheet": "Data", "start_cell": "A1:B2", "values": [[1]]},
        )
    assert exc.value.code == VALIDATION_ERROR


def test_formula_strings_are_never_evaluated(ctx, book):
    res = _data(
        ctx.dispatch(
            "xlsx_write_range",
            {"path": book, "sheet": "Data", "start_cell": "E1", "values": [["=1+1"]]},
        )
    )
    read = _data(
        ctx.dispatch("xlsx_read_range", {"path": res["output"]["path"], "sheet": "Data", "range": "E1:E1"})
    )
    assert read["values"] == [["=1+1"]]  # stored formula string, not 2


# -------------------------------------------------------- xlsx_add_chart

@pytest.mark.parametrize("chart_type", ["bar", "line", "pie"])
def test_add_chart_embeds_native_chart(ctx, book, settings, chart_type):
    res = _data(
        ctx.dispatch(
            "xlsx_add_chart",
            {
                "path": book,
                "sheet": "Data",
                "data_range": "A1:C5",
                "chart_type": chart_type,
                "title": "Quarterly things",
            },
        )
    )
    out_path = res["output"]["path"]
    assert out_path != book
    from openpyxl import load_workbook

    wb = load_workbook(settings.workspace_root / out_path)
    charts = wb["Data"]._charts
    assert len(charts) == 1  # AC-F011-02: native chart part present
    assert charts[0].title is not None
    # source untouched
    assert len(load_workbook(settings.workspace_root / book)["Data"]._charts) == 0


def test_add_chart_in_place(ctx, book, settings):
    _data(
        ctx.dispatch(
            "xlsx_add_chart",
            {
                "path": book,
                "sheet": "Data",
                "data_range": "A1:B5",
                "chart_type": "line",
                "title": "t",
                "in_place": True,
            },
        )
    )
    from openpyxl import load_workbook

    assert len(load_workbook(settings.workspace_root / book)["Data"]._charts) == 1


def test_add_chart_missing_sheet(ctx, book):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch(
            "xlsx_add_chart",
            {"path": book, "sheet": "Nope", "data_range": "A1:B2", "chart_type": "bar", "title": "t"},
        )
    assert exc.value.code == VALIDATION_ERROR


def test_add_chart_invalid_type_rejected_by_schema(ctx, book):
    with pytest.raises(ToolError) as exc:
        ctx.dispatch(
            "xlsx_add_chart",
            {"path": book, "sheet": "Data", "data_range": "A1:B2", "chart_type": "radar", "title": "t"},
        )
    assert exc.value.code == VALIDATION_ERROR
