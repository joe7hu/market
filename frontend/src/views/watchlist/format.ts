import type { WatchState } from "@/viewModels/watchlist";
import type { WatchlistRefreshStatus } from "./columns";

export const storageKey = "market.watchlist.localStates.v1";

export function readLocalStates(): Record<string, WatchState | undefined> {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(storageKey) ?? "{}") as Record<string, WatchState | undefined>;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

export function refreshStatusText(status: WatchlistRefreshStatus, finishedAt: Date | null, error: string | null): string {
  if (status === "starting") return "Starting background refresh";
  if (status === "running") return "Refreshing data in background";
  if (status === "failed") return error ? `Refresh failed: ${shorten(error)}` : "Refresh failed";
  if (finishedAt) return `Fresh at ${formatTimestamp(finishedAt)}`;
  return "No completed refresh yet";
}

export function formatTimestamp(value: Date): string {
  return value.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function shorten(value: string): string {
  return value.length > 72 ? `${value.slice(0, 69)}...` : value;
}

export function formatPrice(value: number): string {
  if (!Number.isFinite(value)) return "-";
  return value.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: value >= 100 ? 0 : 2 });
}

export function formatMarketCap(value: number): string {
  if (!Number.isFinite(value)) return "-";
  if (Math.abs(value) >= 1_000_000_000_000) return `$${(value / 1_000_000_000_000).toFixed(2)}T`;
  if (Math.abs(value) >= 1_000_000_000) return `$${(value / 1_000_000_000).toFixed(1)}B`;
  if (Math.abs(value) >= 1_000_000) return `$${(value / 1_000_000).toFixed(0)}M`;
  return `$${value.toLocaleString()}`;
}

export function formatMultiple(value: number, unavailable = "-"): string {
  if (!Number.isFinite(value)) return unavailable;
  if (Math.abs(value) >= 100) return value.toFixed(0);
  if (Math.abs(value) >= 10) return value.toFixed(1);
  return value.toFixed(2);
}

export function formatPercent(value: number, ratio: boolean): string {
  if (!Number.isFinite(value)) return "-";
  const pct = ratio ? value * 100 : value;
  return `${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%`;
}

export function returnTone(value: number): string {
  if (!Number.isFinite(value)) return "";
  if (value >= 0.5) return "bg-green-100 text-green-900";
  if (value >= 0) return "bg-green-50 text-green-800";
  if (value <= -0.2) return "bg-red-100 text-red-900";
  return "bg-red-50 text-red-800";
}

export function growthTone(value: number): string {
  if (!Number.isFinite(value)) return "";
  if (value >= 0.15) return "bg-green-100 text-green-900";
  if (value >= 0.03) return "bg-green-50 text-green-800";
  if (value >= 0) return "bg-amber-50 text-amber-900";
  if (value <= -0.1) return "bg-red-100 text-red-900";
  return "bg-red-50 text-red-800";
}

export function fcfYieldTone(value: number): string {
  if (!Number.isFinite(value)) return "";
  if (value >= 0.05) return "bg-green-100 text-green-900";
  if (value >= 0.02) return "bg-green-50 text-green-800";
  if (value >= 0) return "bg-amber-50 text-amber-900";
  return "bg-red-50 text-red-800";
}

export function fcfMarginTone(value: number): string {
  if (!Number.isFinite(value)) return "";
  if (value >= 0.2) return "bg-green-100 text-green-900";
  if (value >= 0.08) return "bg-green-50 text-green-800";
  if (value >= 0) return "bg-amber-50 text-amber-900";
  return "bg-red-50 text-red-800";
}

export function roicTone(value: number): string {
  if (!Number.isFinite(value)) return "text-muted-foreground";
  if (value >= 25) return "bg-green-100 text-green-900";
  if (value >= 15) return "bg-green-50 text-green-800";
  if (value < 5) return "bg-red-50 text-red-800";
  return "bg-amber-50 text-amber-900";
}

export function multipleTone(value: number, goodMax: number, warnMax: number): string {
  if (!Number.isFinite(value)) return "text-muted-foreground";
  if (value <= goodMax) return "bg-green-50 text-green-800";
  if (value <= warnMax) return "bg-amber-50 text-amber-900";
  return "bg-red-50 text-red-800";
}
