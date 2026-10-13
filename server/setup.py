"""Guided first-run setup wizard (F-031): `protocol-x-tool-mcp setup`.

Interactive: policy preset -> workspace -> Token Factory key -> bearer token.
Writes `<data_dir>/server.yaml` (auto-loaded by Settings) and prints the
client snippet. Idempotent: a rerun loads the existing file, defaults every
answer to the current values, and keeps the existing bearer token unless the
operator explicitly asks to regenerate it.

Non-interactive (no TTY): prints the manual configuration path instead of
failing — CI and curl-pipe installs stay green (AC-P9-04).
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

import yaml

from server.config import Settings

PRESETS = ("network-egress", "strict", "permissive-dev")


def _ask(prompt: str, default: str) -> str:
    answer = input(f"{prompt} [{default}]: ").strip()
    return answer or default


def _ask_choice(prompt: str, choices: tuple[str, ...], default: str) -> str:
    while True:
        answer = _ask(f"{prompt} ({'/'.join(choices)})", default)
        if answer in choices:
            return answer
        print(f"  please pick one of: {', '.join(choices)}")


def run_setup(env: dict[str, str] | None = None) -> int:
    import os

    env = dict(os.environ if env is None else env)
    data_dir = Path(
        env.get("PROTOX_DATA_DIR") or Path.home() / ".local" / "share" / "protocol-x"
    ).expanduser()
    cfg_path = data_dir / "server.yaml"

    print("Protocol-X Tool MCP — first-run setup\n")
    if not sys.stdin.isatty():
        print("non-interactive shell detected — no wizard. Configure manually:")
        print(f"  1. copy configs/server.yaml.example to {cfg_path} and edit it")
        print("  2. or set PROTOX_* environment variables (see docs/environment-variables.md)")
        print("  3. run `protocol-x-tool-mcp doctor` to verify")
        return 0

    existing: dict = {}
    if cfg_path.exists():
        try:
            existing = yaml.safe_load(cfg_path.read_text()) or {}
        except yaml.YAMLError:
            existing = {}
        print(f"existing configuration found at {cfg_path} — press Enter to keep values\n")

    cur_sec = existing.get("security") or {}
    cur_ws = (existing.get("workspace") or {}).get("root")
    cur_models = existing.get("models") or {}

    preset = _ask_choice(
        "policy preset", PRESETS,
        cur_sec.get("policy_preset") or "network-egress")
    workspace = _ask("workspace root (agent-visible file sandbox)",
                     cur_ws or str(data_dir / "workspace"))
    tf_key = _ask(
        "Nebius Token Factory API key (blank = degraded mode, agent features off)",
        cur_models.get("api_key") or "")
    if tf_key:
        judge = _ask("judge model", cur_models.get("judge") or "nvidia/nemotron-3-ultra")
        fast = _ask("fast model", cur_models.get("fast") or "nvidia/nemotron-3-nano")
    else:
        judge = cur_models.get("judge") or "nvidia/nemotron-3-ultra"
        fast = cur_models.get("fast") or "nvidia/nemotron-3-nano"

    tokens = list(cur_sec.get("tokens") or [])
    if tokens:
        keep = _ask("bearer token exists — regenerate? (y/N)", "N")
        if keep.lower().startswith("y"):
            tokens = [secrets.token_urlsafe(24)]
    else:
        tokens = [secrets.token_urlsafe(24)]

    config = {
        "workspace": {"root": workspace},
        "security": {
            "policy_preset": preset,
            "tokens": tokens,
            "rate_limit_per_min": cur_sec.get("rate_limit_per_min", 60),
        },
        "models": {"api_key": tf_key, "judge": judge, "fast": fast},
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(config, sort_keys=False))

    print(f"\nconfiguration written to {cfg_path}")
    print(f"bearer token (store it — it is shown hashed in logs): {tokens[0]}")
    if not tf_key:
        print("\ndegraded mode (E-20): set PROTOX_TF_API_KEY to enable the "
              "AI-judge, converter, and self-repair agents")
    print("\nnext steps:")
    print("  protocol-x-tool-mcp doctor          # verify the environment")
    print("  protocol-x-tool-mcp serve         # stdio transport (MCP client)")
    print("  protocol-x-tool-mcp serve --transport http  # console + HTTP transport")
    return 0


def configured_settings(env: dict[str, str] | None = None) -> Settings:
    """Helper for tests/scripts: Settings after the wizard has run."""
    return Settings(env=env)
