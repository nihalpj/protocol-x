# Security Model

Threat model, architectural invariants, and the concrete mechanisms that enforce them. Every mechanism cited lives in this repo; nothing here is aspirational.

## Threat model

The server exists to let AI agents run library calls they could otherwise only get by executing arbitrary Python — so it is designed against an adversarial or merely careless agent:

1. **RCE via tool arguments** — an agent smuggling `eval`/`exec`, a free-form code/query string, or a shell-out into a tool call.
2. **Sandbox escape / resource abuse** — a call that spins CPU, balloons memory, fills the disk, or forks, starving the host or other callers.
3. **Path traversal** — reading or writing outside the declared workspace (`../../etc/passwd`, symlinks, absolute paths).
4. **SSRF** — the HTTP tools used to reach private/loopback/link-local targets, including via redirect chains or DNS tricks (e.g. `169.254.169.254`).
5. **Secret exfiltration** — a compromised tool harvesting credentials from the worker environment or logs.
6. **Context/resource blowup** — binary payloads or giant results dumped into the agent's context window.
7. **Malicious generated adapters** — the converter or builder producing a wrapper that violates the invariants; a false-pass from the AI-judge.
8. **Server compromise via error leakage** — tracebacks or internals leaking to clients and feeding further attacks.

## Invariants INV-1 … INV-8

| Inv | Statement | Enforcement in this repo |
|-----|-----------|--------------------------|
| **INV-1** | No arbitrary code — no eval/exec/compile/`__import__`, no free-form code-string parameters, no subprocess/`os.system`; every tool is a fixed wrapper with typed params. | `agents/lint.py` AST linter runs on every generated adapter **before** the judge and hard-blocks: forbidden calls (`eval`, `exec`, `compile`, `__import__`, `input`), forbidden attrs (`os.system`, `os.popen`, `subprocess.run/Popen/call`, `importlib.import_module`), code-shaped parameter names (`code`, `expr`, `query_string`, …), plus contract checks (must expose `TOOLS`, input models must set `extra="forbid"`). `agents/codegen.py` + `templates/adapter.py.jinja` only emit fixed-signature wrappers; the converter skips `*args`/`**kwargs` functions and the LLM proposer can only select/describe real functions. Judge rubric `policies/inv-rubric-v1.2.yaml` scores INV-1 as a blocking severity. |
| **INV-2** | Sandboxed execution — every call runs in a constrained worker; resource budgets are enforced by the OS. | `core/sandbox/limits.py` `apply_limits()` in the child preexec: `RLIMIT_CPU` (soft 25 s / hard +5), `RLIMIT_AS` (512 MB RSS), `RLIMIT_FSIZE` (100 MB), `RLIMIT_NPROC` 0 (best-effort), `RLIMIT_CORE` 0. Wall-clock timeout → SIGKILL → `SANDBOX_TIMEOUT` (`core/sandbox/runner.py`, `core/sandbox/pool.py`). Crash/OOM/non-zero exit → `SANDBOX_CRASH`; the server keeps serving (`tests/integration/test_sandbox.py` covers the timeout bomb, memory bomb, and server-survives-crash cases). The warm pool keys workers by (rss, fs) buckets so rlimits stay exactly as strict per bucket — with the documented CPU trade-off below. |
| **INV-3** | Schema honesty — declared schemas are the whole contract; unknown fields are rejected, limits are stated. | Pydantic v2 input models with `extra="forbid"` (JSON Schema `additionalProperties: false`); `server/registry.py` also forces `additionalProperties: false` into catalog schemas; `agents/lint.py` AST-checks every input model for `extra="forbid"`. Results are JSON-serializable or `BinaryResult` (base64+explicit MIME); clamps report `truncated: true` with applied-vs-actual values (`adapters/_guards.py`); unknown tools → `UNKNOWN_TOOL`. |
| **INV-4** | Structured errors — failures are typed codes, never tracebacks. | `adapters/base.py` defines `ToolError{code,message,details}` and the canonical code vocabulary (`VALIDATION_ERROR` … `RATE_LIMITED`); `server/errors.py` converts every exception to an MCP error carrying the JSON document; `core/sandbox/worker_main.py` maps unexpected worker exceptions to `ADAPTER_ERROR` — the traceback dies with the worker; full tracebacks exist only in server logs, keyed by `tool_call_id`. |
| **INV-5** | Statelessness — a call cannot depend on, or leave, server-side state; all file I/O is workspace-confined. | Per-call worker processes; `core/workspace.py` resolves every path under the workspace root (rejects `..` escapes, foreign absolutes, NUL bytes; symlink targets resolved before the containment check); per-call temp dirs (`protox-*`) are removed after the call; the judge rubric blocks module-level mutable state and writes outside the workspace; the SQLite store holds only server-side operational state — nothing per-tool. |
| **INV-6** | Fail-closed judge — a blocked, unreachable, or unparseable verdict never registers. | `agents/judge.py`: static-lint hard blocks return `blocked` without a model call; `InferenceError`/JSON parse failure/unknown verdict → `JudgeUnavailable` → the adapter stays out of the registry (E-5). `agents/converter_agent.py`'s default judge returns `blocked` (no key → every conversion ends rejected, E-20); `agents/repair_agent.py` treats judge-unavailable as repair failure → rollback; `agents/codegen.py` returns `judge-unavailable` state and never merges. A quarantined tool's cached spec is evicted so it cannot stay dispatchable (`server/registry.py set_state`). |
| **INV-7** | Secrets never in worker env — credentials are injected per call at the dispatcher; workers are keyless. | `core/secrets.py` `SecretsBroker.inject()` fills only declared `secret_params` of the tool's own spec, dispatcher-side; workers spawn with a scrubbed environment (`_SCRUBBED_ENV` in `core/sandbox/runner.py` — PATH/PYTHONPATH/thread caps only); per-call `extra_env` (e.g. `ALLOWED_HOSTS`) is dispatcher-controlled, applied for the call and reverted in the worker (`worker_main._execute`); `scrub_text` redacts secret values from text; `env_leak_check` supports the CI assertion; the DB has **no secret columns** (`migrations/m001_initial.sql`, plus a CI helper `no_secret_columns_guard`). `doctor` verifies the worker env carries no configured secrets. |
| **INV-8** | Egress control — the only sanctioned outbound dependency is the Token Factory endpoint; HTTP tools cannot reach internal targets. | `adapters/_urlguard.py`: scheme allowlist (http/https only), optional `ALLOWED_HOSTS` operator allowlist, DNS resolution with private/loopback/link-local/reserved/multicast/unspecified blocking (incl. cloud metadata), connect-by-IP with Host header pinning and SNI hostname verification for TLS, manual redirect following (max 5) with **per-hop revalidation before any body bytes are read**, stdlib `http.client` only (no requests/httpx in the worker path), inline body cap 256 KB, download abort at 200 MB with partial-file deletion. Metrics/audit/logs are self-hosted — no phone-home anywhere (`tests/security/` covers the SSRF battery). |

## Quarantine and circuit-breaker semantics

From `agents/repair_agent.py`:

- **Eligibility** — only `ToolError`s from *generated* (or manually edited) adapters count; `VALIDATION_ERROR` (caller error) is excluded; built-in launch-core adapters never self-modify. Trigger: ≥2 same-class failures within 600 s.
- **Rollback** — any failed gate (static gate, judge blocked/unavailable, smoke-replay failure) restores the pre-patch snapshot and appends a version row keeping the `parent_id` chain honest; the incident is `rolled-back`.
- **Circuit breaker** — ≥3 rolled-back repairs for the same tool within 24 h quarantine the whole adapter module: every tool in it becomes `quarantined`, calls return `TOOL_DISABLED` (registry raises before dispatch), and a needs-human audit event is filed. A console re-enable (`/api/repair/reenable`) sets the tool live and raises the breaker's window floor to that moment.

## Audit ledger

`core/audit.py`: append-only, dual-written to the SQLite `audit_events` table (system of record, queryable by kind/time) and `audit.jsonl` (human-readable trail). Kinds: `verdict`, `merge`, `repair`, `quarantine`, `config`, `install`, `rejection`. Every judge verdict records model, prompt hash, rubric version, and timestamp (AC-F019-05); every auto-merge, repair incident, rollback, and quarantine lands here.

## Docker hardening (F-021)

`deploy/docker/Dockerfile` + `docker-compose.yml`: non-root runtime user (`protox`, **uid 10001**), **read-only root FS** with a writable `/data` volume only, tmpfs `/tmp` (`noexec,nosuid,size=256m`), `cap_drop: [ALL]`, `no-new-privileges:true`, `/health` HEALTHCHECK, and a `protox-offline` compose variant with `network_mode: none` pinning the `strict` preset. Compute limits (`cpus`, `mem_limit`) are host-derived at creation via `deploy/docker/compute-env.sh` (F-038). The optional OpenShell outer-cage profile (F-034) is documented in `policies/openshell/` (Landlock/seccomp/OPA policy sketches) — strictly additive; the baseline is rlimits + this Docker profile.

## Known limitations (documented in code)

- **CPU rlimit replaced by wall-clock kill in pooled mode.** `apply_pool_limits()` (core/sandbox/limits.py) deliberately omits `RLIMIT_CPU`: it budgets *cumulative* process CPU, which a long-lived warm worker would exhaust across calls. CPU abuse in pooled mode is bounded by the parent's wall-clock SIGKILL instead. Strict per-call `RLIMIT_CPU` is restored with `PROTOX_POOL=0` (spawn-per-call).
- **Allowlisted hosts bypass the private-IP block.** In `_urlguard.py`, a host on the operator-set `ALLOWED_HOSTS` list is trusted and skips the private/loopback IP check (stage 3) so intranet/dev hosts (e.g. localhost test servers) are reachable deliberately. The operator allowlist is therefore a security control: only list hosts you trust to resolve to internal addresses. The dispatcher injects it per call; agents cannot set it.
- **`RLIMIT_NPROC=0` is best-effort** — it fails for root and on some platforms (suppressed by design); the wall-clock kill remains as the backstop.
- **Rate limiting is single-instance in-process** (`server/ratelimit.py`) — per-token sliding window in memory; stdio dispatch (no token) is exempt by design.
- **Degraded mode rejects all conversions.** Without `PROTOX_TF_API_KEY` the judge fails closed by design (E-20): core tools are unaffected, but every `convert` run ends rejected after its bounded cycles. This is intentional, not a bug.
