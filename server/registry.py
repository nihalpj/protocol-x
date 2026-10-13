"""ToolRegistry (F-002, F-037): lazy adapter discovery + schema assembly.

- Auto-discovers adapters/*.py (Hermes skills-folder pattern); modules are
  imported on first use, not at boot — cold start stays flat as the catalog
  grows. A JSON catalog cache (module mtime-keyed) lets tools/list answer
  without importing heavy modules.
- Tool state (live/draft/quarantined) is persisted in the DB and survives
  restarts; a quarantined tool is never re-activated by a reload (AC-P1-08).
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from adapters.base import ADAPTER_ATTRS, TOOL_DISABLED, UNKNOWN_TOOL, ToolError, ToolSpec
from core.db import Store
from core.hashing import module_source_hash  # noqa: F401  (re-export, back-compat)

log = logging.getLogger("protox.registry")

ADAPTERS_DIR = Path(__file__).resolve().parent.parent / "adapters"
SKIP_FILES = {"base.py", "__init__.py"}


@dataclass
class CatalogEntry:
    name: str
    description: str
    schema: dict[str, Any]
    adapter_module: str
    backend: str = "python"
    network: bool = False
    timeout_s: int = 30
    limits: dict[str, Any] = field(default_factory=dict)
    state: str = "live"  # live | draft | quarantined


def _module_names(adapters_dir: Path = ADAPTERS_DIR) -> list[str]:
    names = []
    for path in sorted(adapters_dir.glob("*.py")):
        if path.name in SKIP_FILES or path.name.startswith("_") or path.name.endswith(".draft.py"):
            continue
        names.append(path.stem)
    return names


class ToolRegistry:
    def __init__(self, store: Store, data_dir: Path, adapters_dir: Path = ADAPTERS_DIR):
        self.store = store
        self.adapters_dir = adapters_dir
        self.cache_path = Path(data_dir) / "catalog_cache.json"
        self.catalog: dict[str, CatalogEntry] = {}
        self._specs: dict[str, ToolSpec] = {}
        self._modules: dict[str, Any] = {}

    # -- discovery + catalog assembly -------------------------------------

    def load(self) -> None:
        """Discover adapters and assemble the catalog. Imports only stale modules."""
        cache = self._read_cache()
        new_cache: dict[str, Any] = {}
        for module in _module_names(self.adapters_dir):
            path = self.adapters_dir / f"{module}.py"
            mtime = path.stat().st_mtime
            cached = cache.get(module)
            if cached and cached.get("mtime") == mtime:
                entries = cached["tools"]
                log.debug("adapter %s catalog from cache (%d tools)", module, len(entries))
            else:
                try:
                    specs = self._import_specs(module)
                except Exception as exc:  # broken adapter must not break boot
                    log.warning("adapter %s failed to load: %s", module, exc)
                    continue
                entries = [self._entry_from_spec(module, s).__dict__ for s in specs]
                log.info("adapter %s loaded: %d tools", module, len(entries))
            new_cache[module] = {"mtime": mtime, "tools": entries}
            for entry_dict in entries:
                entry = CatalogEntry(**entry_dict)
                self.catalog[entry.name] = entry
        self._write_cache(new_cache)
        self._sync_db_state()
        log.info("registry catalog assembled: %d tools from %d adapters",
                 len(self.catalog), len(new_cache))

    def _sync_db_state(self) -> None:
        """Persist tool rows; never resurrect a quarantined tool (AC-P1-08)."""
        for entry in self.catalog.values():
            row = self.store.tools.get(entry.name)
            if row is None:
                version_id = self._record_version(entry.adapter_module)
                self.store.tools.upsert(
                    entry.name, "live", entry.adapter_module, entry.backend, version_id
                )
                entry.state = "live"
            else:
                entry.state = row["state"]

    def _record_version(self, module: str) -> int:
        existing = self.store.versions.latest(module)
        digest = module_source_hash(self.adapters_dir, module)
        if existing and existing["version_hash"] == digest:
            return int(existing["id"])
        return self.store.versions.add(module, digest, "built-in")

    def refresh_module(self, module: str) -> None:
        """Re-import one adapter module and update catalog + cache (used by P6–P8)."""
        path = self.adapters_dir / f"{module}.py"
        specs = self._import_specs(module, force=True)
        entries = [self._entry_from_spec(module, s).__dict__ for s in specs]
        cache = self._read_cache()
        cache[module] = {"mtime": path.stat().st_mtime, "tools": entries}
        self._write_cache(cache)
        for entry_dict in entries:
            self.catalog[entry_dict["name"]] = CatalogEntry(**entry_dict)
        self._sync_db_state()
        log.info("adapter %s refreshed: %d tools", module, len(entries))
        log.debug("adapter %s specs: %s", module, [s.name for s in specs])

    # -- lazy loading ------------------------------------------------------

    def _import_specs(self, module: str, force: bool = False) -> list[ToolSpec]:
        if force:
            self._modules.pop(module, None)
            stale = [n for n, s in self._specs.items() if True]
            for name in stale:
                entry = self.catalog.get(name)
                if entry and entry.adapter_module == module:
                    self._specs.pop(name, None)
        dotted = f"adapters.{module}"
        if module not in self._modules:
            path = self.adapters_dir / f"{module}.py"
            if force and dotted in sys.modules:
                mod = importlib.reload(sys.modules[dotted])
            else:
                spec = importlib.util.spec_from_file_location(dotted, path)
                if spec is None or spec.loader is None:
                    raise ImportError(f"cannot load adapter {path}")
                mod = importlib.util.module_from_spec(spec)
                sys.modules[dotted] = mod
                spec.loader.exec_module(mod)
            self._modules[module] = mod
        else:
            mod = self._modules[module]
        specs = None
        for attr in ADAPTER_ATTRS:
            candidate = getattr(mod, attr, None)
            if candidate is not None:
                specs = candidate() if callable(candidate) else candidate
                break
        if not specs:
            raise ValueError(f"adapter {module} exposes no ToolSpecs")
        for spec in specs:
            self._specs[spec.name] = spec
        return list(specs)

    def get_spec(self, name: str) -> ToolSpec:
        """Resolve a ToolSpec, lazy-importing its adapter on first use (F-037)."""
        spec = self._specs.get(name)
        if spec is not None:
            return spec
        entry = self.catalog.get(name)
        if entry is None:
            raise ToolError(UNKNOWN_TOOL, f"unknown tool: {name}")
        if entry.state == "quarantined":
            raise ToolError(TOOL_DISABLED, f"tool {name} is quarantined")
        if entry.state != "live":
            raise ToolError(UNKNOWN_TOOL, f"tool {name} is not live (state={entry.state})")
        for spec in self._import_specs(entry.adapter_module):
            if spec.name == name:
                return spec
        raise ToolError(UNKNOWN_TOOL, f"tool {name} missing from {entry.adapter_module}")

    def entry_state(self, name: str) -> str | None:
        entry = self.catalog.get(name)
        return entry.state if entry else None

    def set_state(self, name: str, state: str) -> None:
        self.store.tools.set_state(name, state)
        if name in self.catalog:
            self.catalog[name].state = state
        if state == "quarantined":
            # a cached lazy-loaded spec must not stay dispatchable (INV-6)
            self._specs.pop(name, None)

    # -- catalog cache ------------------------------------------------------

    def _read_cache(self) -> dict[str, Any]:
        try:
            return cast(dict[str, Any], json.loads(self.cache_path.read_text()))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_cache(self, cache: dict[str, Any]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache))
        tmp.replace(self.cache_path)

    @staticmethod
    def _entry_from_spec(module: str, spec: ToolSpec) -> CatalogEntry:
        schema = spec.input_model.model_json_schema()
        schema.setdefault("additionalProperties", False)
        return CatalogEntry(
            name=spec.name,
            description=spec.description,
            schema=schema,
            adapter_module=module,
            backend=spec.backend,
            network=spec.network,
            timeout_s=spec.timeout_s,
            limits=spec.limits,
        )

    def list_tools(self, include_non_live: bool = False) -> list[CatalogEntry]:
        entries = sorted(self.catalog.values(), key=lambda e: e.name)
        if include_non_live:
            return entries
        return [e for e in entries if e.state == "live"]


def timed(fn, *args, **kwargs):
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, (time.perf_counter() - start) * 1000
