import type { PanelData, RowRecord } from "@/types";
import { rows } from "@/utils";
import { booleanField, listField, numberField, textField } from "@/views/rowFormat";

export type ThesisMonitorViewModel = {
  monitorRows: RowRecord[];
  thesisRows: RowRecord[];
  needsReview: RowRecord[];
  stale: RowRecord[];
  contradictions: RowRecord[];
  invalidationWatch: RowRecord[];
};

export function buildThesisMonitorViewModel(data: PanelData): ThesisMonitorViewModel {
  const monitorRows = rows(data.thesisMonitor);
  return {
    monitorRows,
    thesisRows: rows(data.theses),
    needsReview: monitorRows.filter((row) => booleanField(row, ["needs_review"])),
    stale: monitorRows.filter((row) => booleanField(row, ["stale_thesis"])),
    contradictions: monitorRows.filter((row) => listField(row, ["contradiction_flags"]).length),
    invalidationWatch: monitorRows.filter((row) => Number.isFinite(numberField(row, ["invalidation_distance_pct"], Number.NaN)) || textField(row, ["invalidation_price"])),
  };
}
