# CLI Reference — `protocol-x-tool-mcp`

The `protocol-x-tool-mcp` command is the single entry point for running the server, diagnosing the environment, first-run setup, and converting packages into tools. It is defined in `server/__main__.py` and installed by `pyproject.toml` (`[project.scripts]`).

All snippets below were verified against `--help` output and the source.

```
usage: protocol-x-tool-mcp [-h] [--version]
                           {serve,doctor,setup,convert,version} ...

positional arguments:
  {serve,doctor,setup,convert,version}
    serve               run the MCP server
    doctor              diagnose the environment
    setup               guided first-run setup wizard (P9)
    convert             convert a package into tools (P7)
    version             print version

options:
  -h, --help            show this help message and exit
  --version             print version and exit
```

## Exit codes

| Code | Meaning |
|------|---------|
| `0`  | Success (including `--version`, `version`, `setup`, a clean `doctor`, a merged `convert`, and bare invocations that print help). |
| `1`  | `doctor` found at least one red (FAIL) check. |
| `2`  | Rejection or configuration error: `convert` without a package name, a rejected/erroring conversion, or a `ConfigError` from `Settings` (invalid YAML, bad preset, malformed `PROTOX_TOOL_ACL` / `PROTOX_SECRETS` JSON). The message goes to stderr as `config error: …`. |

## `protocol-x-tool-mcp --version` / `version`

Prints the server version (`VERSION` in `server/app.py`, currently `1.0.0`, matching `pyproject.toml`) and exits `0`. The global flag and the `version` subcommand behave identically.

## `serve` — run the MCP server

```
usage: protocol-x-tool-mcp serve [-h] [--transport {stdio,http}] [--host HOST]
                                 [--port PORT] [--config CONFIG]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--transport {stdio,http}` | value of `server.transport` in config, else `stdio` | `stdio` speaks MCP on stdin/stdout for local MCP clients; `http` serves MCP + the operator console + the REST API over HTTP. |
| `--host HOST` | `127.0.0.1` (`PROTOX_HOST` / `server.host`) | HTTP bind address. |
| `--port PORT` | `7894` (`PROTOX_PORT` / `server.port`) | HTTP port. |
| `--config CONFIG` | auto-discovered (see below) | Path to a `server.yaml`. |

Configuration is loaded by `Settings` (`server/config.py`). Precedence: CLI flags > `PROTOX_*` env vars > config file > policy preset > defaults. With no `--config`, the config file is resolved from `PROTOX_CONFIG`, then `<data_dir>/server.yaml` if it exists (written by `setup`), where `<data_dir>` is `PROTOX_DATA_DIR` or `~/.local/share/protocol-x`. A configuration error prints `config error: …` and exits `2`.

Examples:

```bash
uv run protocol-x-tool-mcp serve                          # stdio, for MCP clients
uv run protocol-x-tool-mcp serve --transport http         # http://127.0.0.1:7894 (+ console)
uv run protocol-x-tool-mcp serve --transport http --host 0.0.0.0 --port 7894 --config ./server.yaml
```

HTTP mode serves: MCP over Streamable HTTP at `/mcp`, the console SPA at `/`, plus `/health`, `/metrics`, and `/api/*` (see `docs/architecture.md`). Bearer auth gates everything except `/health` and `/metrics` when tokens are configured.

## `doctor` — diagnose the environment

```
usage: protocol-x-tool-mcp doctor [-h] [--quick]
```

Runs green/yellow/red checks with remediation hints; exits `1` if any check is red, else `0` (`server/doctor.py`).

`--quick` (pre-flight) checks:

- `python>=3.12` — red below 3.12.
- `posix-rlimits` — red if `resource`/`RLIMIT_AS` is unavailable (non-POSIX → use the Docker profile).
- `port-free` — the configured `host:port` must be bindable.
- `workspace-writable` — the workspace root is creatable and writable.
- `uv-present` — yellow if `uv` is not on PATH (optional; used by `install.sh`).

Full mode (default) additionally checks:

- `sandbox-spawn` — spawns a real sandboxed worker and runs `image_info` on a generated PNG.
- `worker-env-scrubbed` — the worker environment carries none of the configured secrets (INV-7).
- `transport` — boots the ASGI app, verifies `/health`, and verifies `/mcp` returns 401/403 without a token when tokens are configured.
- `token-factory` — yellow with no key (degraded mode, E-20: core tools live, agent features off); green when the endpoint accepts the key; red on 401/403.

```bash
uv run protocol-x-tool-mcp doctor --quick     # pre-flight only
uv run protocol-x-tool-mcp doctor             # full probe
```

## `setup` — guided first-run wizard

```
usage: protocol-x-tool-mcp setup [-h]
```

Interactive wizard (`server/setup.py`): asks one question at a time — policy preset (`network-egress` / `strict` / `permissive-dev`), workspace root, optional Nebius Token Factory API key (blank = degraded mode), judge/fast model names, and whether to regenerate the bearer token. Writes `<data_dir>/server.yaml`, which `Settings` auto-loads on the next run. Idempotent: existing values become the defaults, and the existing token is kept unless regeneration is explicitly confirmed. Always exits `0` on completion.

In a non-interactive shell (no TTY) the wizard does not run; it prints the manual configuration path (copy `configs/server.yaml.example`, or set `PROTOX_*` env vars) and exits `0`.

## `convert` — convert a package into tools (F-029)

```
usage: protocol-x-tool-mcp convert [-h] [--focus FOCUS] [--resume] [--stream]
                                   [--workdir WORKDIR] [--config CONFIG]
                                   [package]
```

| Argument | Description |
|----------|-------------|
| `package` | Installed Python package to convert. Required — omitting it prints `convert requires a package name` and exits `2`. |
| `--focus FOCUS` | Optional natural-language focus for the tool-surface proposal. Accepted and stored on the run; the current heuristic proposer does not filter on it yet. |
| `--resume` | Resume the latest open `conversion_runs` row for the package from its durable checkpoint (skips completed stages). |
| `--stream` | Print each ReAct triple (Thought → Action → Observation) live instead of only at the end. |
| `--workdir WORKDIR` | Artifact/checkpoint directory (default: `<data_dir>/conversions/<module>-<timestamp>`). |
| `--config CONFIG` | Path to `server.yaml`; config errors exit `2`. |

The run is fully autonomous (`server/convert_cli.py` + `agents/converter_agent.py`): introspect the installed package → propose ≤10 tools → codegen the adapter from `templates/adapter.py.jinja` → render the tool-call format from `templates/tool_call_format.jinja` with a bounded auto-fix loop (≤3 cycles) → AI-judge with a bounded repair loop on `blocked` (≤3 cycles) → auto-merge on `pass` / `pass-with-warnings`. A terminal failure is fail-closed: no merge, a machine-readable `rejection.json` in the workdir, a `conversion_runs` row with a `reason_code`, and exit `2`. Success prints the verdict/cycle counts and the tool list and exits `0`.

Without a Token Factory key (`PROTOX_TF_API_KEY`), the run announces degraded mode (E-20): the judge fails closed, so every conversion ends rejected after its bounded cycles. Use `doctor` to verify the key first.

```bash
uv run protocol-x-tool-mcp convert textwrap
uv run protocol-x-tool-mcp convert numpy --focus "array creation" --stream
uv run protocol-x-tool-mcp convert httpx --resume
```

The same pipeline is available to MCP clients as operator tools (`server/operator_tools.py`): `package_install` (download a PyPI package into the server environment), `package_introspect`, `package_convert` (background run, returns a `run_id`), and `package_convert_status` (poll outcome; on merge the new tools are hot-registered and callable without a restart). `package_convert_bulk` converts up to 10 packages in one sequential background batch with per-package failure isolation, polled via `package_convert_status(batch_id=...)`. Like the REST console, these run in the server process rather than the sandbox; bearer auth plus the per-token ACL and rate limit apply.

## `version`

Prints the version and exits `0` — identical to `--version`.

## Standalone converter demo

`agents/converter_agent.py` doubles as a self-contained demo that runs the ReAct pipeline without a server, database, or model credentials (stub seams only):

```
python3 agents/converter_agent.py <package> [options]

--judge {pass,warn,blocked,repair-demo,repair-llm-demo}   demo verdict source;
        omit to FAIL CLOSED (= blocked)
--repairer {llm-demo}        demo LLM repair tier; omit = deterministic strategies only
--proposer {llm-demo}        demo LLM proposal fallback for zero-surface packages
--demo-fault                 break the first tool-call-format render so the
                             auto-fix loop visibly repairs it (cycle 1/3)
--workdir DIR                artifact + checkpoint directory (default: fresh tmp)
--resume                     resume from the checkpoint in --workdir
--stream                     print ReAct triples live as they happen
```

Artifacts (`<module>.py`, `tool_call_format.txt`, `react_trace.jsonl`, `proposals.json`, or `rejection.json`) land in the workdir; the registry merge is simulated. Exit `0` on merge, `2` on rejection — mirroring the production CLI.
