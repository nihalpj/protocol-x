# Connecting MCP Clients (Claude, Kimi, Cursor, and other agent tools)

Protocol-X Tool MCP speaks the standard [Model Context Protocol](https://modelcontextprotocol.io),
so any MCP-capable agent/IDE can use its 34 tools. There are two ways to connect:

| Transport | When to use | Auth |
|---|---|---|
| **stdio** — the client launches `protocol-x-tool-mcp serve` as a child process | Local desktop clients (Claude Desktop, Kimi Code, Cursor, …). Recommended default. | None needed (local-trusted channel) |
| **streamable-HTTP** — the client connects to a running server at `http://<host>:7894/mcp` | Remote/VM deployments, shared servers, or a server you keep running yourself | Bearer token (when tokens are configured) |

> The console's **Snippets** tab (`http://127.0.0.1:7894/?token=…`) shows these same
> configs with your actual host/port filled in (`GET /api/snippets`).

## Prerequisites

```bash
cd /mnt/storage25/Protocal-x/protocol-x-tool-mcp
uv sync                                     # once
uv run protocol-x-tool-mcp doctor           # all checks green before connecting clients
```

If you configured the server with a custom data dir (as on this machine), every
stdio client entry must pass it through — otherwise the server starts with a fresh
default config in `$HOME/.local/share/protocol-x`:

```
PROTOX_DATA_DIR=/mnt/storage25/Protocal-x/protox-data
```

---

## Claude Desktop

Edit the config file for your OS:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "protocol-x": {
      "command": "uv",
      "args": ["run", "protocol-x-tool-mcp", "serve", "--transport", "stdio"],
      "cwd": "/mnt/storage25/Protocal-x/protocol-x-tool-mcp",
      "env": {
        "PROTOX_DATA_DIR": "/mnt/storage25/Protocal-x/protox-data"
      }
    }
  }
}
```

Restart Claude Desktop completely, then look for the 🔌 tools icon — the
`image_*`, `csv_*`, `pdf_*`, `xlsx_*`, `docx_*`, `http_*` tools should be listed.

## Claude Code

```bash
# stdio (recommended for local use)
claude mcp add protocol-x \
  --env PROTOX_DATA_DIR=/mnt/storage25/Protocal-x/protox-data \
  -- uv run --project /mnt/storage25/Protocal-x/protocol-x-tool-mcp protocol-x-tool-mcp serve

# or HTTP, against an already-running server (./start)
claude mcp add --transport http protocol-x http://127.0.0.1:7894/mcp \
  --header "Authorization: Bearer <token>"
```

Verify inside a session with `/mcp` — `protocol-x` should show as connected.

## Kimi Code CLI

MCP servers live in `mcp.json` — user-level `~/.kimi-code/mcp.json` (all projects)
or project-level `.kimi-code/mcp.json` (this repo only). You can also add servers
interactively with the `/mcp-config` command in the TUI.

stdio:

```json
{
  "mcpServers": {
    "protocol-x": {
      "command": "uv",
      "args": ["run", "protocol-x-tool-mcp", "serve", "--transport", "stdio"],
      "cwd": "/mnt/storage25/Protocal-x/protocol-x-tool-mcp",
      "env": {
        "PROTOX_DATA_DIR": "/mnt/storage25/Protocal-x/protox-data"
      }
    }
  }
}
```

HTTP (server already running via `./start`):

```json
{
  "mcpServers": {
    "protocol-x": {
      "url": "http://127.0.0.1:7894/mcp",
      "headers": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```

or keep the token out of the file with `"bearerTokenEnvVar": "PROTOX_TOKEN"`
and `export PROTOX_TOKEN=<token>` in your shell.

Tools appear as `mcp__protocol_x__<tool>` (e.g. `mcp__protocol_x__image_resize`).
Check connection status with `/mcp`. New servers only join **new** sessions. When
a project-level `mcp.json` is in an untrusted folder, Kimi Code shows the launch
command in the trust prompt first — review and confirm.

## Cursor

Add to `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project):

```json
{
  "mcpServers": {
    "protocol-x": {
      "command": "uv",
      "args": ["run", "protocol-x-tool-mcp", "serve", "--transport", "stdio"],
      "cwd": "/mnt/storage25/Protocal-x/protocol-x-tool-mcp",
      "env": { "PROTOX_DATA_DIR": "/mnt/storage25/Protocal-x/protox-data" }
    }
  }
}
```

Then enable it under Settings → MCP. Cursor also accepts `{ "url": "http://127.0.0.1:7894/mcp", "headers": { "Authorization": "Bearer <token>" } }` entries for HTTP.

## Other agent IDEs (Windsurf, Antigravity, Zed, …)

Any client with an `mcpServers`-style config takes the same two shapes:

- **stdio**: `command: "uv"`, `args: ["run", "protocol-x-tool-mcp", "serve", "--transport", "stdio"]`,
  with `cwd` set to the repo and `PROTOX_DATA_DIR` in `env`.
- **HTTP**: `url: "http://<host>:7894/mcp"` with an `Authorization: Bearer <token>` header.

## Custom agents / frameworks (raw HTTP)

For LangGraph, AutoGen, your own agent loop, or plain curl — speak MCP
streamable-HTTP directly:

```bash
# 1. handshake
curl -s -X POST http://127.0.0.1:7894/mcp \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2024-11-05","capabilities":{},
                 "clientInfo":{"name":"my-agent","version":"0.1"}}}'

# 2. list tools / call a tool (same endpoint, after notifications/initialized)
curl -s -X POST http://127.0.0.1:7894/mcp \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"image_info","arguments":{"path":"sample.png"}}}'
```

Or use an SDK — `fastmcp.Client`, the official `@modelcontextprotocol/sdk`, or the
Python `mcp` package — pointed at the same URL with the same header.
Workspace-relative paths resolve inside the configured workspace sandbox
(`protox-workspace://…` resource refs are returned for large/binary outputs).

## The bearer token

HTTP mode requires the token created by `protocol-x-tool-mcp setup` (or
`PROTOX_TOKENS`). `./start` prints it on every launch:

```
bearer token: ITcMES…        ← paste this where a config says <token>
```

Token ACLs (`PROTOX_TOOL_ACL`) can restrict a token to a subset of tools —
give each client only what it needs.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Client shows 0 tools / connect fails | Run `uv run protocol-x-tool-mcp doctor`; check the client's MCP logs |
| stdio server starts but empty config | Missing `PROTOX_DATA_DIR` in the client's `env` — it started with defaults in `$HOME` |
| HTTP 401 `UNAUTHORIZED` | Wrong/missing `Authorization: Bearer <token>` header |
| `TOOL_NOT_PERMITTED` on some calls | The token's ACL excludes that tool (`PROTOX_TOOL_ACL`) |
| `VALIDATION_ERROR` on file paths | Paths are confined to the workspace sandbox; use workspace-relative paths |
| Agent features (convert/builder) disabled | No Token Factory key — degraded mode (E-20); set `PROTOX_TF_API_KEY` |
| Changes to `mcp.json` not picked up | Restart the client / start a new session (servers join new sessions only) |
