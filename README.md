# Protocol-X Tool MCP

A security-first MCP server that wraps the most-used Python packages as schema-validated, sandboxed tools — 34 launch-core tools across Pillow, requests, pypdf, pandas, openpyxl, and python-docx — plus an autonomous ReAct converter agent (governed by an NVIDIA Nemotron AI-judge on Nebius Token Factory) that grows the catalog by requirement, with runtime self-repair, a control console at `localhost:7894`, and a hardened Docker profile.

## Quickstart

```bash
uv sync
uv run protocol-x-tool-mcp serve --transport stdio
# or HTTP + console:
uv run protocol-x-tool-mcp serve --transport http   # http://127.0.0.1:7894
```

See `docs/quickstart.md` for the full install → first-call flow (< 5 minutes).

## Layout

- `server/` — MCP app (FastMCP), transports, auth/ACL, rate limit, registry, health, config
- `core/` — sandbox runner + worker pool, workspace confinement, serialization, secrets, sizing, SQLite store, audit, logging
- `agents/` — Token Factory client, Nemotron AI-judge, ReAct converter, runtime self-repair
- `adapters/` — auto-discovered tool adapters (built-ins + converter-generated)
- `console/` — React + TypeScript control console (served by the server from `console/dist/`)
- `templates/` — Jinja codegen/rendering templates · `migrations/` — SQLite schema · `policies/` — sandbox presets
- `scripts/` — operator scripts (install, doctor, per-use-case tests) · `docs/` — full documentation

## How we used NVIDIA Nemotron + Nebius Token Factory / AI Cloud

All agent inference — the AI-judge (F-019), the ReAct package converter (F-029), and the runtime self-repair agent (F-030) — calls **NVIDIA Nemotron open models via Nebius Token Factory** (Nemotron 3 Ultra for judging and codegen escalation; Nano/Super for fast converter/repair loops). The server, sandbox, and console deploy on **Nebius AI Cloud** VMs via the hardened Docker profile. Endpoint and model names are configurable via `PROTOX_TF_*` env vars; without a key the server runs in degraded mode with the 34 core tools fully live and agent features explicitly disabled.

## Verify

```bash
uv run pytest -q          # full suite (unit, integration, security, adversarial, e2e, benchmarks)
uv run ruff check         # lint
uv run mypy server core agents adapters  # types
uv run lint-imports       # layering contract: server -> agents -> core -> adapters
scripts/test.sh           # everything above + per-use-case scripts -> docs/test-results.md
scripts/check-no-telemetry.sh            # privacy gate: zero phone-home references
```

Per-use-case suites: `scripts/test-uc-01.sh` … `test-uc-07.sh` (guides in `docs/use-cases/`; uc-06 green-skips until the v1.1 polyglot backends land).

## Documentation

- `docs/quickstart.md` — install → first call in minutes
- `docs/cli-reference.md` · `docs/environment-variables.md` — complete operator references
- `docs/features.md` · `docs/architecture.md` · `docs/security.md` — capabilities, module tree + agent loops, threat model (INV-1…8)
- `docs/use-cases/uc-01…07.md` — persona walkthroughs · `docs/contributing.md` — dev setup and PR gates

## License

Apache-2.0 — see `LICENSE`.
