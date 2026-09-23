import { AlertTriangle, CheckCircle2, Clock3, FlaskConical } from "lucide-react";
import { decisionReason } from "./dataFieldState";
import { capitalDecisionHeadline, capitalDecisionState, type CapitalDecisionProps } from "./capitalDecisionModel";

export { capitalDecisionHeadline } from "./capitalDecisionModel";
export type { CapitalDecisionProps } from "./capitalDecisionModel";

const STATES = {
  actionable: { Icon: CheckCircle2, className: "border-emerald-500/40 bg-emerald-500/5", text: "text-emerald-700 dark:text-emerald-300" },
  failed: { Icon: AlertTriangle, className: "border-destructive/40 bg-destructive/5", text: "text-destructive" },
  qualifying: { Icon: FlaskConical, className: "border-blue-500/30 bg-blue-500/5", text: "text-blue-700 dark:text-blue-300" },
  waiting: { Icon: Clock3, className: "border-border bg-muted/20", text: "text-foreground" },
};

export function CapitalDecision(props: CapitalDecisionProps) {
  const { plan, resolution } = props;
  const state = capitalDecisionState(props), presentation = STATES[state], Icon = presentation.Icon;
  const reason = plan ? plan.primary_blocker : resolution?.primary_blocker;
  const rationale = plan ? plan.rationale : resolution?.rationale;
  const next = plan ? plan.next_action : resolution?.next_action;
  const message = reason ? decisionReason(reason) : rationale ? decisionReason(rationale) : null;
  const explanation = rationale ? decisionReason(rationale) : null;
  return <div className={`rounded-lg border p-3 text-sm ${presentation.className}`} aria-label="Capital decision">
    <p className={`flex items-center gap-2 font-semibold ${presentation.text}`}><Icon className="size-4 shrink-0" aria-hidden="true" />{capitalDecisionHeadline(props)}</p>
    {message ? <p className="mt-2 line-clamp-2">{message}</p> : state !== "actionable" ? <p className="mt-2 text-xs text-muted-foreground">No new order is authorized.</p> : null}
    {state === "actionable" ? <p className="mt-2 text-xs text-muted-foreground">Fill-time quote and risk checks still apply.</p> : null}
    {next || explanation ? <details className="mt-2 text-xs"><summary className="cursor-pointer text-muted-foreground">Evidence and next step</summary>{explanation ? <p className="mt-2">{explanation}</p> : null}{reason && explanation !== decisionReason(reason) ? <p className="mt-2">{decisionReason(reason)}</p> : null}{next ? <p className="mt-2">{decisionReason(next)}</p> : null}</details> : null}
    {state === "failed" ? <p className="mt-2 text-xs"><a className="font-medium underline" href="/health">Inspect decision service</a> · refresh_decision_models</p> : null}
  </div>;
}
