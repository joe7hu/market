import type { PanelData, RowRecord } from "@/types";
import { rows } from "@/utils";
import { booleanField, listField, numberField } from "@/views/rowFormat";

// Mirrors INVALIDATION_NEAR_PCT in core/thesis_monitor.py.
const INVALIDATION_NEAR_PCT = 10.0;

export type ThesisMonitorViewModel = {
  monitorRows: RowRecord[];
  thesisRows: RowRecord[];
  needsReview: RowRecord[];
  incomplete: RowRecord[];
  aging: RowRecord[];
  contradictions: RowRecord[];
  invalidationWatch: RowRecord[];
};

function isInvalidationWatch(row: RowRecord): boolean {
  const flags = listField(row, ["contradiction_flags"]).map((flag) => flag.toLowerCase());
  if (flags.some((flag) => flag.includes("invalidation_breached") || flag.includes("invalidation_near"))) {
    return true;
  }
  const distance = numberField(row, ["invalidation_distance_pct"], Number.NaN);
  return Number.isFinite(distance) && distance <= INVALIDATION_NEAR_PCT;
}

export function buildThesisMonitorViewModel(data: PanelData): ThesisMonitorViewModel {
  const monitorRows = rows(data.thesisMonitor);
  return {
    monitorRows,
    thesisRows: rows(data.theses),
    needsReview: monitorRows.filter((row) => booleanField(row, ["needs_review"])),
    // Incomplete: a structured field has never been authored.
    incomplete: monitorRows.filter((row) => listField(row, ["structured_fields_missing"]).length),
    // Aging: content exists but has not been reviewed inside the staleness window.
    aging: monitorRows.filter(
      (row) =>
        booleanField(row, ["stale_thesis"]) &&
        !listField(row, ["structured_fields_missing"]).length,
    ),
    contradictions: monitorRows.filter((row) => listField(row, ["contradiction_flags"]).length),
    invalidationWatch: monitorRows.filter(isInvalidationWatch),
  };
}
