# Environment Variables — `PROTOX_*`

Every environment variable read by the server, verified against `server/config.py` (`Settings`). Precedence: **CLI flags > env vars > config file (`server.yaml`) > policy preset (`policies/*.yaml`) > defaults**.

Config-file auto-discovery: with no `--config` flag and no `PROTOX_CONFIG`, `Settings` loads `<data_dir>/server.yaml` if it exists (written by `protocol-x-tool-mcp setup`).

| Variable | Default | Description |
|----------|---------|-------------|
| `PROTOX_CONFIG` | — | Path to a `server.yaml` config file; overrides the auto-discovered `<data_dir>/server.yaml`. |
| `PROTOX_DATA_DIR` | `~/.local/share/protocol-x` | Root for all server state: `protox.db` (SQLite), `audit.jsonl`, `logs/tool-calls.jsonl`, `catalog_cache.json`, `conversions/`, `versions/`. |
| `PROTOX_WORKSPACE` | `<data_dir>/workspace` | The single confined file root for all tool I/O (INV-5); `..` escapes and foreign absolute paths are rejected. |
| `PROTOX_HOST` | `127.0.0.1` | HTTP transport bind address (`--host` wins). |
| `PROTOX_PORT` | `7894` | HTTP transport port (`--port` wins). |
| `PROTOX_TOKENS` | — (tokenless) | Comma-separated bearer tokens for the HTTP transport. Tokens are stored/compared as SHA-256 hashes. Empty = auth disabled (localhost-trusted, PRD §7.1). Overrides `security.tokens` in config. |
| `PROTOX_TOOL_ACL` | — | JSON object mapping a bearer token to its allowlist of tool names, e.g. `{"tok1": ["image_info"]}`; calls outside the allowlist return `TOOL_NOT_PERMITTED`. Malformed JSON is a startup `ConfigError` (exit 2). Overrides `security.tool_acl`. |
| `PROTOX_RATE_LIMIT` | `60` | Per-token sliding-window budget in calls/minute; excess calls return `RATE_LIMITED`. Applies at dispatch time; stdio (tokenless) dispatch is exempt. |
| `PROTOX_ALLOWED_HOSTS` | — (all public hosts) | Comma-separated egress allowlist for the HTTP adapter. When set, the dispatcher injects it into the worker env as `ALLOWED_HOSTS` per call, and only listed hosts may be fetched (allowlisted hosts bypass the private-IP block — see `docs/security.md`). Overrides `security.allowed_hosts`. |
| `PROTOX_SECRETS` | — | JSON object of named server-side secrets for the credential-injection boundary (F-033), e.g. `{"API_KEY": "…"}`. Values are injected per call into declared `secret_params` only — never ambient env, never logged. Malformed JSON is a startup `ConfigError` (exit 2). |
| `PROTOX_POLICY_PRESET` | `network-egress` | Named sandbox preset from `policies/*.yaml`: `strict` (network off, tightest limits, HTTP adapter disabled) · `network-egress` (HTTP adapter on, standard limits) · `permissive-dev` (relaxed limits, local development only). Unknown names or invalid YAML/keys fail startup loudly (exit 2). |
| `PROTOX_TF_API_KEY` | — (empty) | Nebius Token Factory API key for all agent inference (AI-judge, converter, repair). Empty = degraded mode (E-20): core tools fully live; agent features fail closed. |
| `PROTOX_TF_ENDPOINT` | `https://api.tokenfactory.nebius.com/v1` | OpenAI-compatible chat-completions endpoint for the Token Factory client. |
| `PROTOX_TF_JUDGE_MODEL` | `nvidia/Nemotron-3-Ultra-550b-a55b` | Model for AI-judge reviews (and codegen/repair escalation tier). |
| `PROTOX_TF_FAST_MODEL` | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | Model for converter/repair fast loops (proposal fallback, repair directives, patch generation). |
| `PROTOX_TF_ESCALATION_MODEL` | `nvidia/Nemotron-3-Ultra-550b-a55b` | Escalation model, parsed into `Settings` and configurable here/config (`models.escalation`). Currently reserved — no call site references it yet. |
| `PROTOX_CPUS` | computed (F-038) | Override for the computed container CPU budget (`max(1, cores − 1)`, reserving 2 when cores > 8). Reported by `doctor` and `/api/status` as an override. |
| `PROTOX_MEMORY` | computed (F-038) | Override for the computed memory budget (60% of RAM). Accepts `4096`, `4096m`, or `4g`. |
| `PROTOX_WORKER_POOL` | computed (F-038) | Override for the warm worker-pool size (`min(cores, RAM ÷ (2 × per-call RSS))`, floor 2). |
| `PROTOX_POOL` | `1` | `0` disables the warm worker pool entirely (strict spawn-per-call, per-call `RLIMIT_CPU`). Any other value keeps pooling enabled. |
| `PROTOX_WRITE_BASELINE` | — | Test-only (`tests/benchmarks/test_pool_bench.py`): when set, the pool-vs-spawn benchmark refreshes `baseline.json` with current host numbers. |

## Sandbox limits

Per-call limits (`cpu_s`, `rss_mb`, `fs_mb`) are merged as: built-in defaults (25 s CPU, 512 MB RSS, 100 MB file size) ← preset `limits` ← config `sandbox.limits`. Per-tool `limits` on a `ToolSpec` can override `rss_mb` at dispatch. The `strict` preset ships 15/256/50, `network-egress` 25/512/100, `permissive-dev` 60/1024/500.

## Worker-visible variables

Workers spawn with a scrubbed environment (`core/sandbox/runner.py`): `PATH`, `PYTHONPATH` (project root), `PYTHONUNBUFFERED`, `PYTHONDONTWRITEBYTECODE`, `OPENBLAS_NUM_THREADS=1`, `OMP_NUM_THREADS=1`, `HOME`, `LANG`. The dispatcher may add `ALLOWED_HOSTS` per call for network-enabled tools; nothing else from the server environment leaks in (INV-7).
