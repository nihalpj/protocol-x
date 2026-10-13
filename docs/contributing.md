# Contributing

Development setup, the adapter contract, and the gates a change must pass. Python **3.12+** is required (`requires-python = ">=3.12"` in `pyproject.toml`; the PRD targets 3.12 — the codebase also runs on 3.13).

## Dev setup

```bash
git clone <repo> && cd protocol-x-tool-mcp
uv sync                      # creates .venv and installs all deps (dev group included)
```

Run the checks the CI-equivalent way (all configured in `pyproject.toml`):

```bash
uv run pytest                              # full suite: unit + integration + security + adversarial
uv run pytest -m "not slow"                # skip the soak/benchmark markers
uv run pytest -m "not live"                # tests needing real Token Factory credentials are marked 'live'
uv run ruff check .                        # line-length 110; rules E,F,I,UP,B,SIM
uv run mypy                                # warn_unused_ignores, warn_return_any
uv run lint-imports                        # import-linter layering contract
```

The import-linter contract (`[tool.importlinter]` in `pyproject.toml`) is the architectural rule: layers `server → agents → core → adapters`. If your change needs a cross-layer import, restructure instead of suppressing — e.g. shared helpers go in `core/` (`core/hashing.py` exists precisely so `agents/` never imports `server/`).

Tests live under `tests/`: `unit/` (sizing, db, workspace, serialization), `integration/` (per-adapter tool tests, transports, pool, converter, builder, repair, onboarding, console API), `security/` (presets+secrets, SSRF), `adversarial/` (judge corpus: eval-injection, freeform query, open schema, shell-out), `benchmarks/` (warm pool vs spawn; `PROTOX_WRITE_BASELINE=1` refreshes `baseline.json`). Fixtures (`tests/conftest.py`) point `PROTOX_DATA_DIR`/`PROTOX_WORKSPACE` at a tmp dir, so the suite is safe to run anywhere.

## Writing an adapter

Adapters are auto-discovered from `adapters/*.py` (`server/registry.py`): any module exposing a module-level `TOOLS` list of `ToolSpec` (or a `get_tools()` function) registers. Files named `base.py`, `__init__.py`, starting with `_`, or ending `.draft.py` are skipped. Discovery is lazy — the module is imported on first use, and a catalog cache keeps boot flat.

The contract (from `adapters/base.py`):

```python
class MyToolIn(BaseModel):
    model_config = ConfigDict(extra="forbid")   # INV-3 — required, AST-enforced
    path: str = Field(description="workspace-relative file path")
    width: int = Field(gt=0, le=8192, description="px")

def my_tool(ws, path: str, width: int) -> dict:
    resolved = ws.resolve(path, must_exist=True)   # INV-5 — all I/O via ws
    try:
        ...
    except ToolError:
        raise
    except Exception as exc:                        # INV-4 — structured errors only
        raise ToolError(ADAPTER_ERROR, f"... failed: {type(exc).__name__}",
                        {"exception": type(exc).__name__}) from exc
    return {"output": ws.resource_ref(out, "image/png")}

TOOLS = [ToolSpec(name="my_tool", description="... (limits: ...)", input_model=MyToolIn,
                  handler=my_tool, timeout_s=30, network=False, backend="python",
                  limits={})]
```

Rules that get a file rejected (by `agents/lint.py`, the same gate the converter faces):

- Every input model must set `extra="forbid"` (`additionalProperties: false`).
- No `eval`/`exec`/`compile`/`__import__`/`input`, no `os.system`/`os.popen`/`subprocess.*`/`importlib.import_module` — anywhere in the file.
- No code-shaped parameter names (`code`, `expression`, `expr`, `query_string`, `script`, `program`, `source_code`).
- Handlers take `ws` plus declared args only; `*args`/`**kwargs` wrappers are not tool-shaped.

Conventions the built-ins follow (please match): heavy imports inside the handler, not at module top; limits declared in both the description and `ToolSpec.limits`, enforced boundary-inclusive; magic-byte and size pre-checks via `adapters/_guards.py` (`check_magic`, `check_input_size`) so corrupt/oversize inputs fail **before** parsing; binaries returned via `BinaryResult` or `ws.resource_ref`; `network=True` only when the tool genuinely fetches (the judge emits a warning otherwise, E-9).

References: `templates/adapter.py.jinja` is the canonical codegen template (what the builder emits), and `adapters/images_pillow.py` is the exemplar hand-written adapter — 8 tools, one input model per tool, shared `_load`/`_save` helpers, every path through `ws`.

## PR gates

A change is mergeable when, in order:

1. `uv run pytest` is green (including `tests/security/` and `tests/adversarial/` — the adversarial corpus must keep scoring `blocked`; a false pass fails the build by design).
2. `uv run ruff check .` clean.
3. `uv run mypy` clean.
4. `uv run lint-imports` clean.
5. If you touched dispatch, serialization, or the pool: the benchmark (`uv run pytest tests/benchmarks/`) still shows the warm pool beating spawn-per-call. The `slow` marker (skippable with `-m "not slow"`) covers the soak test and one large-CSV pandas test, not the benchmarks.

Add tests with the change — the repo's convention is to state the test count in the commit message (e.g. "8 tests green").

## Commit conventions

From `git log`:

- Imperative subject, optionally prefixed with the implementation-plan phase: `P7: productionize ReAct converter agent — real judge/merger seams, DB runs + durable checkpoints, CLI convert, 8 tests`.
- Sweep/hygiene commits name themselves plainly: `Lint sweep: repo-wide ruff clean (26 fixes — ...)`.
- One coherent change per commit; details after an em-dash.

## Running the use-case scripts and operator scripts

Operator scripts (`scripts/`, bash, no sudo):

```bash
./scripts/install.sh        # bootstrap uv if missing + uv sync
./scripts/doctor.sh [--quick]   # full or pre-flight diagnostics (exit 1 on red)
./scripts/start.sh          # HTTP server in the background (pid file under data_dir); prints bearer token + console URL
./scripts/start.sh stdio    # stdio transport in the foreground (for MCP clients)
./scripts/status.sh         # pid + /health probe
./scripts/stop.sh           # stop the background server
```

Per-tool verification lives in `tests/integration/` — one file per launch-core package where coverage matters (`test_pdf_pypdf.py`, `test_http_requests.py`, `test_data_pandas.py`, `test_excel_openpyxl.py`, `test_docs_docx.py`) plus sandbox, stdio/HTTP transport, pool, converter, builder, repair, onboarding, console-API, and soak suites; image tools are exercised through the shared `sample_png` fixture and the pool benchmark. Run e.g. `uv run pytest tests/integration/test_pdf_pypdf.py -v`. The end-to-end persona flows (install → setup → doctor → first call → convert) are documented per use case in `docs/use-cases/`, with `samples/sample.png` bundled for the guided first `image_info` call.
