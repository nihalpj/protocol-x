"""E2E matrix (AC-P11-02): every launch-core tool exercised with valid args
through BOTH transports — in-process dispatch (stdio path) and a live MCP
Streamable HTTP server with a real fastmcp client.

http_* tools run against a loopback test server on 127.0.0.1, reachable only
because the test allowlists the host (operator egress allowlist semantics);
no internet access is required.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from server.app import build_http_app


@pytest.fixture()
def matrix_files(settings):
    """One valid input file per format family, in the workspace."""
    root = settings.workspace_root
    root.mkdir(parents=True, exist_ok=True)

    from PIL import Image

    Image.new("RGB", (200, 100), (10, 120, 200)).save(root / "sample.png")
    Image.new("RGB", (200, 100), (200, 10, 10)).save(root / "sample.jpg")

    (root / "data.csv").write_text(
        "name,dept,salary\nalice,eng,100\nbob,eng,90\ncara,ops,80\n")
    (root / "other.csv").write_text("name,city\nalice,berlin\ncara,paris\n")

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    with open(root / "blank2.pdf", "wb") as fh:
        writer.write(fh)

    from docx import Document

    doc = Document()
    doc.add_heading("Matrix Doc", level=0)
    doc.add_paragraph("hello matrix world")
    doc.save(root / "doc.docx")

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "S1"
    ws.append(["name", "salary"])
    ws.append(["alice", 100])
    ws.append(["bob", 90])
    wb.save(root / "book.xlsx")
    return root


def _matrix() -> dict[str, dict]:
    return {
        # images_pillow
        "image_info": {"path": "sample.png"},
        "image_resize": {"path": "sample.png", "width": 64, "height": 64},
        "image_rotate": {"path": "sample.png", "degrees": 90},
        "image_convert_format": {"path": "sample.png", "format": "JPEG"},
        "image_crop": {"path": "sample.png", "left": 0, "top": 0,
                       "right": 50, "bottom": 50},
        "image_thumbnail": {"path": "sample.png", "max_size": 64},
        "image_compress_jpeg": {"path": "sample.jpg", "quality": 60},
        "image_strip_metadata": {"path": "sample.jpg"},
        # pdf_pypdf
        "pdf_info": {"path": "blank2.pdf"},
        "pdf_extract_text": {"path": "blank2.pdf"},
        "pdf_extract_images": {"path": "blank2.pdf"},
        "pdf_merge": {"paths": ["blank2.pdf", "blank2.pdf"]},
        "pdf_rotate_pages": {"path": "blank2.pdf", "pages": [1], "degrees": 90},
        "pdf_split": {"path": "blank2.pdf"},
        # docs_docx
        "docx_create": {"title": "E2E", "paragraphs": ["one", "two"]},
        "docx_extract_text": {"path": "doc.docx"},
        "docx_append_paragraph": {"path": "doc.docx", "text": "appended"},
        "docx_replace_text": {"path": "doc.docx", "find": "matrix",
                              "replace": "MATRIX"},
        "docx_add_table": {"path": "doc.docx",
                           "rows": [["h1", "h2"], ["a", "b"]]},
        # excel_openpyxl
        "xlsx_create": {"sheets": [{"name": "S1",
                                    "rows": [["k", "v"], ["a", "1"]]}]},
        "xlsx_info": {"path": "book.xlsx"},
        "xlsx_read_range": {"path": "book.xlsx", "sheet": "S1",
                            "range": "A1:B3"},
        "xlsx_write_range": {"path": "book.xlsx", "sheet": "S1",
                             "start_cell": "A4", "values": [["cara", 80]]},
        "xlsx_add_chart": {"path": "book.xlsx", "sheet": "S1",
                           "data_range": "B2:B3", "chart_type": "bar",
                           "title": "salaries"},
        # data_pandas
        "csv_read": {"path": "data.csv", "limit": 2},
        "csv_info": {"path": "data.csv"},
        "csv_describe": {"path": "data.csv"},
        "csv_filter": {"path": "data.csv", "column": "dept", "op": "eq",
                       "value": "eng"},
        "csv_aggregate": {"path": "data.csv", "group_by": "dept",
                          "aggregations": {"salary": ["sum", "mean"]}},
        "csv_join": {"left": "data.csv", "right": "other.csv", "on": "name"},
        # http_requests (loopback server; host allowlisted by the fixture)
        "http_get": {"url": "LOOPBACK/echo"},
        "http_head": {"url": "LOOPBACK/echo"},
        "http_post": {"url": "LOOPBACK/echo", "json_body": {"ping": "pong"}},
        "http_download_file": {"url": "LOOPBACK/file"},
    }


class _Handler(BaseHTTPRequestHandler):
    def _respond(self):
        body = json.dumps({"method": self.command, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_HEAD = do_POST = _respond

    def log_message(self, *args):  # quiet
        pass


@pytest.fixture()
def loopback(settings, monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(settings, "allowed_hosts", ["127.0.0.1"])
    base = f"http://127.0.0.1:{server.server_port}"
    yield base
    server.shutdown()


def _args(tool: str, base: str) -> dict:
    return {k: (v.replace("LOOPBACK", base) if isinstance(v, str) else v)
            for k, v in _matrix()[tool].items()}


def test_matrix_dispatch_all_tools(ctx, matrix_files, loopback):
    """Phase A: every live tool succeeds through the dispatcher (stdio path)."""
    matrix = _matrix()
    live = {e.name for e in ctx.registry.list_tools()}
    assert set(matrix) <= live, f"matrix missing tools: {live - set(matrix)}"
    for tool in sorted(matrix):
        result = ctx.dispatch(tool, _args(tool, loopback))
        assert "result" in result, f"{tool} returned no result"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio()
async def test_matrix_http_transport_all_tools(ctx, matrix_files, loopback):
    """Phase B: every live tool succeeds via MCP Streamable HTTP."""
    import uvicorn
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    port = _free_port()
    config = uvicorn.Config(
        build_http_app(ctx), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        import asyncio

        await asyncio.sleep(0.05)
    assert server.started, "http server did not start"
    try:
        async with Client(StreamableHttpTransport(
                f"http://127.0.0.1:{port}/mcp")) as client:
            tools = await client.list_tools()
            names = {t.name for t in tools}
            matrix = _matrix()
            assert set(matrix) <= names
            for tool in sorted(matrix):
                result = await client.call_tool(
                    tool, _args(tool, loopback), raise_on_error=False)
                assert not result.is_error, f"{tool} failed over HTTP"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
