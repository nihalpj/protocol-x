import asyncio
import json
import sys

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

URL = "http://127.0.0.1:7894/mcp"
HEADERS = {"Authorization": "Bearer ***REMOVED***"}

PASS, FAIL = 0, 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail[:200]}")


async def call(client, tool, args):
    r = await client.call_tool(tool, args)
    text = r.content[0].text
    return json.loads(text)


async def main():
    transport = StreamableHttpTransport(URL, headers=HEADERS)
    async with Client(transport) as client:
        tools = await client.list_tools()
        names = sorted(t.name for t in tools)
        print(f"tools: {len(names)}")
        check("45 tools registered", len(names) == 45, str(names))

        print("\n-- images --")
        r = await call(client, "image_info", {"path": "sample.png"})
        check("image_info", "result" in str(r) and r.get("result", {}).get("data"), str(r))
        r = await call(
            client, "image_thumbnail", {"path": "sample.png", "max_size": 64, "out_path": "thumb2.png"}
        )
        check("image_thumbnail", "thumb2" in str(r), str(r))
        r = await call(client, "image_rotate", {"path": "sample.png", "degrees": 90, "out_path": "rot90.png"})
        check("image_rotate", "rot90" in str(r), str(r))

        print("\n-- csv --")
        r = await call(client, "csv_describe", {"path": "data.csv"})
        check("csv_describe", "result" in str(r), str(r))
        r = await call(
            client,
            "csv_filter",
            {"path": "data.csv", "column": "age", "op": "gt", "value": 30, "out_path": "filtered.csv"},
        )
        check("csv_filter", "filtered" in str(r), str(r))
        r = await call(
            client,
            "csv_aggregate",
            {"path": "data.csv", "group_by": "city", "aggregations": {"age": ["mean"]}},
        )
        check("csv_aggregate", "mean" in str(r), str(r))

        print("\n-- pdf --")
        r = await call(client, "pdf_info", {"path": "doc.pdf"})
        check("pdf_info", "result" in str(r), str(r))
        r = await call(client, "pdf_extract_text", {"path": "doc.pdf"})
        check("pdf_extract_text", "result" in str(r), str(r))
        r = await call(client, "pdf_merge", {"paths": ["doc.pdf", "doc.pdf"], "out_path": "merged2.pdf"})
        check("pdf_merge", "merged2" in str(r), str(r))

        print("\n-- docx --")
        r = await call(client, "docx_create", {"title": "MCP Test", "paragraphs": ["hello from mcp test"]})
        docx_out = json.dumps(r)
        check("docx_create", ".docx" in docx_out, str(r))
        docx_path = r["result"]["data"]["output"]["path"] if isinstance(r.get("result"), dict) else None
        r = await call(client, "docx_extract_text", {"path": docx_path})
        check("docx_extract_text", "hello from mcp test" in str(r), str(r))

        print("\n-- xlsx --")
        r = await call(client, "xlsx_create", {"sheets": [{"name": "S1", "rows": [["a", "b"], [1, 2]]}]})
        check("xlsx_create", ".xlsx" in json.dumps(r), str(r))
        xlsx_path = r["result"]["data"]["output"]["path"] if isinstance(r.get("result"), dict) else None
        r = await call(client, "xlsx_info", {"path": xlsx_path})
        check("xlsx_info", "S1" in str(r), str(r))
        r = await call(client, "xlsx_read_range", {"path": xlsx_path, "sheet": "S1", "range": "A1:B2"})
        check("xlsx_read_range", "result" in str(r), str(r))

        print("\n-- http (built-in, SSRF-guarded) --")
        r = await call(client, "http_get", {"url": "https://api.github.com/zen"})
        check("http_get", "200" in str(r), str(r))

        print("\n-- httpx (generated adapter) --")
        r = await call(client, "httpx_get", {"url": "https://api.github.com/zen"})
        data = r.get("result", {}).get("data", {}).get("result", {})
        check("httpx_get", data.get("status_code") == 200 and "text" in data, str(r))
        r = await call(
            client, "httpx_get", {"url": "https://httpbin.org/headers", "headers": {"X-Test": "protx"}}
        )
        check("httpx_get+headers", "protx" in str(r), str(r))
        r = await call(client, "httpx_post", {"url": "https://httpbin.org/post", "json": {"k": "v"}})
        check("httpx_post+json", '"k": "v"' in str(r) or '"k":"v"' in str(r).replace(" ", ""), str(r))
        r = await call(client, "httpx_head", {"url": "https://api.github.com"})
        check("httpx_head", "200" in str(r), str(r))

        print("\n-- reportlab --")
        r = await call(client, "reportlab_cmp", {"a": "alpha", "b": "beta"})
        check("reportlab_cmp", "result" in str(r), str(r))

        print("\n-- error paths (must raise ToolError) --")
        for name, tool, args in [
            ("missing file", "image_info", {"path": "nope.png"}),
            ("workspace escape", "image_info", {"path": "../etc/passwd"}),
            ("unknown tool", "no_such_tool", {}),
        ]:
            try:
                await client.call_tool(tool, args)
                check(f"error: {name}", False, "no error raised")
            except Exception as exc:
                check(f"error: {name}", True, str(exc)[:100])

    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
