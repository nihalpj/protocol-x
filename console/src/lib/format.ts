export function fmtTime(d: Date | string | number): string {
  const dt = typeof d === "object" ? d : new Date(d);
  return dt.toLocaleTimeString("en-GB", { hour12: false });
}

/** Format an epoch timestamp that may be in seconds (Python time.time()) or ms. */
export function fmtTs(ts: number): string {
  return fmtTime(ts < 1e12 ? ts * 1000 : ts);
}

/** Epoch seconds/ms → "13 Oct, 14:32:05". */
export function fmtDateTime(ts: number): string {
  const dt = new Date(ts < 1e12 ? ts * 1000 : ts);
  const date = dt.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
  return `${date}, ${fmtTime(dt)}`;
}

export function fmtUptimeS(s: number): string {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m > 0) return `${m}m ${String(sec).padStart(2, "0")}s`;
  return `${sec}s`;
}

export function payloadOf(e: { payload: unknown }): Record<string, unknown> {
  return typeof e.payload === "object" && e.payload !== null
    ? (e.payload as Record<string, unknown>)
    : {};
}

export function fmtClock(d: Date): string {
  return d.toLocaleTimeString("en-GB", { hour12: false });
}

export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

export const STATE_DOT: Record<string, string> = {
  live: "#16a34a",
  draft: "#d97706",
  quarantined: "#dc2626",
};

export const STATE_BADGE: Record<string, string> = {
  live: "bg-[#f0fdf4] text-[#15803d]",
  draft: "bg-[#fffbeb] text-[#b45309]",
  quarantined: "bg-[#fef2f2] text-[#b91c1c]",
};

export const LEVEL_COLOR: Record<string, string> = {
  info: "#71717a",
  ok: "#16a34a",
  warn: "#d97706",
  error: "#dc2626",
};
