import type { components } from "@/generated/apiSchema";

type DataFieldState = components["schemas"]["DataFieldStateV1"];

export function missingFieldState({
  field,
  source,
  reason,
  blocking = true,
  nextAction,
  availabilityStatus = "missing",
}: {
  field: string;
  source: string;
  reason: string;
  blocking?: boolean;
  nextAction: string;
  availabilityStatus?: DataFieldState["availability_status"];
}): DataFieldState {
  return { field, source, reason, blocking, next_action: nextAction, availability_status: availabilityStatus };
}

export function DataFieldStateNotice({ state, compact = false }: { state: DataFieldState; compact?: boolean }) {
  if (state.availability_status === "available" || state.availability_status === "not_applicable") return null;
  return (
    <div className={compact ? "text-xs text-muted-foreground" : "rounded-md border border-amber-300 bg-amber-50/40 p-3 text-sm"}>
      <p>{decisionReason(state.reason)}</p>
      {state.blocking ? <p className="mt-1">A new trade cannot be assessed until this is resolved.</p> : null}
      <p className="mt-1 text-muted-foreground">{decisionReason(state.next_action)}</p>
    </div>
  );
}

const REASONS: Record<string, string> = {
  "Refresh the required fact and recalculate the resolution.": "Open the ticker assessment, resolve its named input blocker, then refresh decision models. Do not stage a paper order before that check passes.",
  cash_comparator: "No new trade is selected. Review the investment case and required evidence.",
  forecast_missing: "A supported return forecast is not available.",
  alpha_strategy_revision_missing: "The investment signal has not passed strategy validation.",
  trade_plan_missing: "No published entry, position size, invalidation and maximum loss are linked to this decision.",
  trade_plan_expired: "This plan's entry window has ended. Refresh the ticker decision; the old terms cannot authorize a paper order.",
  trade_plan_cutoff_in_future: "This plan uses a future input cutoff. Check source and server clocks in Health before recalculating it.",
  "Refresh the ticker decision and publish its canonical TradePlan.": "Open the ticker assessment; refresh decision models to publish entry, position size, invalidation and maximum loss before staging paper orders.",
  "Refresh the ticker decision and trade plan.": "Open the ticker assessment; refresh decision models to publish entry, position size, invalidation and maximum loss before staging paper orders.",
  trade_plan_identity_mismatch: "The trade plan and decision refer to different evidence. Reload the ticker before acting.",
  risk_policy_blocked: "The proposed trade does not meet the portfolio risk limits.",
  selected_expression_missing: "No investment structure has been selected.",
  marginal_risk_missing: "The change in portfolio risk has not been calculated.",
  risk_budget_consumed_missing: "The required risk budget has not been calculated.",
  cash_hurdle_missing: "The required return above cash has not been set.",
  scenario_evidence_missing: "There is not enough observed data to assess portfolio stress losses.",
  insufficient_history: "There is not enough price history for this calculation.",
  max_loss_required: "A defined maximum loss is required before this can be considered.",
  wait_for_price_discovery_and_complete_liquidity_inputs: "Wait for price discovery and complete liquidity inputs before considering a trade.",
};

/** Translate diagnostics at the display boundary; stored evidence stays intact. */
export function decisionReason(reason: string | null | undefined): string {
  if (!reason) return "No next step was recorded. Open the ticker assessment to check its input and publication status.";
  if (REASONS[reason]) return REASONS[reason];
  if (/^[a-z0-9]+(?:_[a-z0-9]+)+$/i.test(reason)) {
    if (/mismatch|conflict/.test(reason)) return `${reason.replaceAll("_", " ")}. Refresh the linked decision and plan together before staging paper orders.`;
    if (/stale|expired/.test(reason)) return `${reason.replaceAll("_", " ")}. Refresh that source and recalculate the ticker decision before staging paper orders.`;
    if (/missing|unavailable|incomplete/.test(reason)) return `${reason.replaceAll("_", " ")}. Supply this input and recalculate the ticker decision before staging paper orders.`;
    return reason.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
  }
  return reason
    .replace(/^Cash is selected because the current opportunity rank is unavailable:\s*/i, "")
    .replace(/\b[a-z][a-z0-9-]*[.:]v\d+:[a-z0-9:-]+\b/gi, "linked evidence")
    .replace(/\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi, "linked evidence")
    .replace(/\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b/g, (code) => REASONS[code] ?? code.replaceAll("_", " "))
    .replace(/canonical TradePlan|canonical trade plan|immutable plan/gi, "trade plan")
    .replace(/canonical ticker decision/gi, "ticker decision")
    .replace(/publish its /gi, "update its ")
    .replace(/\.{2,}/g, ".");
}
