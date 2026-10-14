"""Server configuration (F-032 seam): env + optional YAML + policy preset merge.

Precedence: CLI flags > env vars (PROTOX_*) > config file > policy preset > defaults.
Invalid preset files or unknown preset keys fail startup loudly (AC-F032-02).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "protocol-x"
_PRESET_KEYS = {"network", "egress_allowlist", "limits", "per_tool_overrides"}
_LIMIT_KEYS = {"cpu_s", "rss_mb", "fs_mb"}
_POLICIES_DIR = Path(__file__).resolve().parent.parent / "policies"


class ConfigError(Exception):
    pass


_BOOL_TRUTHY = {"1", "true", "yes", "on"}
_BOOL_FALSY = {"0", "false", "no", "off"}


def _parse_bool_flag(value: Any, *, source: str) -> bool:
    """Boolean env/YAML flag: true/false/1/0/yes/no/on/off; anything else fails loudly."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _BOOL_TRUTHY:
            return True
        if lowered in _BOOL_FALSY:
            return False
    raise ConfigError(
        f"invalid boolean for {source}: {value!r} "
        "(expected true, false, 1, 0, yes, no, on or off)"
    )


class Settings:
    def __init__(self, env: dict[str, str] | None = None, config_file: str | None = None):
        env = dict(os.environ if env is None else env)
        # Default config location: <data_dir>/server.yaml (written by the setup
        # wizard, F-031). An explicit --config always wins; PROTOX_CONFIG
        # overrides the auto-discovered path.
        if config_file is None:
            candidate = env.get("PROTOX_CONFIG")
            if candidate:
                config_file = candidate
            else:
                prelim = Path(
                    env.get("PROTOX_DATA_DIR") or _DEFAULT_DATA_DIR
                ).expanduser()
                if (prelim / "server.yaml").exists():
                    config_file = str(prelim / "server.yaml")
        file_cfg: dict[str, Any] = {}
        if config_file:
            try:
                file_cfg = yaml.safe_load(Path(config_file).read_text()) or {}
            except yaml.YAMLError as exc:
                raise ConfigError(f"invalid YAML in config file {config_file}: {exc}") from exc
            if not isinstance(file_cfg, dict):
                raise ConfigError(f"config file {config_file} must contain a mapping")
        self.config_file = config_file

        self.data_dir = Path(
            env.get("PROTOX_DATA_DIR") or file_cfg.get("data_dir") or _DEFAULT_DATA_DIR
        ).expanduser()
        ws_default = self.data_dir / "workspace"
        self.workspace_root = Path(
            env.get("PROTOX_WORKSPACE")
            or (file_cfg.get("workspace") or {}).get("root")
            or ws_default
        ).expanduser()
        srv = file_cfg.get("server") or {}
        self.host = env.get("PROTOX_HOST") or srv.get("host", "127.0.0.1")
        self.port = int(env.get("PROTOX_PORT") or srv.get("port", 7894))
        self.transport = srv.get("transport", "stdio")

        sec = file_cfg.get("security") or {}
        tokens_env = env.get("PROTOX_TOKENS", "")
        self.tokens: list[str] = (
            [t.strip() for t in tokens_env.split(",") if t.strip()]
            if tokens_env
            else list(sec.get("tokens") or [])
        )
        acl_env = env.get("PROTOX_TOOL_ACL", "")
        try:
            self.tool_acl: dict[str, list[str]] = (
                json.loads(acl_env) if acl_env else dict(sec.get("tool_acl") or {})
            )
        except json.JSONDecodeError as exc:
            raise ConfigError(f"PROTOX_TOOL_ACL is not valid JSON: {exc}") from exc
        self.rate_limit_per_min = int(
            env.get("PROTOX_RATE_LIMIT") or sec.get("rate_limit_per_min", 60)
        )
        hosts_env = env.get("PROTOX_ALLOWED_HOSTS", "")
        self.allowed_hosts: list[str] = (
            [h.strip() for h in hosts_env.split(",") if h.strip()]
            if hosts_env
            else list(sec.get("allowed_hosts") or [])
        )
        secrets_env = env.get("PROTOX_SECRETS", "")
        try:
            self.secrets: dict[str, str] = json.loads(secrets_env) if secrets_env else {}
        except json.JSONDecodeError as exc:
            raise ConfigError(f"PROTOX_SECRETS is not valid JSON: {exc}") from exc

        # --- sandbox limits: preset defaults, env/file overrides ---
        sbx = file_cfg.get("sandbox") or {}
        limits_file = dict(sbx.get("limits") or {})
        self.preset_name = (
            env.get("PROTOX_POLICY_PRESET") or sec.get("policy_preset") or "network-egress"
        )
        preset = load_preset(self.preset_name)
        preset_limits = dict(preset.get("limits") or {})
        merged_limits = {**{"cpu_s": 25, "rss_mb": 512, "fs_mb": 100}, **preset_limits, **limits_file}
        self.limit_cpu_s = int(merged_limits["cpu_s"])
        self.limit_rss_mb = int(merged_limits["rss_mb"])
        self.limit_fs_mb = int(merged_limits["fs_mb"])
        self.preset_network: str = preset.get("network", "egress")
        self.preset_egress_allowlist: list[str] = list(preset.get("egress_allowlist") or [])
        self.preset_tool_overrides: dict[str, Any] = dict(preset.get("per_tool_overrides") or {})
        self.default_timeout_s = int(sbx.get("default_timeout_s", 30))

        models = file_cfg.get("models") or {}
        self.tf_api_key = env.get("PROTOX_TF_API_KEY") or models.get("api_key", "")
        self.tf_endpoint = (
            env.get("PROTOX_TF_ENDPOINT")
            or models.get("endpoint")
            or "https://api.tokenfactory.nebius.com/v1"
        )
        self.tf_judge_model = (
            env.get("PROTOX_TF_JUDGE_MODEL")
            or models.get("judge")
            or "nvidia/Nemotron-3-Ultra-550b-a55b"
        )
        self.tf_fast_model = (
            env.get("PROTOX_TF_FAST_MODEL")
            or models.get("fast")
            or "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
        )
        self.tf_test_model = (
            env.get("PROTOX_TF_TEST_MODEL")
            or models.get("test")
            or "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
        )
        self.tf_escalation_model = (
            env.get("PROTOX_TF_ESCALATION_MODEL")
            or models.get("escalation")
            or "nvidia/Nemotron-3-Ultra-550b-a55b"
        )
        # Reasoning models on large prompts can take 60-190s — the HTTP read
        # timeout must cover that or the judge flaps judge-unavailable (E-5).
        self.tf_timeout_s = float(env.get("PROTOX_TF_TIMEOUT_S") or models.get("timeout_s") or 300)

        # Adapter tool-testing stage (converter): test model executes every
        # generated tool with sample args, bounded fix loop, unfixable tools
        # removed before the judge. testing.enabled=false skips the stage.
        testing = file_cfg.get("testing") or {}
        testing_enabled_env = env.get("PROTOX_TESTING_ENABLED")
        self.testing_enabled = (
            testing_enabled_env not in ("0", "false")
            if testing_enabled_env is not None
            else bool(testing.get("enabled", True))
        )
        self.testing_max_cycles = int(
            env.get("PROTOX_TESTING_MAX_CYCLES") or testing.get("max_cycles", 3)
        )

        # F-038 env overrides (computed profile lives in core/sizing.py)
        self.cpus_override = env.get("PROTOX_CPUS")
        self.memory_override = env.get("PROTOX_MEMORY")
        self.worker_pool_override = env.get("PROTOX_WORKER_POOL")
        # P10 warm worker pool; PROTOX_POOL=0 -> strict spawn-per-call
        self.pool_enabled = env.get("PROTOX_POOL", "1") != "0"

        # Human-readable app logging: PROTOX_LOG_LEVEL / logging.level / --log-level
        log_cfg = file_cfg.get("logging") or {}
        self.log_level = (
            env.get("PROTOX_LOG_LEVEL") or log_cfg.get("level") or "INFO"
        ).upper()
        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ConfigError(
                f"invalid log level {self.log_level!r} "
                "(expected DEBUG, INFO, WARNING, ERROR or CRITICAL)"
            )
        # F-016 payload capture: PROTOX_LOG_PAYLOADS / logging.log_payloads / off
        payloads_env = env.get("PROTOX_LOG_PAYLOADS") or None
        self.log_payloads = _parse_bool_flag(
            payloads_env if payloads_env is not None else log_cfg.get("log_payloads", False),
            source="PROTOX_LOG_PAYLOADS" if payloads_env is not None else "logging.log_payloads",
        )

        # F-040 semantic tool search: embedding backend selection
        search_cfg = file_cfg.get("search") or {}
        self.search_backend = (
            env.get("PROTOX_SEARCH_BACKEND") or search_cfg.get("backend") or "auto"
        ).lower()
        if self.search_backend not in ("auto", "tokenfactory", "local", "off"):
            raise ConfigError(
                f"invalid search.backend {self.search_backend!r} "
                "(expected auto, tokenfactory, local or off)"
            )
        self.tf_embed_model = (
            env.get("PROTOX_TF_EMBED_MODEL")
            or search_cfg.get("tf_embedding_model")
            or "Qwen/Qwen3-Embedding-8B"
        )
        self.local_embed_model = (
            env.get("PROTOX_LOCAL_EMBED_MODEL")
            or search_cfg.get("local_model")
            or "BAAI/bge-small-en-v1.5"
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "protox.db"

    @property
    def audit_jsonl(self) -> Path:
        return self.data_dir / "audit.jsonl"

    @property
    def log_jsonl(self) -> Path:
        return self.data_dir / "logs" / "tool-calls.jsonl"

    @property
    def app_log_path(self) -> Path:
        return self.data_dir / "logs" / "server.log"

    @property
    def agent_features_enabled(self) -> bool:
        """Degraded mode (E-20): no Token Factory key -> agent features fail closed."""
        return bool(self.tf_api_key)


def load_preset(name: str, policies_dir: Path | None = None) -> dict[str, Any]:
    """Load a policy preset; invalid YAML or unknown keys fail loudly (AC-F032-02)."""
    policies_dir = policies_dir or _POLICIES_DIR
    path = policies_dir / f"{name}.yaml"
    if not path.exists():
        raise ConfigError(f"unknown policy preset '{name}' (no {path})")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in preset file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"preset file {path} must contain a mapping")
    for key in data:
        if key not in _PRESET_KEYS:
            raise ConfigError(f"unknown key '{key}' in preset file {path}")
    network = data.get("network", "egress")
    if network not in ("off", "egress", "on"):
        raise ConfigError(f"preset {path}: network must be off|egress|on, got {network!r}")
    for key in (data.get("limits") or {}):
        if key not in _LIMIT_KEYS:
            raise ConfigError(f"unknown limits key '{key}' in preset file {path}")
    return data
