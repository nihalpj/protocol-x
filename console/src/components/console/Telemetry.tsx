import { useMemo } from "react";
import { api, useApi } from "@/api";
import { cn, fmtTs } from "@/lib/format";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export function Telemetry() {
  const logsQ = useApi(() => api.logs(200), 5000);
  const logs = useMemo(() => logsQ.data?.logs ?? [], [logsQ.data]);

  const calls = logs.length;
  const errors = logs.filter((l) => l.outcome !== "ok" || (l.error_code ?? "") !== "").length;
  const avgMs = calls > 0 ? logs.reduce((s, l) => s + l.duration_ms, 0) / calls : null;

  return (
    <section className="flex h-full min-w-0 flex-1 flex-col bg-[#fafafa]">
      <div className="flex items-center gap-2 border-b border-[#e4e4e7] bg-white px-3 py-2">
        <div className="text-[12px] font-semibold">Telemetry</div>
        <span className="mono rounded bg-[#f4f4f5] px-1.5 py-0.5 text-[10px] text-[#71717a]">
          last {logs.length} calls · polls every 5 s
        </span>
      </div>

      <div className="grid grid-cols-3 gap-2 border-b border-[#e4e4e7] bg-white px-3 pb-3">
        {[
          { label: "tool calls", value: calls > 0 ? String(calls) : "—" },
          { label: "errors", value: calls > 0 ? String(errors) : "—" },
          { label: "avg duration", value: avgMs != null ? `${avgMs.toFixed(1)} ms` : "—" },
        ].map((c) => (
          <div key={c.label} className="rounded-lg border border-[#e4e4e7] bg-[#fafafa] px-2.5 py-2">
            <div className="text-[9px] uppercase tracking-wide text-[#a1a1aa]">{c.label}</div>
            <div className="mono mt-0.5 text-[14px] font-semibold text-[#18181b]">{c.value}</div>
          </div>
        ))}
      </div>

      <div className="min-h-0 flex-1 overflow-auto bg-white p-3 scroll-thin">
        {logsQ.loading && <div className="py-10 text-center text-[11px] text-[#a1a1aa]">loading logs…</div>}
        {logsQ.error && !logsQ.loading && (
          <div className="py-10 text-center text-[11px] text-[#b91c1c]">failed to load logs: {logsQ.error}</div>
        )}
        {!logsQ.loading && logs.length === 0 && (
          <div className="py-10 text-center text-[11px] text-[#a1a1aa]">no tool calls logged yet</div>
        )}
        {logs.length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-24">time</TableHead>
                <TableHead>tool</TableHead>
                <TableHead className="text-right">duration</TableHead>
                <TableHead>outcome</TableHead>
                <TableHead className="text-right">exit</TableHead>
                <TableHead className="text-right">result size</TableHead>
                <TableHead>error</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {[...logs].reverse().map((l) => (
                <TableRow key={l.tool_call_id}>
                  <TableCell className="mono text-[10.5px] text-[#71717a]">{fmtTs(l.ts)}</TableCell>
                  <TableCell className="mono text-[10.5px] font-medium">{l.tool}</TableCell>
                  <TableCell className="mono text-right text-[10.5px]">{l.duration_ms} ms</TableCell>
                  <TableCell
                    className={cn(
                      "mono text-[10.5px] font-medium",
                      l.outcome === "ok" ? "text-[#15803d]" : "text-[#b91c1c]",
                    )}
                  >
                    {l.outcome}
                  </TableCell>
                  <TableCell className="mono text-right text-[10.5px] text-[#71717a]">{l.exit_code}</TableCell>
                  <TableCell className="mono text-right text-[10.5px] text-[#71717a]">{l.result_size} B</TableCell>
                  <TableCell className="mono text-[10.5px] text-[#b91c1c]">{l.error_code ?? ""}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>
    </section>
  );
}
