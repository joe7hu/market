const LABELS: Record<string, string> = {
  active: "Active",
  awaiting_human_review: "Ready for human review",
  candidate: "Being tested",
  cancelled: "Cancelled",
  collecting_outcomes: "Collecting evidence",
  complete: "Verified",
  disabled: "Paused",
  drafted: "Drafted",
  evidence_validation_failed: "Evidence problem",
  excluded: "Excluded",
  fail: "Blocked",
  forward_test: "Forward test",
  insufficient_evidence: "Needs more evidence",
  matched_outcomes: "Comparable completed cases",
  matched_outcomes_below_promotion_floor: "Collecting evidence",
  monitoring: "Monitoring",
  no_continuous_advisor_run: "Waiting for first advisor cycle",
  no_paper_fills: "Waiting for first paper fill",
  no_data: "No data yet",
  partial: "Some evidence missing",
  pass: "Pass",
  pending: "Pending",
  promoted: "Promoted",
  quarantined: "Held for evidence review",
  rejected: "Rejected",
  resolved: "Resolved",
  shadow: "Forward test",
  staged: "Staged",
  unresolvable: "Cannot resolve yet",
  unavailable: "Unavailable",
  unknown: "Unknown",
  valid: "Verified",
  waiting: "Waiting for outcome",
  strategy_mutation_proposal: "Proposed strategy improvement",
  matched_outcomes_below_forward_floor: "Collecting forward evidence",
};

const FIELD_LABELS: Record<string, string> = {
  quality_status: "Evidence quality",
  reconciliation_status: "P&L verification",
  authority_group: "Decision authority",
  semantic_effective_hash: "Template fingerprint",
  input_hash: "Input fingerprint",
  gate_code: "Promotion check",
  calculation_version: "Calculation version",
  scoring_version: "Scoring version",
  evidence_class: "Evidence class",
  candidate_prompt_version: "Prompt being tested",
  active_prompt_version: "Active prompt",
  matched_outcomes: "Comparable completed cases",
  permitted_automatic_action: "Automatic action",
  strategy_revision: "Strategy revision",
  strategy_key: "Strategy",
  paper_order_count: "Paper orders",
};

const GATE_LABELS: Record<string, string> = {
  pit_integrity: "Point-in-time integrity",
  denominator_completeness: "Evidence completeness",
  oos_predictive_validity: "Out-of-sample validity",
  falsification_and_robustness: "Robustness checks",
  economic_promotability: "Economic usefulness",
};

export function humanize(value: unknown, fallback = "—"): string {
  if (value == null || value === "") return fallback;
  const raw = String(value);
  return LABELS[raw] ?? raw.replaceAll("_", " ").replaceAll("-", " ").replace(/\s+/g, " ").replace(/(^|\s)\S/g, (letter) => letter.toUpperCase());
}

export function fieldLabel(value: string): string {
  return FIELD_LABELS[value] ?? humanize(value);
}

export function gateLabel(value: unknown): string {
  const raw = String(value ?? "");
  return GATE_LABELS[raw] ?? humanize(value, "Promotion check");
}

export function statusLabel(value: unknown, fallback = "Unknown"): string {
  return humanize(value, fallback);
}

export function lifecycleLabel(value: unknown): string {
  return humanize(value, "Not staged");
}

export function evidenceLabel(value: unknown): string {
  return humanize(value, "Evidence unavailable");
}

export function statusTone(value: unknown): "good" | "warn" | "bad" | "info" | "muted" {
  const raw = String(value ?? "").toLowerCase();
  if (["complete", "valid", "verified", "active", "promoted", "pass", "resolved"].includes(raw)) return "good";
  if (["fail", "rejected", "invalid", "evidence_validation_failed", "quarantined"].includes(raw)) return "bad";
  if (["partial", "unavailable", "pending", "waiting", "collecting_outcomes", "candidate", "shadow", "staged"].includes(raw)) return "warn";
  if (["disabled", "unknown", "no_data"].includes(raw)) return "muted";
  return "info";
}

export function statusMessage(value: unknown): string {
  const raw = String(value ?? "");
  const messages: Record<string, string> = {
    matched_outcomes_below_promotion_floor: "Collecting evidence before this change can be promoted.",
    evidence_validation_failed: "One or more predictions could not be verified from stored source data.",
    opening_capital_unavailable: "Opening capital is not authoritative, so normalized return is unavailable.",
    verified_current_mark_unavailable: "Current marks are missing or stale, so open P&L cannot be verified.",
    no_paper_fills: "A paper-ready decision must be filled before performance learning begins.",
    no_continuous_advisor_run: "The advisor has not completed its first evidence-backed cycle yet.",
    strategy_history_page_bounded: "Older strategy history is available from the detail view.",
  };
  return messages[raw] ?? humanize(value, "No explanation recorded.");
}

export function money(value: unknown, signed = false): string {
  if (value == null || value === "") return "—";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  const formatted = Math.abs(numeric).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (!signed) return `${numeric < 0 ? "-" : ""}$${formatted}`;
  return `${numeric > 0 ? "+" : numeric < 0 ? "-" : ""}$${formatted}`;
}

export function percent(value: unknown, signed = false): string {
  if (value == null || value === "") return "—";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  const formatted = `${Math.abs(numeric * 100).toFixed(1)}%`;
  if (!signed) return `${numeric < 0 ? "-" : ""}${formatted}`;
  return `${numeric > 0 ? "+" : numeric < 0 ? "-" : ""}${formatted}`;
}

export function dateTime(value: unknown, fallback = "—"): string {
  if (!value) return fallback;
  const parsed = new Date(String(value));
  return Number.isNaN(parsed.getTime()) ? fallback : parsed.toLocaleString();
}

export function shortDate(value: unknown, fallback = "—"): string {
  if (!value) return fallback;
  const parsed = new Date(String(value));
  return Number.isNaN(parsed.getTime()) ? fallback : parsed.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function numberValue(value: unknown): number | null {
  if (value == null || value === "") return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}
