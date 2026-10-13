import { useState } from "react";
import { toast } from "sonner";
import { api, useApi, ApiError } from "@/api";
import { Switch } from "@/components/ui/switch";
import type { ConsoleConfig } from "@/lib/types";

interface Props {
  onClose: () => void;
}

function SettingsForm({ config }: { config: ConsoleConfig }) {
  const [endpoint, setEndpoint] = useState(config.models.endpoint.value);
  const [judgeModel, setJudgeModel] = useState(config.models.judge.value);
  const [fastModel, setFastModel] = useState(config.models.fast.value);
  const [logPayloads, setLogPayloads] = useState(config.log_payloads);
  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);

  const save = async () => {
    setSaving(true);
    try {
      const r = await api.updateConfig({
        tf_endpoint: endpoint,
        tf_judge_model: judgeModel,
        tf_fast_model: fastModel,
        log_payloads: logPayloads,
        // only send the key when a new one was entered — empty keeps the current key
        ...(apiKey ? { tf_api_key: apiKey } : {}),
      });
      setApiKey("");
      toast.success(`settings saved — applied: ${r.applied.join(", ")}`);
    } catch (e) {
      const detail = e instanceof ApiError ? e.message : e instanceof Error ? e.message : String(e);
      toast.error(`save failed — ${detail}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <div className="flex-1 space-y-4 overflow-y-auto px-4 py-3 scroll-thin">
        <div className="flex items-center gap-2 rounded-lg border border-[#e4e4e7] bg-[#fafafa] px-3 py-2 text-[10.5px]">
          <span className="text-[#71717a]">preset</span>
          <span className="mono font-semibold text-[#18181b]">{config.preset.value}</span>
          <span className="mono text-[9px] text-[#a1a1aa]">({config.preset.source})</span>
          <span className="ml-auto text-[#71717a]">rate limit</span>
          <span className="mono font-semibold text-[#18181b]">{config.rate_limit_per_min.value}/min</span>
        </div>

        <div>
          <div className="mb-1.5 text-[9.5px] font-semibold uppercase tracking-[0.08em] text-[#a1a1aa]">
            Token Factory
          </div>
          <label className="block">
            <div className="mb-1 text-[10.5px] text-[#71717a]">endpoint</div>
            <input
              value={endpoint}
              onChange={(e) => setEndpoint(e.target.value)}
              className="mono w-full rounded-md border border-[#e4e4e7] px-2.5 py-1.5 text-[11px] outline-none focus:border-[#5e6ad2]"
            />
          </label>
          <label className="mt-2 block">
            <div className="mb-1 text-[10.5px] text-[#71717a]">api key</div>
            <input
              type="password"
              value={apiKey}
              placeholder={config.api_key_set ? "••••••••••••••••" : ""}
              onChange={(e) => setApiKey(e.target.value)}
              autoComplete="new-password"
              className="mono w-full rounded-md border border-[#e4e4e7] px-2.5 py-1.5 text-[11px] outline-none focus:border-[#5e6ad2]"
            />
          </label>
          <div className="mt-1 text-[9.5px] text-[#a1a1aa]">
            {config.api_key_set
              ? "A key is set — enter a new key to replace it. Leave empty to keep the current key."
              : "no key set — paste a Token Factory key to enable agent features"}
          </div>
          <div className="mt-1 text-[9.5px] text-[#a1a1aa]">
            source: {config.models.endpoint.source} — the key is write-only: never displayed or
            echoed back by the API; workers stay keyless.
          </div>
        </div>

        <div>
          <div className="mb-1.5 text-[9.5px] font-semibold uppercase tracking-[0.08em] text-[#a1a1aa]">
            models
          </div>
          <label className="mb-2 block">
            <div className="mb-1 text-[10.5px] text-[#71717a]">judge model (governance gate)</div>
            <input
              value={judgeModel}
              onChange={(e) => setJudgeModel(e.target.value)}
              className="mono w-full rounded-md border border-[#e4e4e7] px-2.5 py-1.5 text-[11px] outline-none focus:border-[#5e6ad2]"
            />
          </label>
          <label className="block">
            <div className="mb-1 text-[10.5px] text-[#71717a]">fast model (converter loop)</div>
            <input
              value={fastModel}
              onChange={(e) => setFastModel(e.target.value)}
              className="mono w-full rounded-md border border-[#e4e4e7] px-2.5 py-1.5 text-[11px] outline-none focus:border-[#5e6ad2]"
            />
          </label>
          <div className="mt-1 text-[9.5px] text-[#a1a1aa]">
            {config.agent_features_enabled
              ? "agent features enabled — judge fail-closed on outage, nothing merges without a passing verdict"
              : "degraded mode — add a Token Factory key to enable package conversion"}
          </div>
        </div>

        <div>
          <div className="mb-1.5 text-[9.5px] font-semibold uppercase tracking-[0.08em] text-[#a1a1aa]">
            observability
          </div>
          <div className="flex items-center justify-between gap-3 rounded-md border border-[#e4e4e7] bg-[#fafafa] px-3 py-2">
            <div>
              <div className="text-[10.5px] text-[#71717a]">log request/response payloads</div>
              <div className="text-[9.5px] text-[#a1a1aa]">
                record tool-call arguments and results in tool-calls.jsonl — secret-scrubbed,
                capped at 4000 chars per field
              </div>
            </div>
            <Switch checked={logPayloads} onCheckedChange={setLogPayloads} aria-label="Log request/response payloads" />
          </div>
        </div>

        <div>
          <div className="mb-1.5 text-[9.5px] font-semibold uppercase tracking-[0.08em] text-[#a1a1aa]">
            effective limits
          </div>
          <div className="flex flex-wrap gap-1">
            {Object.entries(config.limits).map(([k, v]) => (
              <span
                key={k}
                className="mono rounded border border-[#e4e4e7] bg-[#fafafa] px-1.5 py-0.5 text-[9.5px] text-[#52525b]"
              >
                {k}: {v.value} <span className="text-[#a1a1aa]">({v.source})</span>
              </span>
            ))}
            <span className="mono rounded border border-[#e4e4e7] bg-[#fafafa] px-1.5 py-0.5 text-[9.5px] text-[#52525b]">
              network: {config.network.value}
            </span>
          </div>
        </div>
      </div>

      <div className="flex items-center justify-end border-t border-[#e4e4e7] px-4 py-2.5">
        <button
          disabled={saving}
          onClick={() => void save()}
          className="rounded-md bg-[#18181b] px-4 py-2 text-[11.5px] font-medium text-white hover:bg-[#27272a] disabled:opacity-40"
        >
          {saving ? "saving…" : "save"}
        </button>
      </div>
    </>
  );
}

export function SettingsModal({ onClose }: Props) {
  const configQ = useApi(api.config, null);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/25 p-6" onClick={onClose}>
      <div
        className="flex max-h-[86vh] w-[480px] flex-col overflow-hidden rounded-xl border border-[#e4e4e7] bg-white shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-[#e4e4e7] px-4 py-3">
          <div className="text-[13px] font-semibold">Settings</div>
          <button onClick={onClose} className="rounded p-1 text-[#a1a1aa] hover:bg-[#f4f4f5] hover:text-[#3f3f46]">
            ✕
          </button>
        </div>

        {configQ.loading && <div className="flex-1 px-4 py-8 text-center text-[11px] text-[#a1a1aa]">loading config…</div>}
        {configQ.error && !configQ.loading && (
          <div className="flex-1 px-4 py-8 text-center text-[11px] text-[#b91c1c]">
            failed to load config: {configQ.error}
          </div>
        )}
        {configQ.data && <SettingsForm config={configQ.data} />}
      </div>
    </div>
  );
}
