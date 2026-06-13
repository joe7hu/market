// Pure row/record access + grouping helpers extracted from optionsRadar.tsx.
// Leaf functions over RowRecord / JsonValue with no JSX or component deps —
// the shared data plumbing the radar's components build on.

import type { JsonValue, RowRecord } from "@/types";

import { textField } from "./rowFormat";
import { dateMillis, validationMillis } from "./optionsRadarFormat";

export function recordField(row: RowRecord | undefined, key: string): Record<string, JsonValue> | undefined {
  const value = row?.[key];
  if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, JsonValue>;
  return undefined;
}

export function jsonRecord(value: JsonValue | undefined): Record<string, JsonValue> | undefined {
  if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, JsonValue>;
  return undefined;
}

export function jsonArrayField(row: RowRecord | undefined, key: string): JsonValue[] {
  const value = row?.[key];
  if (Array.isArray(value)) return value;
  if (typeof value === "string" && value.trim().startsWith("[")) {
    try {
      const parsed = JSON.parse(value) as JsonValue;
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }
  return [];
}

export function arrayText(row: RowRecord | undefined, key: string): string[] {
  return jsonArrayField(row, key).map((item) => typeof item === "string" || typeof item === "number" ? String(item) : "").filter(Boolean);
}

export function listFromRecord(record: Record<string, JsonValue> | undefined, key: string): string[] {
  const value = record?.[key];
  if (!Array.isArray(value)) return [];
  return value.map((item) => typeof item === "string" || typeof item === "number" ? String(item) : "").filter(Boolean);
}

export function numberFromRecord(record: Record<string, JsonValue> | undefined, key: string): number {
  const value = record?.[key];
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return Number.NaN;
}

export function stringFromRecord(record: Record<string, JsonValue> | undefined, key: string, fallback = ""): string {
  const value = record?.[key];
  if (typeof value === "string" && value.trim()) return value.trim();
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return fallback;
}

export function boolFromRecord(record: Record<string, JsonValue> | undefined, key: string): boolean {
  return record?.[key] === true;
}

export function mapBy(items: RowRecord[], key: string): Map<string, RowRecord> {
  const map = new Map<string, RowRecord>();
  for (const item of items) {
    const value = textField(item, [key]);
    if (value) map.set(value, item);
  }
  return map;
}

export function latestBy(items: RowRecord[], key: string, dateKey: string): Map<string, RowRecord> {
  const map = new Map<string, RowRecord>();
  for (const item of items) {
    const value = textField(item, [key]);
    if (!value) continue;
    const current = map.get(value);
    if (!current || dateMillis(textField(item, [dateKey])) >= dateMillis(textField(current, [dateKey]))) {
      map.set(value, item);
    }
  }
  return map;
}

export function latestValidationBy(items: RowRecord[], key: string): Map<string, RowRecord> {
  const history = validationHistoryBy(items, key);
  const latest = new Map<string, RowRecord>();
  for (const [value, rows] of history.entries()) {
    if (rows[0]) latest.set(value, rows[0]);
  }
  return latest;
}

export function validationHistoryBy(items: RowRecord[], key: string): Map<string, RowRecord[]> {
  const history = new Map<string, RowRecord[]>();
  for (const item of items) {
    const value = textField(item, [key]);
    if (!value) continue;
    const rows = history.get(value) ?? [];
    rows.push(item);
    history.set(value, rows);
  }
  for (const rows of history.values()) {
    rows.sort((left, right) => validationMillis(right) - validationMillis(left));
  }
  return history;
}
