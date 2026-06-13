import { toneFromOperationalStatus, type Tone } from "@/ui/tone";
import { toneFromText } from "@/views/rowFormat";

export function freshnessTone(freshnessStatus: string, status: string): Tone {
  const fresh = freshnessStatus.toLowerCase();
  if (fresh === "failed") return "bad";
  if (fresh === "stale") return "warn";
  if (fresh === "documentation" || fresh === "not_applicable") return "muted";
  const statusTone = toneFromOperationalStatus(status);
  if (statusTone) return statusTone;
  if (fresh === "fresh") return "good";
  return toneFromText(freshnessStatus || status);
}

/** Collapse per-symbol probes ("yahoo-chart:KNOX.V", "yfinance:ZENA") to their base provider. */
export function baseProvider(provider: string): string {
  const trimmed = provider.trim();
  if (!trimmed) return "unknown";
  const colon = trimmed.indexOf(":");
  return colon > 0 ? trimmed.slice(0, colon) : trimmed;
}

/** Normalized key for matching directory names to freshness providers (space/underscore/case-insensitive). */
export function normKey(value: string): string {
  return value.toLowerCase().replace(/[\s_-]+/g, "");
}

export function statusLabel(tone: Tone): string {
  return tone === "good" ? "Healthy" : tone === "warn" ? "Degraded" : tone === "bad" ? "Failed" : tone === "info" ? "Active" : "Idle";
}

export function booleanValue(value: unknown): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") return ["true", "yes", "1", "followed", "enabled"].includes(value.trim().toLowerCase());
  return false;
}

export function truncate(value: string, max = 180): string {
  const clean = value.trim().replace(/\s+/g, " ");
  return clean.length > max ? `${clean.slice(0, max)}…` : clean;
}

export function dateMs(value: string | undefined | null): number {
  if (!value) return 0;
  const timestamp = new Date(value).getTime();
  return Number.isFinite(timestamp) ? timestamp : 0;
}

export function jsonRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

export function numberFromJson(value: unknown, fallback: number): number {
  const parsed = typeof value === "number" ? value : typeof value === "string" ? Number(value) : Number.NaN;
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function booleanFromJson(value: unknown, fallback: boolean): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") return ["1", "true", "yes", "on", "enabled", "active"].includes(value.trim().toLowerCase());
  return fallback;
}

export function stringFromJson(value: unknown, fallback: string): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

export function formatDateTime(value: string | undefined | null): string {
  if (!value || value === "-") return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}
