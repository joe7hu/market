import type { RowRecord } from "@/types";

export type OptionDecision = {
  key: string;
  symbol: string;
  structure: string;
  entryPrice: number;
  effectiveAssignmentPrice: number;
  maxLoss: number;
  securedCash: number;
  expectedValue: number;
  probabilityAssignment: number;
  action: string;
  summary: string;
  cashSecured: boolean;
};

export function adaptOptionDecision(row: RowRecord): OptionDecision {
  const symbol = text(row, "symbol", "ticker");
  const rawStructure = text(row, "structure") || "option";
  return {
    key: text(row, "stable_key", "decision_id") || `${symbol}-${rawStructure}`,
    symbol,
    structure: rawStructure.replaceAll("_", " "),
    entryPrice: number(row, "entry_price"),
    effectiveAssignmentPrice: number(row, "effective_assignment_price"),
    maxLoss: number(row, "max_loss"),
    securedCash: number(row, "secured_cash"),
    expectedValue: number(row, "expected_value"),
    probabilityAssignment: number(row, "probability_assignment", 0),
    action: text(row, "action") || "setup",
    summary: text(row, "summary") || "Review entry, risk, and invalidation in Options Radar.",
    cashSecured: rawStructure === "cash_secured_put",
  };
}

function text(row: RowRecord, ...keys: string[]): string {
  for (const key of keys) {
    const value = row[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
  }
  return "";
}

function number(row: RowRecord, key: string, fallback = Number.NaN): number {
  const value = row[key];
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}
