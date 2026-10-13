import { useEffect, useState } from "react";
import { cn, fmtClock } from "@/lib/format";
import { copyText } from "@/lib/clipboard";
import type { ServerStatus } from "@/lib/types";

interface Props {
  status: ServerStatus | null;
  error: string | null;
  onOpenSettings: () => void;
}

export function HeaderBar({ status, error, onOpenSettings }: Props) {
  const [clock, setClock] = useState(() => new Date());
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    const t = setInterval(() => setClock(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  const serving = status?.status === "ok";
  const endpoint = status ? `http://${status.host}:${status.port}` : null;

  const copyEndpoint = async () => {
    if (!endpoint) return;
    if (await copyText(endpoint)) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }
  };

  return (
    <header className="flex h-12 shrink-0 items-center gap-3 border-b border-[#e4e4e7] bg-white px-4">
      <div className="flex items-center gap-2.5">
        <div className="flex h-6 w-6 items-center justify-center rounded-md bg-[#5e6ad2] text-[11px] font-bold text-white">
          X
        </div>
        <div className="leading-tight">
          <div className="text-[13px] font-semibold tracking-tight">Protocol-X Tool MCP</div>
          <div className="text-[10px] text-[#a1a1aa]">console · v{status?.version ?? "…"}</div>
        </div>
      </div>

      <div
        className={cn(
          "ml-2 flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[10px] font-medium",
          serving
            ? "border-[#bbf7d0] bg-[#f0fdf4] text-[#15803d]"
            : error
              ? "border-[#fecaca] bg-[#fef2f2] text-[#b91c1c]"
              : "border-[#e4e4e7] bg-[#fafafa] text-[#71717a]",
        )}
      >
        <span
          className={cn(
            "h-1.5 w-1.5 rounded-full",
            serving ? "pulse-dot bg-[#16a34a]" : error ? "bg-[#dc2626]" : "bg-[#a1a1aa]",
          )}
        />
        {serving ? "serving" : error ? "unreachable" : "connecting…"}
      </div>

      {status && (
        <span className="mono rounded bg-[#f4f4f5] px-1.5 py-0.5 text-[10px] text-[#71717a]">
          {status.tools} tools
        </span>
      )}

      {status && (
        <span className="mono rounded bg-[#eef0fb] px-1.5 py-0.5 text-[10px] font-medium text-[#5e6ad2]">
          preset: {status.preset}
        </span>
      )}

      {status && !status.agent_features_enabled && (
        <span
          className="rounded border border-[#fde68a] bg-[#fffbeb] px-1.5 py-0.5 text-[10px] font-medium text-[#b45309]"
          title="Agent features (package conversion, codegen) need a Token Factory API key"
        >
          degraded mode — add Token Factory key
        </span>
      )}

      {endpoint && (
        <button
          onClick={copyEndpoint}
          className="mono flex items-center gap-1.5 rounded-md border border-[#e4e4e7] bg-[#fafafa] px-2 py-1 text-[10.5px] text-[#71717a] transition-colors hover:border-[#c7c9e0] hover:text-[#18181b]"
          title="Copy endpoint"
        >
          {endpoint}
          <span className="text-[9px]">{copied ? "copied ✓" : "⧉"}</span>
        </button>
      )}

      <div className="ml-auto flex items-center gap-3">
        <span className="mono text-[10.5px] text-[#a1a1aa]">{fmtClock(clock)}</span>
        <button
          onClick={onOpenSettings}
          className="rounded-md border border-[#e4e4e7] bg-white px-2.5 py-1 text-[11px] font-medium text-[#3f3f46] transition-colors hover:bg-[#f4f4f5]"
        >
          ⚙ Settings
        </button>
      </div>
    </header>
  );
}
