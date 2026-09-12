import { evidenceLabel, fieldLabel, humanize, statusMessage } from "./labels";

export function evidenceReason(value: unknown): string {
  const raw = String(value ?? "");
  if (raw === "") return "Evidence unavailable";
  return statusMessage(raw) === humanize(raw) ? evidenceLabel(raw) : statusMessage(raw);
}

export function evidenceReasons(values: unknown): string[] {
  if (!Array.isArray(values)) return [];
  return values.map(evidenceReason).filter(Boolean);
}

export function safeRecord(value: unknown): Record<string, any> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, any> : {};
}

export function technicalFieldNames(value: unknown): string[] {
  return Object.keys(safeRecord(value)).map(fieldLabel);
}

export function humanEvidenceSummary(value: unknown, fallback = "No stored evidence is available."): string {
  if (value == null || value === "") return fallback;
  if (Array.isArray(value)) return value.length ? `${value.length} stored record${value.length === 1 ? "" : "s"}` : fallback;
  if (typeof value === "object") {
    const count = Object.keys(value).length;
    return count ? `${count} supporting detail${count === 1 ? "" : "s"} retained` : fallback;
  }
  return String(value);
}
