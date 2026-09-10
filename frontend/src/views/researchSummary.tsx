import { useEffect, useState } from "react";
import { loadResearchSummary, type ResearchSummary as Summary } from "@/api/panel";
import { Button } from "@/components/ui/button";
import { DataTableFrame, EmptyState, StatusBadge } from "@/components/market/workstation";
import { decisionReason } from "@/components/market/dataFieldState";
import { formatPct, titleLabel } from "@/shared/rowFormat";

export function ResearchSummary({ onOpenTicker }: { onOpenTicker: (ticker: string) => void }) {
  const [data, setData] = useState<Summary | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    void loadResearchSummary(controller.signal).then((value) => { if (!controller.signal.aborted) setData(value); }).catch(() => {
      if (!controller.signal.aborted) setError("Research results could not refresh.");
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [attempt]);
  return <div className="space-y-4">
    <div className="flex items-center justify-between gap-3"><h2 className="text-lg font-semibold">Strategy results</h2><Button variant="outline" disabled={loading} onClick={() => setAttempt(attempt + 1)}>Refresh results</Button></div>
    {loading && !data ? <p role="status">Loading strategy evidence…</p> : null}
    {error ? <p role="alert" className="text-sm text-destructive">{error}{data ? " Showing the previous results." : " Results are unavailable."}</p> : null}
    {data ? <ResearchResults data={data} onOpenTicker={onOpenTicker} /> : null}
  </div>;
}

export function ResearchResults({ data, onOpenTicker }: { data: Summary; onOpenTicker: (ticker: string) => void }) {
  const reviews = data.review_activity;
  const strategies = data.strategies ?? [];
  const ideas = data.ideas ?? [];
  return <div className="space-y-4">
    <p className="text-sm text-muted-foreground">Paper only. Research results do not authorize a trade. {strategies.length} of {data.strategy_count} active or latest research revisions shown. Read at {new Date(data.as_of).toLocaleString()}.</p>
    {strategies.length ? <div className="grid gap-3 xl:grid-cols-2">{strategies.map((strategy) => <article key={strategy.strategy_revision_id} className="space-y-3 rounded-lg border border-border p-4">
      <div className="flex items-start justify-between gap-3"><h3 className="font-semibold">{strategy.name} · v{strategy.revision}</h3><StatusBadge tone={strategy.status === "active" ? "info" : "muted"}>{titleLabel(strategy.status)}</StatusBadge></div>
      {strategy.hypothesis ? <p className="text-sm">{strategy.hypothesis}</p> : null}
      <p className="text-xs text-muted-foreground">{strategy.automatic_paper_tuning ? "Automatic paper tuning enabled; promotion gates apply." : "Research only; automatic tuning is not enabled for this strategy."} Last policy change: {strategy.last_policy_change ? new Date(strategy.last_policy_change).toLocaleDateString() : "not recorded"}.</p>
      {strategy.evaluations?.length ? <div className="space-y-2">{strategy.evaluations.map((evaluation) => <div key={evaluation.stage} className="rounded border border-border p-3 text-sm">
        <p className="font-medium">{titleLabel(evaluation.stage)} · {titleLabel(evaluation.verdict)}</p>
        <p className="text-xs text-muted-foreground">Measured period: {evaluation.period_start && evaluation.period_end ? `${new Date(evaluation.period_start).toLocaleDateString()} – ${new Date(evaluation.period_end).toLocaleDateString()}` : "not recorded"}. Evaluated: {evaluation.evaluated_at ? new Date(evaluation.evaluated_at).toLocaleString() : "not recorded"}.</p>
        <p>Independent observations: {evaluation.independent_sample_count ?? "unknown"}</p>
        <p>Return after {evaluation.evidence_basis === "independent_options_paper" ? "actual paper" : "modeled"} costs, lower estimate: {evaluation.net_return_lower_bound == null ? "unknown" : formatPct(evaluation.net_return_lower_bound * 100)}</p>
        <p>Probability error (Brier): {evaluation.brier_score == null ? "unknown" : evaluation.brier_score.toFixed(3)}</p>
        {evaluation.evidence_basis.startsWith("independent_options_") ? <p className="text-xs text-muted-foreground">Comparison: {evaluation.comparison_denominator ?? "unknown"} episodes · window {evaluation.comparison_window_complete == null ? "unknown" : evaluation.comparison_window_complete ? "complete" : "incomplete"} · {evaluation.unmatched_episodes ?? "unknown"} unknown outcomes.</p> : null}
        <p className="text-xs text-muted-foreground">{evaluation.evidence_basis === "obsolete_stock_target" ? "Earlier target: cannot validate this stock strategy."
          : evaluation.evidence_basis === "independent_stock_episodes" ? "Stock outcomes from independent test episodes; not paper fills."
          : evaluation.evidence_basis === "independent_options_replay" ? "Independent historical option episodes with the candidate filter applied. Returns include confirmed CASH decisions."
          : evaluation.evidence_basis === "independent_options_shadow" ? "Prospective candidate shadow outcomes with modeled costs. Returns include confirmed CASH decisions."
          : evaluation.evidence_basis === "independent_options_paper" ? "Completed candidate paper orders with recorded fills and costs. Returns include confirmed CASH decisions."
          : "Independent outcome coverage has not been confirmed."}</p>
      </div>)}</div> : <p className="text-sm text-muted-foreground">No evaluation is recorded. Returns and sample size are unknown.</p>}
      <p className="text-xs text-muted-foreground">Latest trial: {strategy.trial_status ? titleLabel(strategy.trial_status) : "not recorded"}. Universe: {strategy.included_count ?? "unknown"} included · {strategy.excluded_count ?? "unknown"} excluded · {strategy.expected_count ?? "unknown"} expected. These are universe records, not completed trades.</p>
      {strategy.failed_gates?.length ? <p className="text-sm"><strong>Failed or incomplete gates:</strong> {strategy.failed_gates.map(decisionReason).join("; ")}</p> : null}
      <p className="text-sm"><strong>Next observation:</strong> {strategy.next_observation}</p>
    </article>)}</div> : <EmptyState title="No strategy evidence is recorded" detail="A strategy needs an evaluated candidate and measured outcomes before its results can be shown." />}
    <DataTableFrame title="Review usefulness">
      <div className="space-y-2 p-4 text-sm"><p>For {reviews.total} Inbox items created in the last {reviews.window_days} days: {reviews.acknowledged} acknowledged · {reviews.completed} reviewed.</p>
        <p>{reviews.rated} rated · {reviews.helpful} helpful · {reviews.not_helpful} not helpful. Helpful rate: {reviews.helpful_rate == null ? "unknown until feedback is recorded" : `${(reviews.helpful_rate * 100).toFixed(1)}% of rated items`}.</p>
        <p className="text-xs text-muted-foreground">Use Helpful / Not helpful on a Command Center review. Review activity and usefulness ratings do not measure profit.</p></div>
    </DataTableFrame>
    <DataTableFrame title="Three research ideas to review">
      <div className="divide-y divide-border">{ideas.map((idea) => <article key={idea.ticker} className="space-y-2 p-4 text-sm">
        <div className="flex flex-wrap items-center justify-between gap-2"><Button variant="link" className="h-auto p-0 text-base font-semibold" onClick={() => onOpenTicker(idea.ticker)}>{idea.ticker}</Button><span className="text-xs text-muted-foreground">{idea.owned ? `Held · ${idea.holding_weight_pct == null ? "weight unavailable" : `${idea.holding_weight_pct.toFixed(1)}% of holdings value, excluding cash`}` : "Watchlist"}</span></div>
        <p><strong>Thesis:</strong> {idea.thesis || "No thesis is recorded."}</p>
        <p><strong>Countercase:</strong> {idea.countercase || "No opposing case is recorded. This does not confirm the thesis."}</p>
        <p><strong>Catalyst:</strong> {idea.catalyst || "No catalyst is recorded."}</p>
        <p><strong>What changes the decision:</strong> {idea.invalidation || "Add a concrete invalidation before a new trade."}</p>
        <p className="text-muted-foreground"><strong>Next:</strong> {idea.next_action}</p>
        <Button variant="outline" size="sm" onClick={() => onOpenTicker(idea.ticker)}>Review evidence and current plan</Button>
      </article>)}</div>
      {!ideas.length ? <p className="p-4 text-sm text-muted-foreground">No published research briefs are available for current holdings or the watchlist.</p> : null}
    </DataTableFrame>
  </div>;
}
