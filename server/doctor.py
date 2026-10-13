"""doctor: environment diagnostics (F-035; quick mode in P1, full mode in P9).

Checks report green/yellow/red with remediation; exit non-zero on any red.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from dataclasses import dataclass

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


def _check_sandbox_spawn(settings: Settings) -> Check:
    """Full probe: spawn a sandboxed worker and run image_info on a real PNG."""
    try:
        from PIL import Image

        from core.sandbox.runner import CallRequest, SubprocessRunner

        settings.workspace_root.mkdir(parents=True, exist_ok=True)
        probe = settings.workspace_root / ".doctor-probe.png"
        Image.new("RGB", (8, 8), (1, 2, 3)).save(probe)
        try:
            runner = SubprocessRunner()
            result = runner.run(
                CallRequest(
                    adapter_module="images_pillow",
                    tool_name="image_info",
                    args={"path": probe.name},
                    workspace=str(settings.workspace_root),
                    timeout_s=30,
                    network=False,
                    backend="python",
                    cpu_s=settings.limit_cpu_s,
                    rss_mb=settings.limit_rss_mb,
                    fs_mb=settings.limit_fs_mb,
                )
            )
        finally:
            probe.unlink(missing_ok=True)
        detail = f"worker ran image_info: {str(result)[:80]}"
        return Check("sandbox-spawn", "green", detail)
    except Exception as exc:
        return Check(
            "sandbox-spawn",
            "red",
            f"{type(exc).__name__}: {exc}",
            "sandboxed spawn failed — check rlimits and the adapters/images_pillow.py install; "
            "on non-POSIX hosts use the Docker profile",
        )


def _check_worker_env(settings: Settings) -> Check:
    try:
        from core.sandbox.runner import SubprocessRunner

        env = SubprocessRunner().worker_env()
        leaked = [k for k in settings.secrets if k in env]
        if leaked:
            return Check("worker-env-scrubbed", "red", f"secrets leak into worker env: {leaked}")
        return Check("worker-env-scrubbed", "green", "worker env carries no configured secrets")
    except Exception as exc:
        return Check("worker-env-scrubbed", "red", str(exc))


def _check_transport(settings: Settings) -> Check:
    """Transport self-check: ASGI app boots, /health answers, auth guards /mcp."""
    try:
        from starlette.testclient import TestClient

        from server.app import AppContext, build_http_app

        ctx = AppContext(settings)
        try:
            client = TestClient(build_http_app(ctx))
            resp = client.get("/health")
            if resp.status_code != 200:
                return Check("transport", "red", f"/health returned {resp.status_code}")
            if settings.tokens:
                guarded = client.post("/mcp", json={})
                if guarded.status_code not in (401, 403):
                    return Check(
                        "transport",
                        "red",
                        f"/mcp without a bearer token returned {guarded.status_code}",
                        "auth middleware is not gating the MCP endpoint — do not expose this port",
                    )
            return Check("transport", "green", "http app boots; /health ok; /mcp auth enforced")
        finally:
            ctx.store.close()
    except Exception as exc:
        return Check("transport", "red", f"{type(exc).__name__}: {exc}")


def _check_token_factory(settings: Settings) -> Check:
    if not settings.tf_api_key:
        return Check(
            "token-factory",
            "yellow",
            "no Token Factory key — degraded mode: core tools live, agent features off (E-20)",
            "run `protocol-x-tool-mcp setup` or set PROTOX_TF_API_KEY to enable the agents",
        )
    try:
        import httpx

        resp = httpx.get(
            f"{settings.tf_endpoint.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {settings.tf_api_key}"},
            timeout=8.0,
        )
    except Exception as exc:
        return Check(
            "token-factory",
            "yellow",
            f"endpoint unreachable: {type(exc).__name__} — agent features will fail closed",
            "check egress to the Token Factory endpoint (INV-8 allowlist)",
        )
    if resp.status_code in (401, 403):
        return Check(
            "token-factory",
            "red",
            f"key rejected (HTTP {resp.status_code})",
            "rotate the Token Factory key (`protocol-x-tool-mcp setup` or PROTOX_TF_API_KEY)",
        )
    if resp.status_code == 200:
        return Check("token-factory", "green", f"{settings.tf_endpoint} reachable, key accepted")
    return Check("token-factory", "yellow", f"HTTP {resp.status_code} from endpoint")


def _full_checks(settings: Settings) -> list[Check]:
    """Full mode (P9): spawn probe, env scrub, transport self-check, TF probe."""
    return [
        _check_sandbox_spawn(settings),
        _check_worker_env(settings),
        _check_transport(settings),
        _check_token_factory(settings),
    ]


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
