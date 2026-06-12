import type { JsonValue, RowRecord } from "@/types";
import { toneFromOperationalStatus, type Tone } from "@/ui/tone";
import { displayValue, fullDisplayValue, tickerSymbol } from "@/utils";

export type { Tone };

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
  const operationalTone = toneFromOperationalStatus(value);
  if (operationalTone) return operationalTone;

  const normalized = value.toLowerCase().replace(/[_-]+/g, " ");
  const tokens = new Set(normalized.match(/[a-z0-9]+/g) ?? []);
  if (hasAny(tokens, ["breach", "critical", "bad", "sell", "avoid"])) return "bad";
  if (hasAny(tokens, ["warn", "warning", "stale", "blocked", "review", "risk", "low"])) return "warn";
  if (hasAny(tokens, ["good", "ready", "clear", "ok", "high", "strong"])) return "good";
  if (hasAny(tokens, ["none"])) return "muted";
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

function hasAny(tokens: Set<string>, values: string[]): boolean {
  return values.some((value) => tokens.has(value));
}
