import { useEffect, useState } from "react";
import type { components } from "@/generated/apiSchema";
import { CapitalDecision, type CapitalDecisionProps } from "./CapitalDecision";
import { dateTime, money } from "@/presentation/labels";

type Signal = components["schemas"]["ReferenceSignal"];

const LABELS: Record<Signal["action"], string> = {
  BUY_SETUP: "Conditional long setup", EXIT_SETUP: "EXIT SETUP", HOLD: "HOLD",
  WAIT: "WAIT · no setup", AVOID: "AVOID · no new long", SERVICE_FAILURE: "SIGNAL SERVICE FAILED",
};

export function visibleReferenceSignal(signal: Signal | null | undefined, now: number): Signal | null {
  if (!signal) return null;
  if (Date.parse(signal.as_of) <= now && now < Date.parse(signal.expires_at)) return signal;
  return { ...signal, action: "SERVICE_FAILURE", failure_code: "decision_publication_overdue",
    summary: "Trading conditions expired before a replacement was published.",
    condition: "New allocations paused; the decision publisher must deliver a current revision.",
    owner_job: "refresh_decision_models", entry_low: null, entry_high: null,
    stop_price: null, target_price: null, risk_per_unit: null };
}

export function ReferenceSignalCard({ signal, plan, resolution, missingIsFailure = false, compact = false }: {
  signal?: Signal | null; compact?: boolean;
} & CapitalDecisionProps) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 30000); return () => clearInterval(timer); }, []);
  const current = visibleReferenceSignal(signal, now);
  const failed = !current || current.action === "SERVICE_FAILURE";
  const terms = current ? [
    ["Entry condition", current.entry_low != null && current.entry_high != null ? `${money(current.entry_low)} – ${money(current.entry_high)}` : null],
    ["Price invalidation", current.stop_price != null ? money(current.stop_price) : null],
    ["Price objective", current.target_price != null ? money(current.target_price) : null],
    ["Planned risk / unit", current.risk_per_unit != null ? money(current.risk_per_unit) : null],
  ].filter(([, value]) => value != null) : [];
  return <section aria-label="Published trading conditions" className={`rounded-lg border p-4 ${failed ? "border-destructive/50 bg-destructive/5" : "border-border bg-card"}`}>
    {!failed ? <CapitalDecision plan={plan} resolution={resolution} missingIsFailure={missingIsFailure} /> : null}
    <div className="mt-4 flex flex-wrap items-center justify-between gap-2">
      <h2 className={`font-semibold ${failed ? "text-destructive" : "text-primary"}`}>{current ? LABELS[current.action] : "SIGNAL SERVICE FAILED"}</h2>
      {!failed ? <span className="text-xs text-muted-foreground">Measured trend · not a validated alpha forecast</span> : null}
    </div>
    <p className="mt-2 text-sm">{current?.summary ?? "The decision publisher has not supplied a complete signal for this instrument."}</p>
    <p className="mt-2 text-sm font-medium">{current?.condition ?? "New allocations paused. Restore the decision publication service."}</p>
    {terms.length ? <dl className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{terms.map(([label, value]) => <div key={label}><dt className="text-xs text-muted-foreground">{label}</dt><dd className="mt-1 font-medium tabular-nums">{value}</dd></div>)}</dl> : null}
    {failed ? <p className="mt-3 text-xs"><strong>Failing producer:</strong> {current?.owner_job ?? "refresh_decision_models"} · <a className="underline" href="/health">Open service diagnostics →</a></p> : <>
      <p className="mt-3 text-xs text-muted-foreground">{current?.quote_state === "closed_reference" ? "US venue closed · last completed-session reference; no fill is implied." : "Continuous / open-session reference; execution needs a separate admissible quote."} Quote observed {dateTime(current?.quote_observed_at)}.</p>
      {!compact ? <p className="mt-1 text-xs text-muted-foreground">{current?.horizon} · feature session {current?.feature_session} · conditions expire {dateTime(current?.expires_at)}. Stops can slip or gap; planned risk is not a guaranteed maximum loss.</p> : null}

    </>}
  </section>;
}
