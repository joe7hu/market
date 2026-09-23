import type { components } from "@/generated/apiSchema";

type Plan = Pick<components["schemas"]["TodayTradePlanSummaryResponse"], "eligibility" | "authorization_mode"> &
  Partial<Pick<components["schemas"]["TodayTradePlanSummaryResponse"], "action" | "rationale" | "primary_blocker" | "next_action">>;
type Resolution = Partial<components["schemas"]["TodayResolutionSummaryResponse"]>;
export type CapitalDecisionProps = { plan?: Plan | null; resolution?: Resolution | null; missingIsFailure?: boolean };

export function capitalDecisionState({ plan, resolution, missingIsFailure }: CapitalDecisionProps): "actionable" | "failed" | "qualifying" | "waiting" {
  if (!plan && missingIsFailure) return "failed";
  if (plan?.eligibility === "ACTIONABLE") {
    return ["PAPER", "ADVISORY"].includes(plan.authorization_mode) && Boolean(plan.action)
      && !["NO_TRADE", "AVOID"].includes(plan.action ?? "") && !plan.primary_blocker ? "actionable" : "failed";
  }
  const reason = plan ? plan.primary_blocker : resolution?.primary_blocker;
  return reason === "alpha_strategy_revision_missing" || reason === "repeated_control_observations_unavailable" ? "qualifying" : "waiting";
}

// Display backend authority, never infer a trade from price conditions or ranks.
export function capitalDecisionHeadline(props: CapitalDecisionProps): string {
  const { plan, resolution, missingIsFailure } = props;
  if (!plan && missingIsFailure) return "TRADING PAUSED — decision publication failed";
  const state = capitalDecisionState(props);
  if (state === "failed") return "TRADING PAUSED — inconsistent trade authorization";
  if (state === "actionable") return `${plan!.authorization_mode} — ${plan!.action!.replaceAll("_", " ")}`;
  if (state === "qualifying") return "WAIT — strategy qualification";
  const action = plan ? plan.action : resolution?.action;
  if (action === "WAIT_FOR_PRICE") return "WAIT — entry condition not met";
  if (plan?.eligibility === "BLOCKED") return "NO TRADE — requirements not met";
  if (action === "NO_TRADE" || action === "AVOID") return "NO TRADE — no qualified entry";
  return "WAIT — no executable plan";
}
