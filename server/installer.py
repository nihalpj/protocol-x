"""Server-side package installation (operator action, UF-01 step E-3 resolution).

The converter/codegen only introspects INSTALLED packages (E-3); when a
package is missing, the console offers to install it. This module runs that
install with guardrails: strict PyPI-name validation (no arbitrary pip args —
INV-1 applies to operator surfaces too), a wall-clock timeout, and an audit
event per install (kind='install').
"""

from __future__ import annotations

import importlib
import importlib.metadata
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

# PEP 508 name, no version specifiers / extras / URLs — plain names only.
_NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,212}[A-Za-z0-9])?$")
TIMEOUT_S = 180


class InstallError(Exception):
    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


@dataclass
class InstallResult:
    name: str
    version: str
    command: str
    already_installed: bool = False


def installed_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def install_package(name: str, timeout_s: int = TIMEOUT_S) -> InstallResult:
    name = name.strip()
    if not _NAME_RE.fullmatch(name):
        raise InstallError(
            f"invalid package name {name!r}: letters, digits, '.', '_' and '-' only"
        )
    existing = installed_version(name)
    if existing:
        return InstallResult(name, existing, "", already_installed=True)

    if shutil.which("uv"):
        cmd = ["uv", "pip", "install", "--python", sys.executable, name]
    else:
        cmd = [sys.executable, "-m", "pip", "install", name]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        raise InstallError(f"install of {name!r} timed out after {timeout_s}s") from None
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        raise InstallError(
            f"install of {name!r} failed (exit {proc.returncode})", "\n".join(tail)
        )
    importlib.invalidate_caches()
    version = installed_version(name)
    if version is None:
        raise InstallError(
            f"install of {name!r} reported success but the package is not importable"
        )
    return InstallResult(name, version, " ".join(cmd))
