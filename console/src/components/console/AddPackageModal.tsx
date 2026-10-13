import { useState } from "react";
import { toast } from "sonner";
import { api } from "@/api";
import type { IntrospectFunction, JudgeVerdictInfo } from "@/api";
import type { PypiOk } from "@/lib/types";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

interface Props {
  onClose: () => void;
  agentEnabled: boolean;
}

type Phase = "idle" | "searching" | "ok" | "not-found" | "offline";

type Action =
  | { kind: "idle" }
  | { kind: "busy"; label: string }
  | { kind: "not-installed"; package: string; message: string; resume: "generate" | "convert" }
  | { kind: "choose"; functions: IntrospectFunction[] }
  | { kind: "generated"; module: string; path: string; tools: string[] }
  | { kind: "judged"; state: string; tools: string[]; diagnostics: string[]; verdict: JudgeVerdictInfo | null }
  | { kind: "failed"; title: string; lines: string[] };

export function AddPackageModal({ onClose, agentEnabled }: Props) {
  const [name, setName] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [pkg, setPkg] = useState<PypiOk | null>(null);
  const [message, setMessage] = useState("");
  const [action, setAction] = useState<Action>({ kind: "idle" });
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const doSearch = async (q: string) => {
    const query = q.trim();
    if (!query) return;
    setAction({ kind: "idle" });
    setPhase("searching");
    const r = await api.pypi(query);
    if (r.state === "ok") {
      setPkg(r);
      setMessage("");
      setPhase("ok");
    } else {
      setPkg(null);
      setMessage(r.message);
      setPhase(r.state);
    }
  };

  const fail = (title: string, e: unknown) =>
    setAction({ kind: "failed", title, lines: [e instanceof Error ? e.message : String(e)] });

  // "generate adapter →" — introspect, let the operator pick functions, render a draft.
  const onGenerate = async () => {
    if (!pkg) return;
    setAction({ kind: "busy", label: "introspecting installed package…" });
    const intro = await api.introspect(pkg.name).catch((e: unknown) => {
      fail("introspection failed", e);
      return null;
    });
    if (!intro) return;
    if (intro.state !== "ok") {
      setAction({ kind: "not-installed", package: pkg.name, message: intro.message, resume: "generate" });
      return;
    }
    setSelected(new Set(intro.functions.map((f) => f.name)));
    setAction({ kind: "choose", functions: intro.functions });
  };

  const onGenerateSelected = async (functions: string[]) => {
    if (!pkg || functions.length === 0) return;
    setAction({ kind: "busy", label: `generating adapter for ${functions.length} function(s)…` });
    try {
      const r = await api.generateAdapter(pkg.name, functions);
      if (r.state === "generated" && r.module) {
        setAction({ kind: "generated", module: r.module, path: r.path ?? "", tools: r.tools ?? [] });
        toast.success(`adapter draft written — ${r.tools?.length ?? 0} tool(s), awaiting judge`);
      } else {
        setAction({ kind: "failed", title: "static gate rejected the generated adapter", lines: r.diagnostics ?? [] });
      }
    } catch (e) {
      fail("generation failed", e);
    }
  };

  const onJudge = async (module: string) => {
    setAction({ kind: "busy", label: "AI-judge reviewing adapter…" });
    try {
      const r = await api.judgeAdapter(module);
      setAction({ kind: "judged", state: r.state, tools: r.tools, diagnostics: r.diagnostics, verdict: r.verdict });
      if (r.state === "merged") toast.success(`${r.tools.length} tool(s) merged and live`);
      else toast.warning(`judge: ${r.state}`);
    } catch (e) {
      fail("judge failed", e);
    }
  };

  // "convert to tools" — one click: introspect all → generate → judge → merge.
  const onConvert = async () => {
    if (!pkg) return;
    setAction({ kind: "busy", label: "introspecting installed package…" });
    const intro = await api.introspect(pkg.name).catch((e: unknown) => {
      fail("introspection failed", e);
      return null;
    });
    if (!intro) return;
    if (intro.state !== "ok") {
      setAction({ kind: "not-installed", package: pkg.name, message: intro.message, resume: "convert" });
      return;
    }
    setAction({ kind: "busy", label: `generating adapter for ${intro.functions.length} function(s)…` });
    try {
      const g = await api.generateAdapter(pkg.name, intro.functions.map((f) => f.name));
      if (g.state !== "generated" || !g.module) {
        setAction({ kind: "failed", title: "static gate rejected the generated adapter", lines: g.diagnostics ?? [] });
        return;
      }
      await onJudge(g.module);
    } catch (e) {
      fail("conversion failed", e);
    }
  };

  const onInstall = async (packageName: string, resume: "generate" | "convert") => {
    setAction({ kind: "busy", label: `installing ${packageName} on the server (uv pip install)…` });
    const r = await api.installPackage(packageName);
    if (r.state !== "installed") {
      setAction({ kind: "failed", title: `could not install ${packageName}`, lines: [r.message] });
      return;
    }
    toast.success(`${r.package} ${r.version} installed on the server`);
    // resume the flow the operator originally chose
    if (resume === "convert") await onConvert();
    else await onGenerate();
  };

  const busy = action.kind === "busy";
  const actionDisabled = !agentEnabled || busy;
  const actionTooltip = agentEnabled
    ? "generate: draft adapter from selected functions · convert: full pipeline (generate → judge → merge)"
    : "requires a Token Factory key (degraded mode)";

  const toggle = (fn: string) => {
    const next = new Set(selected);
    if (next.has(fn)) next.delete(fn);
    else next.add(fn);
    setSelected(next);
  };

  return (
    <TooltipProvider delayDuration={150}>
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/25 p-6" onClick={onClose}>
        <div
          className="flex max-h-[86vh] w-[560px] flex-col overflow-hidden rounded-xl border border-[#e4e4e7] bg-white shadow-2xl"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex items-center justify-between border-b border-[#e4e4e7] px-4 py-3">
            <div>
              <div className="text-[13px] font-semibold">Add package</div>
              <div className="text-[10px] text-[#a1a1aa]">
                look up a PyPI package, then convert it into sandboxed tools
              </div>
            </div>
            <button onClick={onClose} className="rounded p-1 text-[#a1a1aa] hover:bg-[#f4f4f5] hover:text-[#3f3f46]">
              ✕
            </button>
          </div>

          <div className="flex-1 space-y-3 overflow-y-auto px-4 py-3 scroll-thin">
            <div className="flex gap-2">
              <input
                autoFocus
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && void doSearch(name)}
                placeholder="e.g. reportlab"
                className="mono flex-1 rounded-md border border-[#e4e4e7] px-2.5 py-2 text-[12px] outline-none focus:border-[#5e6ad2]"
              />
              <button
                onClick={() => void doSearch(name)}
                disabled={phase === "searching"}
                className="rounded-md bg-[#5e6ad2] px-3.5 py-2 text-[11.5px] font-medium text-white hover:bg-[#4f5ac0] disabled:opacity-40"
              >
                {phase === "searching" ? "searching…" : "search PyPI"}
              </button>
            </div>

            {phase === "not-found" && (
              <div className="rounded-md border border-[#fecaca] bg-[#fef2f2] px-3 py-2.5">
                <div className="text-[11px] font-semibold text-[#b91c1c]">not found</div>
                <div className="mt-0.5 text-[10.5px] text-[#52525b]">{message}</div>
                <div className="mt-1 text-[10px] text-[#71717a]">check the spelling and retry — the search box stays live.</div>
              </div>
            )}

            {phase === "offline" && (
              <div className="rounded-md border border-[#fde68a] bg-[#fffbeb] px-3 py-2.5">
                <div className="text-[11px] font-semibold text-[#b45309]">PyPI offline</div>
                <div className="mt-0.5 text-[10.5px] text-[#52525b]">{message}</div>
                <button
                  onClick={() => void doSearch(name)}
                  className="mt-2 rounded-md border border-[#fde68a] bg-white px-2.5 py-1 text-[10.5px] font-medium text-[#b45309] hover:bg-[#fef3c7]"
                >
                  ↻ retry
                </button>
              </div>
            )}

            {phase === "ok" && pkg && (
              <div className="space-y-3">
                <div className="rounded-lg border border-[#e4e4e7] bg-[#fafafa] px-3 py-2.5">
                  <div className="flex items-baseline gap-2">
                    <span className="mono text-[13px] font-semibold">{pkg.name}</span>
                    <span className="mono text-[10px] text-[#71717a]">{pkg.version}</span>
                    <span className="mono ml-auto text-[9.5px] text-[#a1a1aa]">
                      {pkg.license ?? "license unknown"}
                      {pkg.requires_python && ` · python ${pkg.requires_python}`}
                    </span>
                  </div>
                  {pkg.summary && <div className="mt-1 text-[11px] text-[#52525b]">{pkg.summary}</div>}
                  {pkg.home_page && (
                    <div className="mono mt-1 truncate text-[9.5px] text-[#a1a1aa]" title={pkg.home_page}>
                      {pkg.home_page}
                    </div>
                  )}
                </div>

                <div className="flex gap-2">
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <span className="flex-1">
                        <button
                          disabled={actionDisabled}
                          onClick={() => void onGenerate()}
                          className="w-full rounded-md bg-[#5e6ad2] px-3 py-2 text-[11.5px] font-medium text-white transition-colors hover:bg-[#4f5ac0] disabled:opacity-40"
                        >
                          generate adapter →
                        </button>
                      </span>
                    </TooltipTrigger>
                    <TooltipContent side="bottom">{actionTooltip}</TooltipContent>
                  </Tooltip>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <span className="flex-1">
                        <button
                          disabled={actionDisabled}
                          onClick={() => void onConvert()}
                          className="w-full rounded-md border border-[#e4e4e7] bg-white px-3 py-2 text-[11.5px] font-medium text-[#3f3f46] transition-colors hover:bg-[#f4f4f5] disabled:opacity-40"
                        >
                          convert to tools
                        </button>
                      </span>
                    </TooltipTrigger>
                    <TooltipContent side="bottom">{actionTooltip}</TooltipContent>
                  </Tooltip>
                </div>
                {!agentEnabled && (
                  <div className="rounded-md border border-[#fde68a] bg-[#fffbeb] px-3 py-2 text-[10.5px] text-[#b45309]">
                    degraded mode — add a Token Factory key in Settings to enable package conversion.
                  </div>
                )}

                {busy && (
                  <div className="rounded-md border border-[#e4e4e7] bg-[#fafafa] px-3 py-2.5 text-[11px] text-[#52525b]">
                    <span className="mr-2 inline-block animate-spin">◌</span>
                    {action.label}
                  </div>
                )}

                {action.kind === "not-installed" && (
                  <div className="space-y-2 rounded-lg border border-[#fde68a] bg-[#fffbeb] px-3 py-2.5">
                    <div className="text-[11px] font-semibold text-[#b45309]">
                      package not installed on the server
                    </div>
                    <div className="text-[10.5px] text-[#52525b]">{action.message}</div>
                    <button
                      onClick={() => void onInstall(action.package, action.resume)}
                      className="rounded-md bg-[#b45309] px-3 py-1.5 text-[11px] font-medium text-white hover:bg-[#92400e]"
                    >
                      install {action.package} on the server →
                    </button>
                    <div className="text-[10px] text-[#71717a]">
                      runs <span className="mono">uv pip install {action.package}</span> in the server
                      environment and is written to the audit log.
                    </div>
                  </div>
                )}

                {action.kind === "choose" && (
                  <div className="space-y-2 rounded-lg border border-[#e4e4e7] px-3 py-2.5">
                    <div className="text-[11px] font-semibold text-[#3f3f46]">
                      {action.functions.length} candidate function(s) — select what becomes a tool
                    </div>
                    <div className="max-h-44 space-y-1 overflow-y-auto scroll-thin">
                      {action.functions.map((f) => (
                        <label key={f.name} className="flex items-start gap-2 text-[11px] text-[#52525b]">
                          <input
                            type="checkbox"
                            checked={selected.has(f.name)}
                            onChange={() => toggle(f.name)}
                            className="mt-0.5"
                          />
                          <span>
                            <span className="mono text-[10.5px] font-medium text-[#3f3f46]">
                              {f.name}
                              {f.signature}
                            </span>
                            {f.doc && <span className="block text-[10px] text-[#a1a1aa]">{f.doc.slice(0, 120)}</span>}
                          </span>
                        </label>
                      ))}
                    </div>
                    <button
                      disabled={selected.size === 0}
                      onClick={() => void onGenerateSelected([...selected])}
                      className="rounded-md bg-[#5e6ad2] px-3 py-1.5 text-[11px] font-medium text-white hover:bg-[#4f5ac0] disabled:opacity-40"
                    >
                      generate {selected.size} tool(s) →
                    </button>
                  </div>
                )}

                {action.kind === "generated" && (
                  <div className="space-y-2 rounded-lg border border-[#bbf7d0] bg-[#f0fdf4] px-3 py-2.5">
                    <div className="text-[11px] font-semibold text-[#15803d]">
                      adapter draft written — {action.tools.length} tool(s)
                    </div>
                    <div className="mono text-[10px] text-[#52525b]">{action.path}</div>
                    <div className="mono text-[10px] text-[#52525b]">{action.tools.join(", ")}</div>
                    <button
                      onClick={() => void onJudge(action.module)}
                      className="rounded-md bg-[#15803d] px-3 py-1.5 text-[11px] font-medium text-white hover:bg-[#166534]"
                    >
                      judge &amp; merge →
                    </button>
                  </div>
                )}

                {action.kind === "judged" && (
                  <div
                    className={`space-y-1.5 rounded-lg border px-3 py-2.5 ${
                      action.state === "merged"
                        ? "border-[#bbf7d0] bg-[#f0fdf4]"
                        : "border-[#fecaca] bg-[#fef2f2]"
                    }`}
                  >
                    <div
                      className={`text-[11px] font-semibold ${
                        action.state === "merged" ? "text-[#15803d]" : "text-[#b91c1c]"
                      }`}
                    >
                      {action.state === "merged"
                        ? `merged — ${action.tools.length} tool(s) live in the registry`
                        : `blocked — ${action.state}`}
                    </div>
                    {action.tools.length > 0 && (
                      <div className="mono text-[10px] text-[#52525b]">{action.tools.join(", ")}</div>
                    )}
                    {action.verdict && (
                      <div className="text-[10px] text-[#71717a]">
                        judge verdict: {action.verdict.verdict} · {action.verdict.model}
                      </div>
                    )}
                    {(action.verdict?.findings.length || action.diagnostics.length > 0) && (
                      <ul className="list-inside list-disc space-y-0.5 text-[10px] text-[#71717a]">
                        {(action.verdict?.findings ?? []).map((f, i) => (
                          <li key={`f${i}`}>{f}</li>
                        ))}
                        {action.diagnostics.map((d, i) => (
                          <li key={`d${i}`}>{d}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                )}

                {action.kind === "failed" && (
                  <div className="space-y-1 rounded-lg border border-[#fecaca] bg-[#fef2f2] px-3 py-2.5">
                    <div className="text-[11px] font-semibold text-[#b91c1c]">{action.title}</div>
                    <ul className="list-inside list-disc space-y-0.5 text-[10px] text-[#52525b]">
                      {action.lines.map((l, i) => (
                        <li key={i}>{l}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )}

            {phase === "idle" && (
              <div className="text-[10.5px] leading-relaxed text-[#71717a]">
                The converter introspects the installed package and never guesses from the index — the PyPI
                lookup here only confirms the package exists and shows its metadata. If the package is missing
                from the server environment, the console offers to install it for you.
              </div>
            )}
          </div>

          <div className="flex items-center justify-end border-t border-[#e4e4e7] px-4 py-2.5">
            <button
              onClick={onClose}
              className="rounded-md border border-[#e4e4e7] bg-white px-4 py-2 text-[11.5px] font-medium text-[#3f3f46] hover:bg-[#f4f4f5]"
            >
              close
            </button>
          </div>
        </div>
      </div>
    </TooltipProvider>
  );
}
