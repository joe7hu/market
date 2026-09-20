export type PaperObservationProgress = {
  title: string;
  detail: string;
  attention: boolean;
};

/** Counts describe recorded observations, not worker health or trading edge. */
export function paperObservationProgress(counts: Readonly<Record<string, number | undefined>>): PaperObservationProgress {
  const names = ["pending", "entered", "closed", "unfilled", "rejected", "unmeasurable"] as const;
  if (names.some((name) => counts[name] !== undefined && (!Number.isSafeInteger(counts[name]) || counts[name]! < 0))) {
    return { title: "Observation counts need attention", detail: "The snapshot contains invalid counts. Reload the observations before interpreting progress.", attention: true };
  }
  const pending = counts.pending ?? 0;
  const entered = counts.entered ?? 0;
  const closed = counts.closed ?? 0;
  const unfilled = counts.unfilled ?? 0;
  const rejected = counts.rejected ?? 0;
  const unmeasurable = counts.unmeasurable ?? 0;
  if (entered || pending) {
    return { title: `${entered} open observations · ${pending} awaiting entry`, detail: `${closed} completed · ${unmeasurable} unmeasurable. Outstanding observations do not establish that the worker is healthy; review deadlines and quote evidence.`, attention: unmeasurable > 0 };
  }
  if (closed) {
    return { title: `${closed} completed observations`, detail: `${unfilled} unfilled · ${rejected} rejected · ${unmeasurable} unmeasurable. Completion alone does not establish profitability. These observations are excluded from funded paper-account P&L.`, attention: unmeasurable > 0 };
  }
  if (unfilled || rejected || unmeasurable) {
    return { title: "No completed observations", detail: `${unfilled} unfilled · ${rejected} rejected · ${unmeasurable} unmeasurable; none are awaiting entry or open. Review rejection and fill-window reasons below. These counts do not establish whether the execution worker is healthy.`, attention: true };
  }
  return { title: "No observations recorded", detail: "Research observations have not been recorded yet. They remain separate from funded paper orders and their P&L.", attention: false };
}


/** Session state is server-owned; this formatter never ages or promotes a quote. */
export function paperValuationDescription(trade: {
  mark_status?: string; mark_stale?: boolean | null; mark_observed_at?: string | null;
  mark?: Record<string, unknown>;
}): string {
  if (trade.mark_status !== "verified" || trade.mark_stale) return "Open P&L is unavailable until every position leg has a verified valuation mark.";
  const at = trade.mark_observed_at ? ` Observed ${trade.mark_observed_at}.` : "";
  return trade.mark?.session_state === "last_completed_session"
    ? `Last available session price retained for valuation; this is not a live quote or fill authorization.${at}`
    : `Verified valuation only; a paper fill still requires a fresh executable quote.${at}`;
}
