import type { JsonValue, RowRecord } from "@/types";
import { displayValue, fullDisplayValue, tickerSymbol } from "@/utils";

export type Tone = "good" | "warn" | "bad" | "info" | "muted";

export function textField(row: RowRecord | undefined, keys: string[], fallback = ""): string {
  if (!row) return fallback;
  for (const key of keys) {
    const value = row[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
    if (typeof value === "boolean") return value ? "Yes" : "No";
  }
  return fallback;
}

export function numberField(row: RowRecord | undefined, keys: string[], fallback = 0): number {
  if (!row) return fallback;
  for (const key of keys) {
    const value = row[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string") {
      const parsed = Number(value.trim().replace(/[$,%_,]/g, ""));
      if (Number.isFinite(parsed)) return parsed;
    }
  }
  return fallback;
}

export function booleanField(row: RowRecord | undefined, keys: string[]): boolean {
  if (!row) return false;
  for (const key of keys) {
    const value = row[key];
    if (typeof value === "boolean") return value;
    if (typeof value === "number") return value !== 0;
    if (typeof value === "string") {
      const normalized = value.trim().toLowerCase();
      if (["true", "yes", "y", "1"].includes(normalized)) return true;
      if (["false", "no", "n", "0", ""].includes(normalized)) return false;
    }
  }
  return false;
}

export function listField(row: RowRecord | undefined, keys: string[]): string[] {
  if (!row) return [];
  for (const key of keys) {
    const value = row[key];
    const list = toList(value);
    if (list.length) return list;
  }
  return [];
}

export function symbolList(row: RowRecord | undefined): string[] {
  const direct = listField(row, ["symbols", "tickers"]);
  const candidates = direct.length ? direct : [textField(row, ["symbol", "ticker", "security"])];
  return [...new Set(candidates.map((value) => tickerSymbol(value)).filter(Boolean))];
}

export function displayField(row: RowRecord | undefined, keys: string[], fallback = "-"): string {
  if (!row) return fallback;
  for (const key of keys) {
    const value = row[key];
    if (value !== undefined && value !== null && value !== "") return displayValue(value);
  }
  return fallback;
}

export function fullField(row: RowRecord | undefined, keys: string[], fallback = "-"): string {
  if (!row) return fallback;
  for (const key of keys) {
    const value = row[key];
    if (value !== undefined && value !== null && value !== "") return fullDisplayValue(value);
  }
  return fallback;
}

export function titleLabel(value: string): string {
  return value.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function toneFromText(value: string): Tone {
  const normalized = value.toLowerCase();
  if (normalized.includes("breach") || normalized.includes("critical") || normalized.includes("bad") || normalized.includes("sell") || normalized.includes("avoid")) return "bad";
  if (normalized.includes("warn") || normalized.includes("stale") || normalized.includes("blocked") || normalized.includes("review") || normalized.includes("risk")) return "warn";
  if (normalized.includes("good") || normalized.includes("ready") || normalized.includes("clear") || normalized.includes("ok")) return "good";
  if (normalized.includes("none") || normalized.includes("missing")) return "muted";
  return "info";
}

export function formatMoney(value: number): string {
  if (!Number.isFinite(value)) return "-";
  return value.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: Math.abs(value) > 1000 ? 0 : 2 });
}

export function formatPct(value: number): string {
  if (!Number.isFinite(value)) return "-";
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function toList(value: JsonValue | undefined): string[] {
  if (value === undefined || value === null || value === "") return [];
  if (Array.isArray(value)) {
    return value.map((item) => typeof item === "string" || typeof item === "number" ? String(item).trim() : displayValue(item)).filter(Boolean);
  }
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return [];
    if (trimmed.startsWith("[") || trimmed.startsWith("{")) {
      try {
        const parsed = JSON.parse(trimmed) as JsonValue;
        return toList(parsed);
      } catch {
        return [trimmed];
      }
    }
    return trimmed.split(/[;|]/).map((item) => item.trim()).filter(Boolean);
  }
  if (typeof value === "number" || typeof value === "boolean") return [String(value)];
  return [displayValue(value)];
}
