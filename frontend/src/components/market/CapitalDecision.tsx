import type { components } from "@/generated/apiSchema";
import { decisionReason } from "./dataFieldState";

type Plan = Pick<components["schemas"]["TodayTradePlanSummaryResponse"], "eligibility" | "authorization_mode"> &
  Partial<Pick<components["schemas"]["TodayTradePlanSummaryResponse"], "action" | "rationale" | "primary_blocker" | "next_action">>;
type Resolution = Partial<components["schemas"]["TodayResolutionSummaryResponse"]>;
export type CapitalDecisionProps = { plan?: Plan | null; resolution?: Resolution | null; missingIsFailure?: boolean };

// Display the backend's verdict, never infer authorization from price conditions.
export function capitalDecisionHeadline({ plan, missingIsFailure }: CapitalDecisionProps): string {
  if (!plan && missingIsFailure) return "TRADING PAUSED — decision publication failed";
  if (plan?.eligibility === "ACTIONABLE") return `${plan.authorization_mode === "PAPER" ? "PAPER" : "ADVISORY"} — ${plan.action || "qualified trade"}`;
  return "WAIT — retain cash for this decision";
}

export function CapitalDecision(props: CapitalDecisionProps) {
  const { plan, resolution, missingIsFailure } = props;
  const failed = !plan && missingIsFailure;
  const reason = plan ? plan.primary_blocker : resolution?.primary_blocker;
  const rationale = plan ? plan.rationale : resolution?.rationale;
  const next = plan ? plan.next_action : resolution?.next_action;
  return <div className={`rounded-md border p-3 text-sm ${failed ? "border-destructive/40 bg-destructive/5" : "border-border bg-muted/20"}`} aria-label="Capital decision">
    <p className="font-semibold">{capitalDecisionHeadline(props)}</p>
    {rationale ? <p className="mt-2">{decisionReason(rationale)}</p> : !failed ? <p className="mt-2">No allocation is authorized by these measured price conditions.</p> : null}
    {plan?.eligibility === "ACTIONABLE" ? <p className="mt-2">{plan.authorization_mode === "PAPER" ? "Paper-qualified terms published" : "Advisory terms published"}; fill-time risk and quote checks still apply.</p> : null}
    {reason ? <p className="mt-2 text-xs"><strong>{failed ? "Failed dependency:" : "Decision constraint:"}</strong> {decisionReason(reason)}</p> : null}
    {next ? <p className="mt-2"><strong>Next system action:</strong> {decisionReason(next)}</p> : null}
    {failed ? <p className="mt-2"><strong>Owner:</strong> refresh_decision_models · <a className="underline" href="/health">Inspect publication failure</a></p> : null}
  </div>;
}
