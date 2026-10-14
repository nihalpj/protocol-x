import { useState } from "react";
import { toast } from "sonner";
import { api, useApi } from "@/api";
import type { DraftTool } from "@/api";
import { cn, fmtDateTime, fmtTs, payloadOf } from "@/lib/format";
import type { AuditEvent } from "@/lib/types";

function str(v: unknown): string | null {
  return typeof v === "string" && v ? v : null;
}

function verdictTone(verdict: string): string {
  if (verdict.startsWith("pass") || verdict === "approved") return "bg-[#f0fdf4] text-[#15803d]";
  if (verdict.startsWith("fail") || verdict === "blocked") return "bg-[#fef2f2] text-[#b91c1c]";
  return "bg-[#f4f4f5] text-[#52525b]";
}

function VerdictList({ events }: { events: AuditEvent[] }) {
  if (events.length === 0) return <Empty text="no verdicts recorded yet" />;
  return (
    <div className="space-y-1.5">
      {events.map((e) => {
        const p = payloadOf(e);
        const verdict = str(p.verdict) ?? str(p.outcome) ?? e.kind;
        const model = str(p.model) ?? str(p.judge_model) ?? e.actor;
        return (
          <div key={e.id} className="flex items-center gap-2 rounded-md border border-[#f4f4f5] bg-white px-2.5 py-1.5">
            <span className={cn("rounded px-1.5 py-px text-[9px] font-semibold uppercase", verdictTone(verdict))}>
              {verdict}
            </span>
            <span className="mono text-[10px] text-[#3f3f46]">{model}</span>
            <span className="mono ml-auto shrink-0 text-[9px] text-[#a1a1aa]">{fmtTs(e.ts)}</span>
          </div>
        );
      })}
    </div>
  );
}

function RejectionList({ events }: { events: AuditEvent[] }) {
  if (events.length === 0) return <Empty text="no rejections — nothing has been blocked yet" />;
  return (
    <div className="space-y-1.5">
      {events.map((e) => {
        const p = payloadOf(e);
        const reason = str(p.reason_code) ?? str(p.reason) ?? str(p.summary) ?? JSON.stringify(p);
        const subject = str(p.package) ?? str(p.tool) ?? null;
        return (
          <div key={e.id} className="rounded-md border border-[#fecaca] bg-[#fef2f2]/60 px-2.5 py-1.5">
            <div className="flex items-center gap-2">
              <span className="mono text-[10px] font-semibold text-[#b91c1c]">rejected</span>
              {subject && <span className="mono text-[10px] text-[#3f3f46]">{subject}</span>}
              <span className="mono ml-auto shrink-0 text-[9px] text-[#a1a1aa]">{fmtTs(e.ts)}</span>
            </div>
            <div className="mono mt-0.5 truncate text-[9.5px] text-[#52525b]" title={reason}>
              {reason}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return (
    <div className="rounded-lg border border-dashed border-[#e4e4e7] px-3 py-4 text-center text-[10.5px] text-[#a1a1aa]">
      {text}
    </div>
  );
}

function Card({
  title,
  action,
  children,
}: {
  title: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="flex max-h-72 min-w-0 flex-col rounded-xl border border-[#e4e4e7] bg-[#fafafa] p-3">
      <div className="mb-2 flex shrink-0 items-center gap-2 text-[9.5px] font-semibold uppercase tracking-[0.08em] text-[#a1a1aa]">
        <span>{title}</span>
        {action && <span className="ml-auto normal-case tracking-normal">{action}</span>}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto pr-1 scroll-thin">{children}</div>
    </div>
  );
}

/** Draft tools grouped by adapter module, with a button to run the AI judge
 *  and complete the pipeline (judge → auto-merge on pass). */
function DraftList({ drafts, agentEnabled }: { drafts: DraftTool[]; agentEnabled: boolean }) {
  const [judging, setJudging] = useState<string | null>(null);
  if (drafts.length === 0) return <Empty text="no draft tools" />;

  const byModule = new Map<string, string[]>();
  for (const d of drafts) {
    const list = byModule.get(d.module) ?? [];
    list.push(d.name);
    byModule.set(d.module, list);
  }

  const onJudge = async (module: string) => {
    setJudging(module);
    try {
      const r = await api.judgeAdapter(module);
      if (r.state === "merged") toast.success(`${module}: ${r.tools.length} tool(s) judged, merged and live`);
      else if (r.state === "blocked") toast.error(`${module}: judge blocked the adapter`);
      else toast.warning(`${module}: judge ${r.state}`);
    } catch (e) {
      toast.error(`${module}: judge failed — ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setJudging(null);
    }
  };

  return (
    <div className="space-y-1.5">
      {[...byModule.entries()].map(([module, tools]) => (
        <div key={module} className="rounded-md border border-[#fde68a] bg-[#fffbeb]/60 px-2.5 py-1.5">
          <div className="flex items-center gap-2">
            <span className="mono text-[10px] font-semibold text-[#b45309]">{module}</span>
            <span className="mono text-[9px] text-[#a1a1aa]">
              {tools.length} draft tool{tools.length === 1 ? "" : "s"}
            </span>
            <button
              disabled={!agentEnabled || judging !== null}
              onClick={() => void onJudge(module)}
              title={
                agentEnabled
                  ? "run the AI judge on this draft adapter and merge it if it passes"
                  : "requires a Token Factory key (degraded mode)"
              }
              className="ml-auto shrink-0 rounded-md bg-[#15803d] px-2 py-0.5 text-[9.5px] font-medium text-white hover:bg-[#166534] disabled:opacity-40"
            >
              {judging === module ? "judging…" : "judge & merge →"}
            </button>
          </div>
          <div className="mono mt-1 text-[9.5px] leading-relaxed text-[#71717a]">{tools.join(", ")}</div>
        </div>
      ))}
    </div>
  );
}

/** Judge-blocked modules grouped by adapter: the failed terminal state.
 *  The operator edits the adapter file, then re-runs the judge (E-6). */
function BlockedList({ blocked, agentEnabled }: { blocked: DraftTool[]; agentEnabled: boolean }) {
  const [judging, setJudging] = useState<string | null>(null);
  if (blocked.length === 0) return <Empty text="no blocked adapters" />;

  const byModule = new Map<string, string[]>();
  for (const d of blocked) {
    const list = byModule.get(d.module) ?? [];
    list.push(d.name);
    byModule.set(d.module, list);
  }

  const onRejudge = async (module: string) => {
    setJudging(module);
    try {
      const r = await api.judgeAdapter(module);
      if (r.state === "merged") toast.success(`${module}: ${r.tools.length} tool(s) judged, merged and live`);
      else if (r.state === "blocked") toast.error(`${module}: judge blocked the adapter again — see the verdict findings`);
      else toast.warning(`${module}: judge ${r.state}`);
    } catch (e) {
      toast.error(`${module}: judge failed — ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setJudging(null);
    }
  };

  return (
    <div className="space-y-1.5">
      {[...byModule.entries()].map(([module, tools]) => (
        <div key={module} className="rounded-md border border-[#fecaca] bg-[#fef2f2]/60 px-2.5 py-1.5">
          <div className="flex items-center gap-2">
            <span className="mono text-[10px] font-semibold text-[#b91c1c]">{module}</span>
            <span className="mono text-[9px] text-[#a1a1aa]">
              {tools.length} blocked tool{tools.length === 1 ? "" : "s"} — never registered
            </span>
            <button
              disabled={!agentEnabled || judging !== null}
              onClick={() => void onRejudge(module)}
              title={
                agentEnabled
                  ? "re-run the AI judge after editing the adapter file"
                  : "requires a Token Factory key (degraded mode)"
              }
              className="ml-auto shrink-0 rounded-md border border-[#fecaca] bg-white px-2 py-0.5 text-[9.5px] font-medium text-[#b91c1c] hover:bg-[#fef2f2] disabled:opacity-40"
            >
              {judging === module ? "judging…" : "re-judge →"}
            </button>
          </div>
          <div className="mono mt-1 text-[9.5px] leading-relaxed text-[#71717a]">{tools.join(", ")}</div>
        </div>
      ))}
    </div>
  );
}

export function Governance() {
  const govQ = useApi(api.governance, 10000);
  const gov = govQ.data;
  const [clearing, setClearing] = useState(false);

  const onClearDrafts = async () => {
    if (!gov || gov.drafts.length === 0) return;
    const modules = [...new Set(gov.drafts.map((d) => d.module))].join(", ");
    if (!window.confirm(`Discard all ${gov.drafts.length} draft tool(s) from: ${modules}?\n\nUnmerged adapter files are deleted; this cannot be undone.`)) {
      return;
    }
    setClearing(true);
    try {
      const r = await api.clearDrafts();
      toast.success(
        `drafts cleared — ${r.tools_cleared} tool(s) discarded` +
          (r.files_deleted.length ? `, removed: ${r.files_deleted.join(", ")}` : ""),
      );
    } catch (e) {
      toast.error(`clear drafts failed — ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setClearing(false);
    }
  };

  return (
    <section className="flex h-full min-w-0 flex-1 flex-col bg-[#fafafa]">
      <div className="flex items-center gap-2 border-b border-[#e4e4e7] bg-white px-3 py-2">
        <div className="text-[12px] font-semibold">Governance</div>
        <span className="mono rounded bg-[#f4f4f5] px-1.5 py-0.5 text-[10px] text-[#71717a]">read-only</span>
        {gov && !gov.agent_features_enabled && (
          <span className="rounded border border-[#fde68a] bg-[#fffbeb] px-1.5 py-0.5 text-[10px] font-medium text-[#b45309]">
            agent features disabled — drafts and quarantine only
          </span>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-3 scroll-thin">
        {govQ.loading && <div className="py-10 text-center text-[11px] text-[#a1a1aa]">loading governance…</div>}
        {govQ.error && !govQ.loading && (
          <div className="py-10 text-center text-[11px] text-[#b91c1c]">failed to load governance: {govQ.error}</div>
        )}
        {gov && (
          <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
            <Card
              title={`drafts (${gov.drafts.length})`}
              action={
                gov.drafts.length > 0 ? (
                  <button
                    disabled={clearing}
                    onClick={() => void onClearDrafts()}
                    title="discard every draft tool and delete unmerged adapter files"
                    className="rounded-md border border-[#fecaca] bg-white px-2 py-0.5 text-[9.5px] font-medium text-[#b91c1c] hover:bg-[#fef2f2] disabled:opacity-40"
                  >
                    {clearing ? "clearing…" : "clear drafts"}
                  </button>
                ) : undefined
              }
            >
              <DraftList drafts={gov.drafts} agentEnabled={gov.agent_features_enabled} />
            </Card>

            <Card title={`blocked (${gov.blocked.length})`}>
              <BlockedList blocked={gov.blocked} agentEnabled={gov.agent_features_enabled} />
            </Card>

            <Card title={`quarantined (${gov.quarantined.length})`}>
              {gov.quarantined.length === 0 ? (
                <Empty text="no quarantined tools" />
              ) : (
                <div className="flex flex-wrap gap-1.5">
                  {gov.quarantined.map((n) => (
                    <span key={n} className="mono rounded border border-[#fecaca] bg-[#fef2f2] px-1.5 py-0.5 text-[10px] text-[#b91c1c]">
                      {n}
                    </span>
                  ))}
                </div>
              )}
            </Card>

            <Card title={`judge verdicts (${gov.verdicts.length})`}>
              <VerdictList events={gov.verdicts} />
            </Card>

            <Card title={`rejections (${gov.rejections.length})`}>
              <RejectionList events={gov.rejections} />
            </Card>

            <Card title={`conversion runs (${gov.conversion_runs.length})`}>
              {gov.conversion_runs.length === 0 ? (
                <Empty text="no conversion runs yet" />
              ) : (
                <div className="space-y-1.5">
                  {gov.conversion_runs.map((r) => (
                    <div key={r.id} className="rounded-md border border-[#f4f4f5] bg-white px-2.5 py-1.5">
                      <div className="flex items-center gap-2">
                        <span className="mono text-[10px] font-semibold">#{r.id}</span>
                        <span className="mono text-[10px] text-[#3f3f46]">{r.package}</span>
                        <span
                          className={cn(
                            "ml-auto rounded px-1.5 py-px text-[9px] font-semibold uppercase",
                            r.outcome === "merged"
                              ? "bg-[#f0fdf4] text-[#15803d]"
                              : r.outcome === "rejected"
                                ? "bg-[#fef2f2] text-[#b91c1c]"
                                : "bg-[#f4f4f5] text-[#52525b]",
                          )}
                        >
                          {r.outcome ?? "running"}
                        </span>
                      </div>
                      <div className="mono mt-0.5 text-[9px] text-[#a1a1aa]">
                        {fmtDateTime(r.started_ts)}
                        {r.fix_cycles != null && ` · fixes ${r.fix_cycles}`}
                        {r.judge_cycles != null && ` · judge ${r.judge_cycles}`}
                        {r.reason_code && ` · ${r.reason_code}`}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Card>

            <Card title={`repair incidents (${gov.repair_incidents.length})`}>
              {gov.repair_incidents.length === 0 ? (
                <Empty text="no repair incidents" />
              ) : (
                <div className="space-y-1.5">
                  {gov.repair_incidents.map((i) => (
                    <div key={i.id} className="flex items-center gap-2 rounded-md border border-[#f4f4f5] bg-white px-2.5 py-1.5">
                      <span className="mono text-[10px] font-medium">{i.tool}</span>
                      <span className="mono rounded bg-[#f4f4f5] px-1 py-px text-[9px] text-[#52525b]">{i.error_class}</span>
                      <span
                        className={cn(
                          "rounded px-1 py-px text-[9px] font-semibold uppercase",
                          i.state === "healed"
                            ? "bg-[#f0fdf4] text-[#15803d]"
                            : i.state === "quarantined"
                              ? "bg-[#fef2f2] text-[#b91c1c]"
                              : "bg-[#fffbeb] text-[#b45309]",
                        )}
                      >
                        {i.state}
                      </span>
                      <span className="mono ml-auto text-[9px] text-[#a1a1aa]">
                        {i.attempts} attempt{i.attempts === 1 ? "" : "s"} · {fmtTs(i.window_start_ts)}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          </div>
        )}
      </div>
    </section>
  );
}
