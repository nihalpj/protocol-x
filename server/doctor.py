"""doctor: environment diagnostics (F-035; quick mode in P1, full mode in P9).

Checks report green/yellow/red with remediation; exit non-zero on any red.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from server.config import Settings


@dataclass
class Check:
    name: str
    status: str  # green | yellow | red
    detail: str
    remediation: str = ""


def _check_python() -> Check:
    v = sys.version_info
    if v >= (3, 12):
        return Check("python>=3.12", "green", f"{v.major}.{v.minor}.{v.micro}")
    return Check(
        "python>=3.12",
        "red",
        f"found {v.major}.{v.minor}.{v.micro}",
        "install Python 3.12+ (e.g. via `uv python install 3.12`) or use the Docker profile",
    )


def _check_rlimits() -> Check:
    try:
        import resource

        resource.getrlimit(resource.RLIMIT_AS)
        return Check("posix-rlimits", "green", "resource limits supported")
    except (ImportError, AttributeError):
        return Check(
            "posix-rlimits",
            "red",
            "rlimits unsupported on this OS",
            "use the Docker profile (deploy/docker) — the only supported path on non-POSIX hosts",
        )


def _check_port(settings: Settings) -> Check:
    import socket

    with socket.socket() as sock:
        try:
            sock.bind((settings.host, settings.port))
        except OSError:
            return Check(
                "port-free",
                "red",
                f"{settings.host}:{settings.port} is occupied",
                "set PROTOX_PORT to a free port; the setup wizard relocates snippets automatically",
            )
    return Check("port-free", "green", f"{settings.host}:{settings.port} available")


def _check_workspace(settings: Settings) -> Check:
    try:
        settings.workspace_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=settings.workspace_root, delete=True):
            pass
        return Check("workspace-writable", "green", str(settings.workspace_root))
    except OSError as exc:
        return Check(
            "workspace-writable",
            "red",
            f"{settings.workspace_root}: {exc}",
            "set PROTOX_WORKSPACE to a writable directory",
        )


def _check_uv() -> Check:
    if shutil.which("uv"):
        return Check("uv-present", "green", "uv on PATH")
    return Check("uv-present", "yellow", "uv not on PATH", "optional; used by install.sh")


def run_checks(settings: Settings, quick: bool) -> list[Check]:
    checks = [
        _check_python(),
        _check_rlimits(),
        _check_port(settings),
        _check_workspace(settings),
        _check_uv(),
    ]
    if not quick:
        checks.extend(_full_checks(settings))
    return checks


def _full_checks(settings: Settings) -> list[Check]:
    """Full mode (P9): spawn test, workspace I/O, transport self-check, TF reachability."""
    checks: list[Check] = []
    try:
        from core.sandbox.runner import CallRequest, SubprocessRunner

        runner = SubprocessRunner()
        settings.workspace_root.mkdir(parents=True, exist_ok=True)
        # smoke: run image_info is adapter-dependent; a spawn-only probe uses the python backend env
        env = runner.worker_env()
        leaked = [k for k in settings.secrets if k in env]
        if leaked:
            checks.append(Check("worker-env-scrubbed", "red", f"secrets leak into worker env: {leaked}"))
        else:
            checks.append(Check("worker-env-scrubbed", "green", "worker env carries no configured secrets"))
        _ = CallRequest  # full spawn probe lands in P9 with a hello adapter
    except Exception as exc:
        checks.append(Check("worker-env-scrubbed", "red", str(exc)))
    if settings.tf_api_key:
        checks.append(Check("token-factory", "yellow", "key configured; live reachability probe lands in P9"))
    else:
        checks.append(
            Check(
                "token-factory",
                "yellow",
                "no PROTOX_TF_API_KEY — degraded mode: 38 core tools live, agent features disabled (E-20)",
                "add a Token Factory key to enable the judge / converter / repair agents",
            )
        )
    return checks


def run_doctor(quick: bool = False, settings: Settings | None = None) -> int:
    settings = settings or Settings()
    checks = run_checks(settings, quick)
    red = 0
    for c in checks:
        icon = {"green": "PASS", "yellow": "WARN", "red": "FAIL"}[c.status]
        print(f"[{icon}] {c.name}: {c.detail}")
        if c.remediation and c.status != "green":
            print(f"       fix: {c.remediation}")
        if c.status == "red":
            red += 1
    return 1 if red else 0


if __name__ == "__main__":
    raise SystemExit(run_doctor(quick="--quick" in sys.argv))
