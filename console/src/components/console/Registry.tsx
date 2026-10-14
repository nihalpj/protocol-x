import { useEffect, useMemo, useState } from "react";
import { api, useApi } from "@/api";
import type { SearchHit } from "@/api";
import { cn, STATE_DOT, STATE_BADGE } from "@/lib/format";
import type { ToolRow } from "@/lib/types";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

interface Props {
  onOpenAdd: () => void;
}

function limitChips(limits: Record<string, unknown>): Array<[string, string]> {
  return Object.entries(limits).map(([k, v]) => [k, typeof v === "string" ? v : JSON.stringify(v)]);
}

function ToolCard({ tool, open, onToggle, score }: { tool: ToolRow; open: boolean; onToggle: () => void; score?: number }) {
  const limits = limitChips(tool.limits);
  return (
    <div className="overflow-hidden rounded-lg border border-[#e4e4e7] bg-white">
      <button onClick={onToggle} className="flex w-full items-center gap-2.5 px-3 py-2 text-left hover:bg-[#fafafa]">
        <span className="text-[9px] text-[#a1a1aa]">{open ? "▾" : "▸"}</span>
        <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: STATE_DOT[tool.state] ?? "#a1a1aa" }} />
        <span className="mono shrink-0 text-[11px] font-medium text-[#18181b]">{tool.name}</span>
        <span className="min-w-0 flex-1 truncate text-[10.5px] text-[#71717a]">{tool.description}</span>
        {score !== undefined && (
          <span className="mono shrink-0 rounded bg-[#eef2ff] px-1.5 py-px text-[8.5px] font-semibold text-[#5e6ad2]">
            semantic {score.toFixed(2)}
          </span>
        )}
        <span className={cn("shrink-0 rounded px-1.5 py-px text-[8.5px] font-semibold uppercase", STATE_BADGE[tool.state])}>
          {tool.state}
        </span>
        <span className="mono shrink-0 text-[9.5px] text-[#a1a1aa]">{tool.timeout_s}s</span>
      </button>

      {open && (
        <div className="space-y-3 border-t border-[#f4f4f5] px-3 py-2.5">
          <p className="text-[11px] leading-relaxed text-[#3f3f46]">{tool.description || "No description."}</p>

          <div className="flex flex-wrap gap-1">
            <span className="mono rounded border border-[#e4e4e7] bg-[#fafafa] px-1.5 py-0.5 text-[9.5px] text-[#52525b]">
              adapter {tool.adapter_module}
            </span>
            <span className="mono rounded border border-[#e4e4e7] bg-[#fafafa] px-1.5 py-0.5 text-[9.5px] text-[#52525b]">
              {tool.backend} backend
            </span>
            <span
              className={cn(
                "mono rounded border px-1.5 py-0.5 text-[9.5px]",
                tool.network
                  ? "border-[#fde68a] bg-[#fffbeb] text-[#b45309]"
                  : "border-[#e4e4e7] bg-[#fafafa] text-[#52525b]",
              )}
            >
              {tool.network ? "network egress" : "network off"}
            </span>
            <span className="mono rounded border border-[#e4e4e7] bg-[#fafafa] px-1.5 py-0.5 text-[9.5px] text-[#52525b]">
              timeout {tool.timeout_s}s
            </span>
            {tool.source && (
              <span className="mono rounded border border-[#e4e4e7] bg-[#fafafa] px-1.5 py-0.5 text-[9.5px] text-[#52525b]">
                source: {tool.source}
              </span>
            )}
            {tool.version_hash && (
              <span className="mono rounded border border-[#e4e4e7] bg-[#fafafa] px-1.5 py-0.5 text-[9.5px] text-[#52525b]">
                v{tool.version_hash}
              </span>
            )}
            {limits.length === 0 && (
              <span className="mono rounded border border-[#e4e4e7] bg-[#fafafa] px-1.5 py-0.5 text-[9.5px] text-[#52525b]">
                default limits
              </span>
            )}
            {limits.map(([k, v]) => (
              <span
                key={k}
                className="mono rounded border border-[#e4e4e7] bg-[#fafafa] px-1.5 py-0.5 text-[9.5px] text-[#52525b]"
              >
                {k}: {v}
              </span>
            ))}
          </div>

          <div>
            <div className="mb-1 text-[9.5px] font-semibold uppercase tracking-[0.08em] text-[#a1a1aa]">
              input schema (JSON)
            </div>
            <pre className="mono max-h-64 overflow-auto rounded-md border border-[#e4e4e7] bg-[#fafafa] p-2.5 text-[10px] leading-relaxed text-[#3f3f46] scroll-thin">
              {JSON.stringify(tool.schema, null, 2)}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}

export function Registry({ onOpenAdd }: Props) {
  const toolsQ = useApi(api.tools, 5000);
  const [query, setQuery] = useState("");
  const [adapterFilter, setAdapterFilter] = useState<string>("all");
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [semantic, setSemantic] = useState<{ hits: SearchHit[]; backend: string } | null>(null);

  const tools = useMemo(() => toolsQ.data?.tools ?? [], [toolsQ.data]);

  // F-040: debounce a semantic search once the query is specific enough;
  // any failure falls back silently to the client-side substring filter.
  useEffect(() => {
    const q = query.trim();
    if (q.length < 3) {
      setSemantic(null);
      return;
    }
    const timer = setTimeout(() => {
      api
        .search(q)
        .then((r) => setSemantic({ hits: r.results, backend: r.backend }))
        .catch(() => setSemantic(null));
    }, 300);
    return () => clearTimeout(timer);
  }, [query]);

  const semanticRows = useMemo(() => {
    if (!semantic) return null;
    const byName = new Map(tools.map((t) => [t.name, t]));
    return semantic.hits
      .map((h) => ({ row: byName.get(h.name), score: h.score }))
      .filter((x): x is { row: ToolRow; score: number } => x.row !== undefined)
      .filter((x) => adapterFilter === "all" || x.row.adapter_module === adapterFilter);
  }, [semantic, tools, adapterFilter]);

  const adapters = useMemo(() => {
    const set = new Set(tools.map((t) => t.adapter_module));
    return [...set].sort();
  }, [tools]);

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    const rows = tools.filter(
      (t) =>
        (adapterFilter === "all" || t.adapter_module === adapterFilter) &&
        (!q || t.name.toLowerCase().includes(q) || t.description.toLowerCase().includes(q)),
    );
    const byAdapter = new Map<string, ToolRow[]>();
    for (const t of rows) {
      const list = byAdapter.get(t.adapter_module) ?? [];
      list.push(t);
      byAdapter.set(t.adapter_module, list);
    }
    return [...byAdapter.entries()].map(([adapter, list]) => ({ adapter, tools: list }));
  }, [tools, query, adapterFilter]);

  const live = tools.filter((t) => t.state === "live").length;

  return (
    <section className="flex h-full min-w-0 flex-1 flex-col bg-[#fafafa]">
      <div className="flex items-center gap-2 border-b border-[#e4e4e7] bg-white px-3 py-2">
        <div className="text-[12px] font-semibold">Tool registry</div>
        <span className="mono rounded bg-[#f4f4f5] px-1.5 py-0.5 text-[10px] text-[#71717a]">
          {live}/{tools.length} live
        </span>
        <div className="ml-auto flex items-center gap-2">
          <Select value={adapterFilter} onValueChange={setAdapterFilter}>
            <SelectTrigger className="h-7 w-44 border-[#e4e4e7] text-[11px]">
              <SelectValue placeholder="all adapters" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all" className="text-[11px]">
                all adapters
              </SelectItem>
              {adapters.map((a) => (
                <SelectItem key={a} value={a} className="mono text-[11px]">
                  {a}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="search name / description…"
            className="w-52 rounded-md border border-[#e4e4e7] bg-[#fafafa] px-2.5 py-1.5 text-[11px] outline-none transition-colors focus:border-[#5e6ad2] focus:bg-white"
          />
          <button
            onClick={onOpenAdd}
            className="rounded-md bg-[#5e6ad2] px-2.5 py-1.5 text-[11px] font-medium text-white transition-colors hover:bg-[#4f5ac0]"
          >
            + Add package
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-3 scroll-thin">
        {toolsQ.loading && <div className="py-10 text-center text-[11px] text-[#a1a1aa]">loading registry…</div>}
        {toolsQ.error && !toolsQ.loading && (
          <div className="py-10 text-center text-[11px] text-[#b91c1c]">failed to load registry: {toolsQ.error}</div>
        )}
        {!toolsQ.loading && !toolsQ.error && semanticRows && (
          <div className="mb-3">
            <div className="mb-1.5 flex items-center gap-2 px-1">
              <span className="mono text-[11px] font-semibold text-[#3f3f46]">semantic results</span>
              <span className="mono text-[9.5px] text-[#a1a1aa]">{semanticRows.length}</span>
              {semantic && (
                <span className="mono rounded bg-[#eef2ff] px-1.5 py-px text-[8.5px] font-semibold uppercase text-[#5e6ad2]">
                  {semantic.backend}
                </span>
              )}
            </div>
            <div className="space-y-1.5">
              {semanticRows.map(({ row, score }) => (
                <ToolCard
                  key={row.name}
                  tool={row}
                  score={score}
                  open={!!open[row.name]}
                  onToggle={() => setOpen((o) => ({ ...o, [row.name]: !o[row.name] }))}
                />
              ))}
            </div>
          </div>
        )}
        {!semanticRows &&
          groups.map(({ adapter, tools: list }) => (
          <div key={adapter} className="mb-3">
            <div className="mb-1.5 flex items-center gap-2 px-1">
              <span className="mono text-[11px] font-semibold text-[#3f3f46]">{adapter}</span>
              <span className="mono text-[9.5px] text-[#a1a1aa]">{list.length}</span>
            </div>
            <div className="space-y-1.5">
              {list.map((t) => (
                <ToolCard
                  key={t.name}
                  tool={t}
                  open={!!open[t.name]}
                  onToggle={() => setOpen((o) => ({ ...o, [t.name]: !o[t.name] }))}
                />
              ))}
            </div>
          </div>
        ))}
        {!toolsQ.loading && !toolsQ.error && !semanticRows && groups.length === 0 && (
          <div className="py-10 text-center text-[11px] text-[#a1a1aa]">no tools match the current filters</div>
        )}
        {semanticRows && semanticRows.length === 0 && (
          <div className="py-10 text-center text-[11px] text-[#a1a1aa]">no semantic matches — try different words</div>
        )}
      </div>
    </section>
  );
}
