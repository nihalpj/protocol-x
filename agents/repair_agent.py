"""Runtime self-repair agent (F-030) — PRD v1.10.

A failing generated adapter heals itself without human action:

  capture    — the dispatcher records a repair_incidents row for every
               non-validation ToolError from a *generated* adapter
               (built-in launch-core adapters never self-modify, INV-7)
  eligibility— >= ELIGIBILITY_THRESHOLD same-class failures inside
               SAME_CLASS_WINDOW_S trigger a repair attempt
  repair     — diagnose + patch via the fast Nemotron tier (the model returns
               a full corrected adapter; it never runs anything), then the
               same gates as generation: static gate (E-4) -> AI-judge
               (INV-6) -> smoke replay of the captured failing input -> hot
               swap via registry.refresh_module
  rollback   — any failed gate restores the pre-patch snapshot and records
               the incident 'rolled-back' (adapter_versions.parent_id chain)
  breaker    — BREAKER_MAX_FAILED rolled-back repairs in BREAKER_WINDOW_S
               quarantine the whole module (TOOL_DISABLED); a console
               re-enable resets the breaker window

The repair subsystem must never break dispatch: every capture is wrapped
in try/except at the call site, and background attempts are daemon threads.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from adapters.base import VALIDATION_ERROR, ToolError

SAME_CLASS_WINDOW_S = 600.0     # 10 min eligibility window
ELIGIBILITY_THRESHOLD = 2       # same-class failures before a repair attempt
BREAKER_WINDOW_S = 86400.0      # 24 h circuit-breaker window
BREAKER_MAX_FAILED = 3          # rolled-back repairs before quarantine


class RepairAgent:
    def __init__(self, ctx):
        self.ctx = ctx
        self._in_flight: set[str] = set()
        self._lock = threading.Lock()
        self._client = None
        self._judge = None
        if ctx.settings.agent_features_enabled:
            from agents.judge import Judge
            from agents.tokenfactory import TokenFactoryClient

            self._client = TokenFactoryClient(
                ctx.settings.tf_api_key, ctx.settings.tf_endpoint)
            self._judge = Judge(self._client, ctx.settings.tf_judge_model)

    @property
    def _versions_dir(self) -> Path:
        path = self.ctx.settings.data_dir / "versions"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------ capture
    def maybe_capture(self, tool_name: str, exc: ToolError,
                      args: dict[str, Any], *, sync: bool = False) -> int | None:
        """Record a failure; trigger a repair attempt when eligible.

        Returns the incident id, or None when the failure is not repairable
        (validation errors, unknown/built-in/quarantined tools)."""
        if not isinstance(exc, ToolError) or exc.code == VALIDATION_ERROR:
            return None
        entry = self.ctx.registry.catalog.get(tool_name)
        if entry is None or entry.state != "live":
            return None
        module = entry.adapter_module
        version = self.ctx.store.versions.latest(module)
        if not version or version["source"] not in ("generated", "manual"):
            return None  # built-in launch-core adapters never self-modify
        incident_id = self.ctx.store.incidents.add(
            tool_name, exc.code,
            json.dumps(args, default=str)[:4000], version["id"])
        self.ctx.audit.record("repair", "repair-agent", {
            "event": "captured", "tool": tool_name,
            "error_class": exc.code, "incident": incident_id})
        count = self.ctx.store.incidents.count_same_class(
            tool_name, exc.code, SAME_CLASS_WINDOW_S)
        if count >= ELIGIBILITY_THRESHOLD:
            with self._lock:
                if tool_name in self._in_flight:
                    return incident_id
                self._in_flight.add(tool_name)
            if sync:
                self._attempt_guarded(incident_id)
            else:
                threading.Thread(
                    target=self._attempt_guarded, args=(incident_id,),
                    daemon=True, name=f"repair-{tool_name}").start()
        return incident_id

    def _attempt_guarded(self, incident_id: int) -> None:
        try:
            self.attempt_repair(incident_id)
        except Exception as exc:  # repair must never take the server down
            self.ctx.audit.record("repair", "repair-agent", {
                "event": "attempt-error", "incident": incident_id,
                "error": f"{type(exc).__name__}: {exc}"})
        finally:
            with self._lock:
                incident = self.ctx.store.incidents.get(incident_id)
                if incident:
                    self._in_flight.discard(incident["tool"])

    # ------------------------------------------------------------- repair
    def attempt_repair(self, incident_id: int) -> dict[str, Any]:
        """Synchronous repair attempt — also the test seam."""
        from core.hashing import module_source_hash

        ctx = self.ctx
        incident = ctx.store.incidents.get(incident_id)
        if incident is None:
            return {"state": "unknown-incident"}
        tool = incident["tool"]
        entry = ctx.registry.catalog.get(tool)
        if entry is None:
            return {"state": "unknown-tool"}
        module = entry.adapter_module
        attempts = int(incident["attempts"]) + 1

        if self._breaker_tripped(tool):
            return self._quarantine(module, tool, incident_id, attempts)

        path = ctx.registry.adapters_dir / f"{module}.py"
        original = path.read_text()
        pre_hash = module_source_hash(ctx.registry.adapters_dir, module)
        self._snapshot(pre_hash, original)

        patched = self._diagnose_and_patch(incident, original)
        failure: str | None = None
        if patched is None:
            failure = "no repair model available or no patch produced"
        else:
            from agents.codegen import static_gate

            problems = static_gate(patched)
            if problems:
                failure = "static gate: " + "; ".join(problems)
            elif self._judge is None:
                failure = "judge unavailable (degraded mode E-20) — fail closed"
            else:
                from agents.judge import JudgeUnavailable

                try:
                    verdict = self._judge.review(patched, module)
                except JudgeUnavailable as exc:
                    verdict = None
                    failure = f"judge unavailable: {exc} — fail closed"
                if verdict is not None:
                    ctx.audit.record("verdict", "judge",
                                     {**verdict.to_dict(), "module": module,
                                      "context": "repair"})
                    if verdict.verdict == "blocked":
                        failure = ("judge blocked: "
                                   + json.dumps(verdict.findings, default=str))

        if failure is None and patched is not None:
            path.write_text(patched)
            ctx.registry.refresh_module(module)
            smoke_error = self._smoke_replay(tool, incident.get("input_json"))
            if smoke_error is None:
                new_hash = module_source_hash(ctx.registry.adapters_dir, module)
                ctx.store.versions.add(module, new_hash, "generated",
                                       parent_id=incident["adapter_version_id"])
                ctx.store.incidents.set_state(incident_id, "healed", attempts)
                ctx.audit.record("repair", "repair-agent", {
                    "event": "healed", "tool": tool, "module": module,
                    "incident": incident_id, "attempts": attempts})
                return {"state": "healed", "tool": tool}
            failure = f"smoke replay failed: {smoke_error}"

        # rollback: restore the snapshot, keep the version chain honest
        path.write_text(original)
        ctx.registry.refresh_module(module)
        latest = ctx.store.versions.latest(module)
        ctx.store.versions.add(
            module, pre_hash, "generated",
            parent_id=latest["id"] if latest else None)
        ctx.store.incidents.set_state(incident_id, "rolled-back", attempts)
        ctx.audit.record("repair", "repair-agent", {
            "event": "rolled-back", "tool": tool, "module": module,
            "incident": incident_id, "reason": failure})
        if self._breaker_tripped(tool):
            return self._quarantine(module, tool, incident_id, attempts)
        return {"state": "rolled-back", "tool": tool, "reason": failure}

    def _diagnose_and_patch(self, incident: dict, source: str) -> str | None:
        """Fast-model tier: return the full corrected adapter source.

        The model's output is untrusted text — it only becomes code after the
        static gate, the judge, and a smoke replay all pass."""
        if self._client is None:
            return None
        from agents.tokenfactory import InferenceError

        prompt = (
            f"This generated MCP adapter failed at runtime with "
            f"{incident['error_class']} on input {incident.get('input_json')}.\n\n"
            f"```python\n{source}\n```\n\n"
            "Return ONLY the full corrected adapter source in one python code "
            "fence. Keep every invariant: fixed signatures, no eval/exec, "
            "structured ToolError failures, statelessness.")
        try:
            completion = self._client.chat(
                self.ctx.settings.tf_fast_model,
                [{"role": "user", "content": prompt}])
        except InferenceError:
            return None
        text = completion.content.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [ln for ln in lines if not ln.startswith("```")]
            text = "\n".join(lines)
        return text if text.strip().startswith("#") or "ToolSpec" in text else None

    def _smoke_replay(self, tool: str, input_json: str | None) -> str | None:
        """Re-run the captured failing input in the sandbox (E-13 replay)."""
        from core.sandbox.runner import CallRequest

        ctx = self.ctx
        try:
            spec = ctx.registry.get_spec(tool)
            args = json.loads(input_json) if input_json else {}
            validated = spec.input_model.model_validate(args)
            req = CallRequest(
                adapter_module=spec_adapter_module(ctx, tool),
                tool_name=tool,
                args=validated.model_dump(),
                workspace=str(ctx.workspace.root),
                timeout_s=spec.timeout_s or ctx.settings.default_timeout_s,
                network=spec.network,
                backend=spec.backend,
                cpu_s=ctx.settings.limit_cpu_s,
                rss_mb=int(spec.limits.get("rss_mb", ctx.settings.limit_rss_mb)),
                fs_mb=ctx.settings.limit_fs_mb,
            )
            ctx.runner.run(req)
        except ToolError as exc:
            return f"{exc.code}: {exc.message}"
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        return None

    # ------------------------------------------------- breaker + quarantine
    def _breaker_tripped(self, tool: str) -> bool:
        reenabled = [
            e["ts"] for e in self.ctx.store.audit.query(kind="repair", limit=500)
            if e["payload"].get("event") == "re-enabled"
            and e["payload"].get("tool") == tool
        ]
        since_ts = max(reenabled) if reenabled else None
        return (
            self.ctx.store.incidents.count_failed_repairs(
                tool, BREAKER_WINDOW_S, since_ts=since_ts)
            >= BREAKER_MAX_FAILED
        )

    def _quarantine(self, module: str, tool: str, incident_id: int,
                    attempts: int) -> dict[str, Any]:
        for entry in self.ctx.registry.list_tools(include_non_live=True):
            if entry.adapter_module == module:
                self.ctx.registry.set_state(entry.name, "quarantined")
        self.ctx.store.incidents.set_state(incident_id, "quarantined", attempts)
        self.ctx.audit.record("repair", "repair-agent", {
            "event": "quarantined", "module": module, "tool": tool,
            "incident": incident_id})
        return {"state": "quarantined", "module": module}

    def reenable(self, tool: str) -> dict[str, Any]:
        """Console re-enable: tool goes live again, breaker window resets."""
        self.ctx.registry.set_state(tool, "live")
        self.ctx.audit.record("repair", "operator", {
            "event": "re-enabled", "tool": tool})
        return {"state": "live", "tool": tool}

    # ------------------------------------------------------------- helpers
    def _snapshot(self, digest: str, source: str) -> Path:
        path = self._versions_dir / f"{digest}.py"
        if not path.exists():
            path.write_text(source)
        return path


def spec_adapter_module(ctx, tool_name: str) -> str:
    entry = ctx.registry.catalog.get(tool_name)
    return entry.adapter_module if entry else tool_name
