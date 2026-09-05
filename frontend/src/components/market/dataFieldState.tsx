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
  return (
    <div className={compact ? "text-xs text-muted-foreground" : "rounded-md border border-amber-300 bg-amber-50/40 p-3 text-sm"}>
      <p className="font-semibold text-foreground">Field unavailable: {state.field}</p>
      <p className="mt-1 text-muted-foreground">Status: {state.availability_status} · Source: {state.source}</p>
      <p className="mt-1 text-muted-foreground">Reason: {state.reason}</p>
      {state.owner ? <p className="mt-1 text-muted-foreground">Owner: {state.owner} · Impact: {state.impact ?? "unspecified"}</p> : null}
      <p className="mt-1 text-muted-foreground">{state.blocking ? "This blocks the decision." : "This does not block the decision."} Next: {state.next_action}</p>
    </div>
  );
}
