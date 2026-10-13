// Typed fetch client for the Protocol-X Tool MCP server REST API.
// Same-origin: the Python server serves this SPA from console/dist and the
// /api/* routes, so all calls are relative.

import { useEffect, useRef, useState } from "react";

// ---------- response types ----------

export interface SizingInfo {
  tier: string;
  cpus: number;
  memory_mb: number;
  worker_pool: number;
  per_call_rss_mb: number;
  warnings: string[];
  overrides: Record<string, string>;
}

export interface ServerStatus {
  status: string;
  version: string;
  tools: number;
  preset: string;
  sizing: SizingInfo;
  agent_features_enabled: boolean;
  judge_model: string;
  fast_model: string;
  tf_endpoint: string;
  workspace: string;
  host: string;
  port: number;
}

export type ToolState = "live" | "draft" | "quarantined";
export type ToolSource = "built-in" | "generated" | "manual";

export interface ToolRow {
  name: string;
  description: string;
  schema: Record<string, unknown>;
  adapter_module: string;
  backend: string;
  network: boolean;
  timeout_s: number;
  limits: Record<string, unknown>;
  state: ToolState;
  version_hash: string | null;
  source: ToolSource | null;
}

export interface LogLine {
  ts: number;
  tool_call_id: string;
  tool: string;
  duration_ms: number;
  outcome: string;
  exit_code: number;
  result_size: number;
  error_code?: string;
}

export interface ConfigValue<T> {
  value: T;
  source: string;
}

export interface ConsoleConfig {
  preset: ConfigValue<string>;
  limits: {
    cpu_s: ConfigValue<number>;
    rss_mb: ConfigValue<number>;
    fs_mb: ConfigValue<number>;
  };
  network: ConfigValue<string>;
  allowed_hosts: ConfigValue<string[]>;
  rate_limit_per_min: ConfigValue<number>;
  models: {
    endpoint: ConfigValue<string>;
    judge: ConfigValue<string>;
    fast: ConfigValue<string>;
  };
  agent_features_enabled: boolean;
  sizing_overrides: Record<string, string>;
}

export interface ConfigUpdate {
  tf_endpoint?: string;
  tf_judge_model?: string;
  tf_fast_model?: string;
  preset?: string;
}

export interface ConfigUpdateResult {
  ok: boolean;
  applied: string[];
  persisted_to: string;
}

export interface AuditEvent {
  id: number;
  ts: number;
  kind: string;
  actor: string;
  payload: Record<string, unknown>;
}

export interface ConversionRun {
  id: number;
  package: string;
  workdir: string;
  trace_path: string;
  started_ts: number;
  ended_ts: number | null;
  outcome: string | null;
  reason_code: string | null;
  fix_cycles: number | null;
  judge_cycles: number | null;
}

export interface RepairIncident {
  id: number;
  tool: string;
  error_class: string;
  input_json: string | null;
  adapter_version_id: number | null;
  attempts: number;
  state: string;
  window_start_ts: number;
}

export interface Governance {
  drafts: string[];
  quarantined: string[];
  verdicts: AuditEvent[];
  rejections: AuditEvent[];
  conversion_runs: ConversionRun[];
  repair_incidents: RepairIncident[];
  agent_features_enabled: boolean;
}

export interface PypiOk {
  state: "ok";
  name: string;
  version: string;
  summary: string | null;
  license: string | null;
  requires_python: string | null;
  home_page: string | null;
}

export type PypiResult =
  | PypiOk
  | { state: "not-found"; message: string }
  | { state: "offline"; message: string };

export interface GenericHttpSnippet {
  url: string;
  transport: string;
  headers: Record<string, string>;
}

// ---------- agent builder (F-019 / UF-01) ----------

export interface IntrospectFunction {
  name: string;
  signature: string;
  doc: string;
}

export type IntrospectResult =
  | { state: "ok"; package: string; functions: IntrospectFunction[] }
  | { state: "not-installed"; message: string };

export interface GenerateResult {
  state: string; // generated | generation-failed
  module?: string;
  path?: string;
  tools?: string[];
  diagnostics?: string[];
}

export interface JudgeVerdictInfo {
  verdict: string;
  findings: string[];
  model: string;
}

export interface JudgeResult {
  state: string; // merged | blocked | judge-unavailable | generation-failed
  tools: string[];
  diagnostics: string[];
  verdict: JudgeVerdictInfo | null;
}

export interface Snippets {
  claude_desktop: Record<string, unknown>;
  generic_http: GenericHttpSnippet;
}

// ---------- client ----------

const TOKEN_KEY = "protox.token";

// Browsers cannot set Authorization on navigation, so the console is opened
// as /?token=... once; the token is moved to localStorage and stripped from
// the URL, then sent as a Bearer header on every /api call.
function bootstrapToken(): string {
  const params = new URLSearchParams(window.location.search);
  const fromUrl = params.get("token");
  if (fromUrl) {
    localStorage.setItem(TOKEN_KEY, fromUrl);
    params.delete("token");
    const rest = params.toString();
    window.history.replaceState(null, "", window.location.pathname + (rest ? `?${rest}` : ""));
    return fromUrl;
  }
  return localStorage.getItem(TOKEN_KEY) ?? "";
}

const bearerToken = bootstrapToken();

export class ApiError extends Error {
  status: number;
  body: unknown;

  constructor(status: number, body: unknown) {
    const message =
      typeof body === "object" && body !== null && "error" in body
        ? String((body as Record<string, unknown>).error)
        : `request failed (${status})`;
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(bearerToken ? { Authorization: `Bearer ${bearerToken}` } : {}),
      ...(init?.headers ?? {}),
    },
  });
  const body: unknown = await resp.json().catch(() => null);
  if (!resp.ok) throw new ApiError(resp.status, body);
  return body as T;
}

function messageOf(body: unknown, fallback: string): string {
  if (typeof body === "object" && body !== null && "message" in body) {
    const m = (body as Record<string, unknown>).message;
    if (typeof m === "string" && m) return m;
  }
  return fallback;
}

export const api = {
  status: () => request<ServerStatus>("/api/status"),
  tools: () => request<{ tools: ToolRow[] }>("/api/tools"),
  logs: (limit = 200) => request<{ logs: LogLine[] }>(`/api/logs?limit=${limit}`),
  config: () => request<ConsoleConfig>("/api/config"),
  updateConfig: (update: ConfigUpdate) =>
    request<ConfigUpdateResult>("/api/config", {
      method: "POST",
      body: JSON.stringify(update),
    }),
  governance: () => request<Governance>("/api/governance"),
  snippets: () => request<Snippets>("/api/snippets"),
  pypi: async (name: string): Promise<PypiResult> => {
    try {
      return await request<PypiOk>(`/api/pypi/${encodeURIComponent(name)}`);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        return { state: "not-found", message: messageOf(e.body, `package '${name}' was not found on PyPI`) };
      }
      if (e instanceof ApiError && e.status === 502) {
        return { state: "offline", message: messageOf(e.body, "PyPI is unreachable — check the network and retry") };
      }
      return {
        state: "offline",
        message: e instanceof Error ? e.message : "request failed — retry",
      };
    }
  },
  introspect: async (pkg: string): Promise<IntrospectResult> => {
    try {
      const r = await request<{ package: string; functions: IntrospectFunction[] }>(
        `/api/introspect/${encodeURIComponent(pkg)}`
      );
      return { state: "ok", ...r };
    } catch (e) {
      if (e instanceof ApiError && e.status === 400) {
        return {
          state: "not-installed",
          message: messageOf(e.body, `package '${pkg}' is not installed in the server environment`),
        };
      }
      throw e;
    }
  },
  generateAdapter: (pkg: string, functions: string[]) =>
    request<GenerateResult>("/api/builder/generate", {
      method: "POST",
      body: JSON.stringify({ package: pkg, functions }),
    }),
  judgeAdapter: (module: string) =>
    request<JudgeResult>("/api/builder/judge", {
      method: "POST",
      body: JSON.stringify({ module }),
    }),
};

// ---------- polling hook ----------

interface ApiState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
}

/** Fetch immediately, then re-fetch every intervalMs (null = fetch once). */
export function useApi<T>(fn: () => Promise<T>, intervalMs: number | null): ApiState<T> {
  const [state, setState] = useState<ApiState<T>>({ data: null, error: null, loading: true });
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const data = await fnRef.current();
        if (!cancelled) setState({ data, error: null, loading: false });
      } catch (e) {
        if (!cancelled) {
          setState((s) => ({ ...s, error: e instanceof Error ? e.message : String(e), loading: false }));
        }
      }
    };
    void tick();
    if (intervalMs == null) return;
    const timer = setInterval(tick, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [intervalMs]);

  return state;
}
