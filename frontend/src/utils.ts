import type { JsonValue, RowRecord } from "./types";

const MAX_TEXT_LENGTH = 160;
const MAX_ARRAY_ITEMS = 4;
const EMPTY_TEXT = "-";
const SYMBOL_KEYS = ["ticker", "symbol", "security", "name"];
const SOURCE_KEYS = ["source", "provider", "capability", "source_url"];

export type RowGroup = {
  symbol: string;
  source: string;
  rows: RowRecord[];
};

export function rows(payload: { rows?: RowRecord[] } | undefined): RowRecord[] {
  return Array.isArray(payload?.rows) ? payload.rows : [];
}

export function displayValue(value: JsonValue | undefined): string {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  if (typeof value === "number") {
    return Number.isInteger(value) ? value.toLocaleString() : value.toLocaleString(undefined, { maximumFractionDigits: 4 });
  }
  if (typeof value === "boolean") {
    return value ? "Yes" : "No";
  }
  if (Array.isArray(value)) {
    if (!value.length) {
      return "-";
    }
    if (value.every((item) => typeof item !== "object" || item === null)) {
      const preview = value.slice(0, MAX_ARRAY_ITEMS).map(displayValue).join(", ");
      return value.length > MAX_ARRAY_ITEMS ? `${preview} +${value.length - MAX_ARRAY_ITEMS} more` : preview;
    }
    return `${value.length.toLocaleString()} ${value.length === 1 ? "item" : "items"}`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value).filter(([, entryValue]) => entryValue !== null && entryValue !== undefined && entryValue !== "");
    if (!entries.length) {
      return "-";
    }
    const preferredKeys = ["verdict", "stage", "grade", "status", "method", "score", "price", "fair_value", "upside_pct", "source"];
    const summary = preferredKeys
      .filter((key) => value[key] !== undefined && value[key] !== null && value[key] !== "")
      .slice(0, 3)
      .map((key) => `${titleize(key)} ${displayValue(value[key])}`);

    if (summary.length) {
      return truncate(summary.join(" | "));
    }
    return `${entries.length.toLocaleString()} ${entries.length === 1 ? "field" : "fields"}`;
  }
  const normalized = value.replace(/\s+/g, " ").trim();
  const parsed = parseStructuredText(normalized);
  return parsed === undefined ? truncate(normalized) : displayValue(parsed);
}

export function fullDisplayValue(value: JsonValue | undefined): string {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  if (typeof value === "string") {
    return value.replace(/\s+/g, " ").trim();
  }
  return displayValue(value);
}

export function normalizeRows(sourceRows: RowRecord[] | undefined): RowRecord[] {
  return Array.isArray(sourceRows) ? sourceRows.map(normalizeRow) : [];
}

export function normalizeRow(row: RowRecord | undefined): RowRecord {
  if (!row) {
    return {};
  }
  return Object.fromEntries(
    Object.entries(row)
      .map(([key, value]) => [key, normalizeJsonValue(value)] as const)
      .filter(([, value]) => value !== undefined),
  );
}

export function textFallback(row: RowRecord | undefined, keys: string[], fallback = EMPTY_TEXT): string {
  const value = firstPresent(row, keys);
  return value || fallback;
}

export function numberValue(value: JsonValue | undefined, fallback = 0): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value !== "string") {
    return fallback;
  }
  const parsed = Number(value.trim().replace(/[$,%_,]/g, ""));
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function numberFromRow(row: RowRecord | undefined, keys: string[], fallback = 0): number {
  if (!row) {
    return fallback;
  }
  for (const key of keys) {
    const parsed = numberValue(row[key], Number.NaN);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  return fallback;
}

export function percentValue(value: JsonValue | undefined, fallback = 0): number {
  const parsed = numberValue(value, Number.NaN);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  if (typeof value === "string" && value.includes("%")) {
    return parsed;
  }
  return Math.abs(parsed) > 0 && Math.abs(parsed) <= 1 ? parsed * 100 : parsed;
}

export function percentFromRow(row: RowRecord | undefined, keys: string[], fallback = 0): number {
  if (!row) {
    return fallback;
  }
  for (const key of keys) {
    const parsed = percentValue(row[key], Number.NaN);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  return fallback;
}

function truncate(value: string): string {
  if (value.length <= MAX_TEXT_LENGTH) {
    return value;
  }
  return `${value.slice(0, MAX_TEXT_LENGTH - 1)}...`;
}

function parseStructuredText(value: string): JsonValue | undefined {
  if (!value.startsWith("{") && !value.startsWith("[")) {
    return undefined;
  }
  try {
    const parsed = JSON.parse(value) as JsonValue;
    return typeof parsed === "object" && parsed !== null ? parsed : undefined;
  } catch {
    return undefined;
  }
}

export function firstPresent(row: RowRecord | undefined, keys: string[]): string {
  if (!row) {
    return "";
  }
  for (const key of keys) {
    const value = row[key];
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
    if (typeof value === "number") {
      return String(value);
    }
  }
  return "";
}

export function symbolFromRow(row: RowRecord | undefined): string {
  return tickerSymbolFromRow(row);
}

export function tickerSymbol(value: JsonValue | undefined): string {
  if (typeof value !== "string" && typeof value !== "number") {
    return "";
  }
  const raw = String(value).trim();
  if (!raw) {
    return "";
  }
  const candidate = raw
    .split(/[,\s;]/)[0]
    .split(":")
    .at(-1)
    ?.replace(/^\$/, "")
    .replace(/[^A-Za-z0-9.-]/g, "")
    .toUpperCase() ?? "";
  return /^[A-Z0-9][A-Z0-9.-]{0,14}$/.test(candidate) ? candidate : "";
}

export function tickerSymbolFromRow(row: RowRecord | undefined, keys = SYMBOL_KEYS): string {
  if (!row) {
    return "";
  }
  for (const key of keys) {
    const symbol = tickerSymbol(row[key]);
    if (symbol) {
      return symbol;
    }
  }
  return "";
}

export function groupRowsBySymbolSource(sourceRows: RowRecord[] | undefined): RowGroup[] {
  const grouped = new Map<string, RowGroup>();
  for (const row of normalizeRows(sourceRows)) {
    const symbol = tickerSymbolFromRow(row) || "MARKET";
    const source = textFallback(row, SOURCE_KEYS, "local");
    const key = `${symbol}\u0000${source}`;
    const existing = grouped.get(key);
    if (existing) {
      existing.rows.push(row);
    } else {
      grouped.set(key, { symbol, source, rows: [row] });
    }
  }
  return Array.from(grouped.values());
}

export function titleize(value: string): string {
  return value
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function collectColumns(rows: RowRecord[], preferred: string[]): string[] {
  const seen = new Set<string>();
  const columns: string[] = [];
  for (const key of preferred) {
    if (rows.some((row) => row[key] !== undefined)) {
      seen.add(key);
      columns.push(key);
    }
  }
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (!seen.has(key)) {
        seen.add(key);
        columns.push(key);
      }
    }
  }
  return columns.slice(0, 10);
}

export function getMetric(metrics: Record<string, number> | undefined, key: string, fallback: number): number {
  const value = metrics?.[key];
  return typeof value === "number" ? value : fallback;
}

function normalizeJsonValue(value: JsonValue | undefined): JsonValue | undefined {
  if (value === undefined) {
    return undefined;
  }
  if (typeof value === "string") {
    const trimmed = value.replace(/\s+/g, " ").trim();
    return trimmed ? trimmed : undefined;
  }
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : undefined;
  }
  if (Array.isArray(value)) {
    return value.map(normalizeJsonValue).filter((item): item is JsonValue => item !== undefined);
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value)
        .map(([key, entryValue]) => [key, normalizeJsonValue(entryValue)] as const)
        .filter(([, entryValue]) => entryValue !== undefined),
    ) as { [key: string]: JsonValue };
  }
  return value;
}
