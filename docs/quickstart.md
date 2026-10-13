# Quickstart — Protocol-X Tool MCP

From a clean checkout to a working MCP server in about two minutes.

## 1. Install

```bash
./scripts/install.sh        # bootstraps uv (no sudo) + installs dependencies
```

## 2. Configure

```bash
uv run protocol-x-tool-mcp setup
```

The wizard asks for a policy preset (default: `network-egress`), a workspace
root (the only directory tools can touch), an optional Nebius Token Factory
API key (blank = degraded mode: all 34 core tools work; the AI-judge,
converter, and self-repair agents stay off), and generates a bearer token for
the HTTP transport. It writes `<data-dir>/server.yaml` — re-running it is
safe and keeps your existing token.

## 3. Verify

```bash
uv run protocol-x-tool-mcp doctor
```

Full mode probes the sandbox (spawns a worker and runs `image_info` on a real
PNG), checks the worker env is scrubbed, boots the HTTP app and verifies
`/health` + bearer auth, and probes Token Factory reachability when a key is
configured. Any FAIL line comes with a remediation hint.

## 4. Serve

```bash
uv run protocol-x-tool-mcp serve                    # stdio — for MCP clients
uv run protocol-x-tool-mcp serve --transport http   # HTTP + operator console
```

## 5. Try it

With the HTTP transport running (`http://127.0.0.1:7894`):

```bash
curl -s localhost:7894/health
curl -s -H "Authorization: Bearer <token>" localhost:7894/api/status
```

The operator console (registry browser, telemetry, governance, builder) is
served at `/`. Browsers cannot send `Authorization` headers, so open it once
with the bearer token as a query parameter — the console stores it in
localStorage, strips it from the URL, and sends it as a header from then on:

```
http://127.0.0.1:7894/?token=<token>
```

A sample image for the image tools is bundled at
`samples/sample.png` — copy it into your workspace:

```bash
cp samples/sample.png ~/.local/share/protocol-x/workspace/
```

Then ask your MCP client to call `image_info` with `path="sample.png"`.

## Convert a package into tools

```bash
uv run protocol-x-tool-mcp convert textwrap
```

The ReAct converter introspects the installed package, proposes a tool
surface (≤10 tools), codegens a sandboxed adapter, auto-fixes format issues,
and — only when the AI-judge passes — auto-merges it live. Without a Token
Factory key the judge fails closed and the conversion is rejected with a
machine-readable `rejection.json`; every run is resumable via
`--resume` and recorded in the `conversion_runs` table.

## Docker

```bash
cd deploy/docker && docker compose up --build
```

See `docs/environment-variables.md` for every `PROTOX_*` knob and
`docs/architecture.md` for the moving parts.
