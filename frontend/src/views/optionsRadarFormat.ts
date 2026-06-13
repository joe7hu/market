// Pure number/date formatting helpers extracted from optionsRadar.tsx. These
// are leaf functions (no JSX, no component deps) shared across the radar's
// components; keeping them here shrinks the view file and gives future
// component splits a small, stable import surface.

import type { RowRecord } from "@/types";
import type { Tone } from "@/ui/tone";

import { formatMoney, numberField, textField } from "./rowFormat";

export function moneyField(row: RowRecord | undefined, keys: string[]): string {
  return formatMoney(numberField(row, keys, Number.NaN));
}

export function formatRatio(value: number): string {
  if (!Number.isFinite(value)) return "-";
  return `${(value * 100).toFixed(Math.abs(value) >= 10 ? 0 : 1)}%`;
}

export function formatSignedRatio(value: number): string {
  if (!Number.isFinite(value)) return "-";
  const pct = value * 100;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(Math.abs(pct) >= 100 ? 0 : 1)}%`;
}

export function formatMultiple(value: number): string {
  if (!Number.isFinite(value)) return "-";
  return `${(value + 1).toFixed(value + 1 >= 10 ? 1 : 2)}x`;
}

export function formatScore(value: number): string {
  if (!Number.isFinite(value)) return "-";
  return value.toFixed(Math.abs(value) >= 10 ? 0 : 1);
}

export function formatNumber(value: number, digits: number): string {
  if (!Number.isFinite(value)) return "-";
  return value.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

export function formatDate(value: string): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

// The backend writes naive UTC ISO timestamps (datetime.utcnow().isoformat()),
// so append a Z when no timezone is present before measuring age.
export function parseUtcMillis(value: string): number {
  if (!value) return 0;
  const hasZone = /[zZ]|[+-]\d{2}:?\d{2}$/.test(value);
  const date = new Date(hasZone ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? 0 : date.getTime();
}

export function formatAge(minutes: number): string {
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

// Honest freshness state for the snapshot. Replaces a static "Hourly
// deterministic" badge that implied freshness even when data was days stale.
export function freshnessBadge(snapshotIso: string): { label: string; tone: Tone } {
  const ms = parseUtcMillis(snapshotIso);
  if (!ms) return { label: "No data", tone: "muted" };
  const minutes = Math.max(0, Math.round((Date.now() - ms) / 60000));
  if (minutes < 90) return { label: `Live · ${formatAge(minutes)} ago`, tone: "good" };
  if (minutes < 24 * 60) return { label: `${formatAge(minutes)} old`, tone: "warn" };
  return { label: `Stale · ${formatAge(minutes)} old`, tone: "bad" };
}

// Session-aware header badge. During RTH the freshness/age is what matters; when
// the market is closed we say so (and that we're frozen on the last regular-hours
// snapshot) instead of showing a misleading "Live"/"Stale" age.
export function sessionBadge(
  marketSession: string,
  frozen: boolean,
  snapshotIso: string,
): { label: string; tone: Tone } {
  if (marketSession === "closed") {
    const suffix = frozen && snapshotIso ? ` · last RTH ${formatDate(snapshotIso)}` : "";
    return { label: `Market closed${suffix}`, tone: "info" };
  }
  if (marketSession === "rth") {
    const fresh = freshnessBadge(snapshotIso);
    return { label: `RTH · ${fresh.label}`, tone: fresh.tone };
  }
  return freshnessBadge(snapshotIso); // session unknown — fall back to freshness
}

export function formatShortDate(value: string): string {
  if (!value) return "-";
  const date = new Date(/^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T12:00:00` : value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

export function dateMillis(value: string): number {
  if (!value) return 0;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 0 : date.getTime();
}

export function validationMillis(row: RowRecord): number {
  return dateMillis(textField(row, ["validated_at"])) || dateMillis(textField(row, ["validation_date"]));
}
