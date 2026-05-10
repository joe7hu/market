import type { JsonValue, RowRecord } from "./types";

const MAX_TEXT_LENGTH = 160;
const MAX_ARRAY_ITEMS = 4;

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
  return firstPresent(row, ["ticker", "symbol", "security", "name"]).toUpperCase();
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
