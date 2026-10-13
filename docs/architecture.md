# Architecture

Module tree, request flow, agent loops, and key classes — all verified against the source. The layering contract (enforced by import-linter) is:

```
server  ->  agents  ->  core  ->  adapters
```

`adapters/base.py` imports nothing from the layers above it; `agents/` never imports `server/` (composition roots like `server/convert_cli.py` live in `server/`).

## Module tree

```
protocol-x-tool-mcp/
├── server/                    MCP app + HTTP/stdio transports + operator API
│   ├── __main__.py            CLI entry point (serve/doctor/setup/convert/version)
│   ├── app.py                 FastMCP instance, AppContext, dispatch(), /health, /metrics
│   ├── config.py              Settings: env + server.yaml + policy preset merge
│   ├── registry.py            ToolRegistry: lazy adapter discovery, catalog cache
│   ├── auth.py                BearerAuthMiddleware, TokenStore, per-tool ACL
│   ├── ratelimit.py           sliding-window RateLimiter
│   ├── doctor.py              environment diagnostics (quick + full)
│   ├── setup.py               guided first-run wizard
│   ├── api.py                 console REST API (/api/*) + SPA static serving
│   ├── convert_cli.py         production `convert` composition root
│   └── errors.py              ToolError -> MCP error mapping
├── agents/                    agent inference + governance (Token Factory only)
│   ├── tokenfactory.py        OpenAI-compatible TokenFactoryClient (retries, prompt hash)
│   ├── judge.py               AI-judge vs inv-rubric v1.2, fail-closed
│   ├── lint.py                INV-1 AST linter (pre-judge deterministic gate)
│   ├── codegen.py             introspection + Jinja render + static gate + Builder
│   ├── converter_agent.py     ReAct converter pipeline (F-029) + standalone demo
│   ├── repair_agent.py        runtime self-repair (F-030): capture/patch/rollback/breaker
│   └── react.py               ReActTracer: triples, streaming, checkpoints
├── core/                      sandbox + shared infrastructure
│   ├── sandbox/
│   │   ├── runner.py          SubprocessRunner: spawn-per-call, scrubbed env
│   │   ├── pool.py            PooledRunner: warm worker pool (rlimit buckets)
│   │   ├── worker_main.py     in-worker entry (one-shot + --serve loop)
│   │   ├── limits.py          POSIX rlimit presets (spawn + pool variants)
│   │   └── backends/          worker runtime backends (python; node/go/rust seam)
│   ├── workspace.py           Workspace confinement (path resolution, resource refs)
│   ├── serialization.py       1 MB inline / 20 MB hard caps, base64+MIME, persistence
│   ├── secrets.py             SecretsBroker: per-call injection, scrub/redact helpers
│   ├── sizing.py              F-038 host probe + compute profile
│   ├── audit.py               AuditLedger: SQLite + JSONL dual-write
│   ├── db.py                  Store + typed DAOs (the only SQL call sites)
│   ├── hashing.py             module source hash (version identity)
│   └── logging.py             JsonlLogger + Prometheus Metrics
├── adapters/                  tool adapters (auto-discovered; TOOLS or get_tools())
│   ├── base.py                ToolSpec, ToolError, BinaryResult, canonical codes
│   ├── _urlguard.py           SSRF guard (stdlib http.client only)
│   ├── _guards.py             shared size guards + magic-byte checks
│   └── images_pillow.py / http_requests.py / pdf_pypdf.py /
│       data_pandas.py / excel_openpyxl.py / docs_docx.py
├── console/                   React+TS operator SPA (served from console/dist/)
├── policies/                  sandbox presets + inv-rubric v1.2 (+ openshell/ profile docs)
├── templates/                 adapter.py.jinja, tool_call_format.jinja, tool_result.jinja
├── migrations/                m001_initial.sql, m002_repair_healed.sql
├── deploy/docker/             Dockerfile, compose, compute-env.sh (F-038)
├── scripts/                   operator scripts (install/start/stop/status/doctor)
├── configs/server.yaml.example
├── samples/sample.png         bundled first-call sample
└── tests/                     unit / integration / security / adversarial / benchmarks
```

## Tool-call request flow

```
MCP client (stdio)          agent (HTTP :7894)
      │                            │
      └────────► FastMCP tool fn ◄─┘                    server/app.py _make_tool()
                      │ asyncio.to_thread
                      ▼
              AppContext.dispatch(tool, args)             server/app.py
                      │
        1 ──► token_store.check_acl(token, tool)         TOOL_NOT_PERMITTED   server/auth.py
        2 ──► rate_limiter.check(token)                  RATE_LIMITED         server/ratelimit.py
        3 ──► registry.get_spec(tool)                    UNKNOWN_TOOL / TOOL_DISABLED
                      │  (lazy-imports the adapter on first use, F-037)
        4 ──► secrets.inject(spec, args)                 per-call secret fill  core/secrets.py
        5 ──► spec.input_model.model_validate(args)      VALIDATION_ERROR (extra="forbid")
                      │
        6 ──► CallRequest ──► runner.run(req)            PooledRunner / SubprocessRunner
                      │            │  spawn (preexec rlimits) or warm --serve worker
                      │            ▼
                      │     core/sandbox/worker_main.py
                      │            │ import adapter module, find ToolSpec
                      │            │ ws = Workspace(root); handler(ws, **args)
                      │            │ BinaryResult ──► base64 | persisted resource ref
                      │            ▼
                      │     {"ok": true, "result": …} | {"ok": false, "error": {code,…}}
                      │
        7 ──► normalize_result(raw, ws)                  >1 MB → workspace resource ref;
                                                           >20 MB → RESULT_TOO_LARGE   core/serialization.py
                      │
        8 ──► JsonlLogger.log_call + Metrics.observe    per-call JSONL + Prometheus
                      │
              success ◄── return {tool_call_id, tool, result}

        ToolError path: log + metrics ──► repairer.maybe_capture(tool, exc, args)  F-030
                      │                     (generated adapters only; validation errors excluded)
                      └─► raise ──► to_mcp_error() ──► client gets isError with the
                          JSON ToolError document; traceback stays server-side (INV-4)
```

The HTTP transport wraps the FastMCP app in `BearerAuthMiddleware` (401 without a valid token when tokens are configured; `/health` + `/metrics` open) and runs it under uvicorn (`serve_http`).

## Agent loops

### Converter pipeline (F-029) — `agents/converter_agent.py`

Stages: `imported → proposed → codegen → format → merged` (each checkpointed to both `checkpoint.json` in the workdir and the `checkpoints` SQL table — a `--resume` run skips completed stages).

```
"convert <pkg>" ──► [introspect: importlib + inspect, installed pkg only]
        ──► [propose ≤10 fixed-signature tools (MAX_TOOLS, E-12);
        │     empty surface → LLM proposer tier (select-and-describe only)]
        ──► [codegen adapters/<mod>.py from adapter.py.jinja ──► static gate
        │     (py_compile + INV-1 lint + contract); fail → reject "codegen-static-gate"]
        ──► [render tool_call_format.jinja ──► validate (<tools> block, name pattern,
        │     additionalProperties:false) ──► AUTO-FIX ≤3 cycles (MAX_FIX_CYCLES):
        │     deterministic strategies first, then LLM repair tier; exhausted → reject]
        ──► [AI-judge] ── blocked ──► repair per findings (deterministic, then LLM tier)
        │     ──► re-submit, ≤3 cycles (MAX_JUDGE_CYCLES) ──► still blocked → reject
        │        "judge-blocked-exhausted" + rejection.json (E-10, INV-6)
        └── pass / pass-with-warnings ──► AUTO-MERGE into the live registry
              (E-7: existing file → lands as .draft.py instead of clobbering)
```

Every step is a Thought→Action→Observation triple via `ReActTracer` (`agents/react.py`): appended to `react_trace.jsonl`, optionally streamed (`--stream`), and each run opens/closes a `conversion_runs` DB row (`merged` | `rejected` + `reason_code`). Terminal rejection is fail-closed: no merge, exit 2, `rejection.json` for the audit ledger. Without a Token Factory key the judge seam is absent and the agent's default judge returns `blocked` — every run ends rejected (E-20).

### Repair loop (F-030) — `agents/repair_agent.py`

```
[ToolError from a generated adapter] ──► incident row (tool, error_class, input_json, version)
        VALIDATION_ERROR / built-in adapters / non-live tools: ignored
        ──► eligibility: ≥2 same-class failures within 600 s (SAME_CLASS_WINDOW_S)
        ──► snapshot current source ──► diagnose + patch (fast Nemotron tier returns
        │     full corrected source; untrusted until gated)
        ──► static gate ──► re-judge (blocked/unavailable = failure) ──► write + refresh_module
        │     ──► smoke replay of the captured input in the sandbox
        │           pass → healed (new version row, audit event, incident 'healed')
        │           fail → ROLLBACK: restore snapshot, version chain row, 'rolled-back'
        └── breaker: ≥3 rolled-back repairs in 24 h (BREAKER_WINDOW_S)
              → quarantine the whole module (all its tools → TOOL_DISABLED,
                needs-human audit event); console re-enable resets the window
```

Repair attempts run on a daemon thread and every entry point is exception-guarded — repair can never break dispatch.

## Key classes

| Class / function | File | Role |
|------------------|------|------|
| `ToolSpec`, `ToolError`, `BinaryResult` | `adapters/base.py` | adapter contract: name, description, input model, handler, timeout, network, backend, limits, secret_params |
| `ToolRegistry`, `CatalogEntry` | `server/registry.py` | auto-discovery of `adapters/*.py`, mtime-keyed catalog cache, lazy spec imports, live/draft/quarantined state persisted in SQLite |
| `Settings` | `server/config.py` | env + YAML + preset merge; derived paths (`db_path`, `audit_jsonl`, `log_jsonl`); `agent_features_enabled` |
| `AppContext` | `server/app.py` | composition root: store, audit, registry, workspace, sizing, runner, logger, metrics, tokens, rate limiter, secrets, repairer; `dispatch()` |
| `SubprocessRunner` / `CallRequest` | `core/sandbox/runner.py` | spawn-per-call execution, scrubbed env, rlimits preexec, timeout SIGKILL |
| `PooledRunner` | `core/sandbox/pool.py` | warm worker pool: (rss, fs) rlimit buckets, one in-flight call per worker, recycling, `PROTOX_POOL=0` off-switch |
| `Workspace` | `core/workspace.py` | path confinement, per-call temp dirs, output paths, `protox-workspace://` resource refs |
| `normalize_result` | `core/serialization.py` | 1 MB inline / 20 MB hard caps, base64+MIME, workspace persistence |
| `SecretsBroker` | `core/secrets.py` | per-call injection of declared secret params; scrub/redact/leak-check helpers |
| `SizingProfile`, `compute_profile`, `probe_host` | `core/sizing.py` | F-038 host probe (cgroup-aware) + tiered compute formula + env overrides |
| `Store` + DAOs (`ToolRepo`, `VersionRepo`, `AuditRepo`, `RunRepo`, `IncidentRepo`, `CheckpointRepo`) | `core/db.py` | SQLite (WAL, FK on, busy_timeout) — the only SQL call sites; `parent_id` version chain is the F-030 rollback link |
| `AuditLedger` | `core/audit.py` | dual-write: `audit_events` table + `audit.jsonl`, kinds validated |
| `JsonlLogger`, `Metrics` | `core/logging.py` | per-call JSONL log; Prometheus counters/histogram rendered at `/metrics` |
| `TokenFactoryClient`, `Completion` | `agents/tokenfactory.py` | OpenAI-compatible chat client, 3 retries w/ backoff, sha256 `prompt_hash` per request |
| `Judge`, `JudgeVerdict` | `agents/judge.py` | static lint first (hard blocks), then model review vs `inv-rubric v1.2`; `JudgeUnavailable` = fail closed |
| `lint_adapter_source` | `agents/lint.py` | INV-1 AST linter: forbidden calls/attrs, code-shaped params, TOOLS contract, `extra="forbid"` |
| `Builder` | `agents/codegen.py` | UF-01 server side: introspect → render → static gate → judge → auto-merge; E-7 draft writes |
| `ConverterAgent`, `ConversionResult` | `agents/converter_agent.py` | F-029 ReAct pipeline + demo seams (`_demo_judge` etc.) |
| `run_convert` | `server/convert_cli.py` | CLI composition root: DB-tracked run, production judge/merger/repairer/proposer seams |
| `RepairAgent` | `agents/repair_agent.py` | F-030 capture/patch/re-judge/replay/rollback/breaker/quarantine |
| `ReActTracer`, `Step` | `agents/react.py` | triples, live streaming, JSONL trace, stage checkpoints |
| `TokenStore`, `BearerAuthMiddleware` | `server/auth.py` | SHA-256-hashed bearer tokens, per-tool ACL contextvar |
| `RateLimiter` | `server/ratelimit.py` | per-token sliding window |

## Data layout under `data_dir`

Default `~/.local/share/protocol-x` (`PROTOX_DATA_DIR`); the Docker profile uses `/data`.

```
<data_dir>/
├── protox.db                  SQLite system of record (WAL; migrations m001/m002 applied in order)
├── audit.jsonl                append-only governance trail (dual of audit_events)
├── logs/tool-calls.jsonl      one JSON line per tool call
├── catalog_cache.json         registry catalog cache (mtime-keyed, lazy-load enabler)
├── conversions/<mod>-<ts>/    one workdir per converter run
│   ├── react_trace.jsonl      full ReAct trace (AC-F029-05)
│   ├── checkpoint.json        stage checkpoint (E-13)
│   ├── proposals.json         tool-surface proposal
│   ├── <mod>.py               generated adapter · tool_call_format.txt
│   └── rejection.json         machine-readable fail-closed record (on rejection)
└── versions/<sha256>.py       repair snapshots (last-known-good adapter sources)
```

The workspace (`PROTOX_WORKSPACE`, default `<data_dir>/workspace`) holds agent-visible files: tool inputs/outputs, an `out/` directory for auto-named results, and `protox-*` per-call temp dirs.
