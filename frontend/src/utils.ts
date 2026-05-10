import type { JsonValue, RowRecord } from "./types";

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
    return value.length ? value.map(displayValue).join(", ") : "-";
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return value;
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
