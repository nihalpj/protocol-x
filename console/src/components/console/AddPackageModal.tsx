import { useState } from "react";
import { toast } from "sonner";
import { api } from "@/api";
import type { PypiOk } from "@/lib/types";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";

interface Props {
  onClose: () => void;
  agentEnabled: boolean;
}

type Phase = "idle" | "searching" | "ok" | "not-found" | "offline";

export function AddPackageModal({ onClose, agentEnabled }: Props) {
  const [name, setName] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [pkg, setPkg] = useState<PypiOk | null>(null);
  const [message, setMessage] = useState("");

  const doSearch = async (q: string) => {
    const query = q.trim();
    if (!query) return;
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

  // codegen/convert land in a later phase — when agent features are off the
  // buttons stay disabled with a tooltip; when on they explain what's next.
  const actionDisabled = !agentEnabled;
  const actionTooltip = agentEnabled
    ? "codegen & convert actions land in a later phase"
    : "requires a Token Factory key (degraded mode)";
  const onAction = () => toast.info("codegen & convert actions land in a later phase");

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
                          onClick={onAction}
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
                          onClick={onAction}
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
              </div>
            )}

            {phase === "idle" && (
              <div className="text-[10.5px] leading-relaxed text-[#71717a]">
                The converter introspects the installed package and never guesses from the index — the PyPI
                lookup here only confirms the package exists and shows its metadata.
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
