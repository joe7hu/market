import { ReferenceSignalCard } from "@/components/market/ReferenceSignalCard";
import { RefreshCw, SlidersHorizontal } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { components } from "@/generated/apiSchema";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { decisionReason } from "@/components/market/dataFieldState";
import { DataTableFrame, EmptyState } from "@/components/market/workstation";
import { ScopeStatusNotice } from "@/components/market/scopeStatus";
import { rows } from "@/utils";
import { humanize, money, dateTime } from "@/presentation/labels";
import { textField } from "@/shared/rowFormat";
import type { PanelData, RowRecord, ScopeSnapshotStatus } from "@/types";
import type { OpenTicker } from "./workspacePage";

type SavedView = "episodes" | "screener";
export type OpportunityDecisionRow = components["schemas"]["OpportunityRank"] & {
  presentation_state?: string;
  presentation_blocker?: string | null;
  presentation_next_action?: string | null;
  plan_read_status?: string;
  trade_plan?: components["schemas"]["TradePlan"] | null;
  company_name?: string | null;
  rationale?: string | null;
  horizon?: string | null;
  countercase?: string | null;
  research_as_of?: string | null;
};
export const OPPORTUNITIES_SAVED_VIEW_KEY = "market.opportunities.saved-view";
export const EXPRESSION_KINDS = ["stock", "option/spread", "CSP", "crypto", "hedge", "cash"] as const;

export function opportunityDecisionRows(input: { rows?: RowRecord[] } | undefined): OpportunityDecisionRow[] {
  return rows(input).flatMap((row) => typeof row.ticker === "string" && typeof row.opportunity_episode_id === "string"
    && typeof row.rank_id === "string" && typeof row.decision_revision === "string"
    && typeof row.availability_status === "string" && Array.isArray(row.blockers) ? [row as unknown as OpportunityDecisionRow] : []);
}
export function dedupeOpportunityEpisodes(input: OpportunityDecisionRow[]): OpportunityDecisionRow[] {
  const seen = new Set<string>();
  return input.filter((row) => { if (seen.has(row.opportunity_episode_id)) return false; seen.add(row.opportunity_episode_id); return true; });
}
export function shouldLoadScreener(view: SavedView): boolean { return view === "screener"; }

export function opportunityReason(row: OpportunityDecisionRow): string {
  if (row.presentation_blocker) return decisionReason(row.presentation_blocker);
  const substantive = row.blockers.filter((reason) => reason !== "cash_comparator");
  return decisionReason(substantive[0] ?? row.primary_blocker ?? row.trade_rank_unavailable_reason);
}

const METRICS = [
  ["price", "Price", 1, "$"], ["roic", "ROIC", 1, "%"], ["forward_pe", "Forward P/E", 1, "×"],
  ["revenue_growth_yoy", "Revenue growth", 100, "%"], ["fcf_yield", "FCF yield", 100, "%"],
] as const;
export function screenerMetric(value: unknown, factor: number, unit: string): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const formatted = (value * factor).toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: unit === "$" ? 2 : 0 });
  return unit === "$" ? `$${formatted}` : `${formatted}${unit}`;
}

export function OpportunitiesPage({ data, loading, scopeStatus, onOpenTicker, onLoadScreener, onRefresh, onLoadMore }: {
  data: PanelData; loading: boolean; scopeStatus?: ScopeSnapshotStatus; onOpenTicker: OpenTicker;
  onLoadScreener: () => Promise<void>; onRefresh: (includeScreener?: boolean) => Promise<void>;
  onLoadMore?: (screener: boolean) => Promise<void>;
}) {
  const [view, setView] = useState<SavedView>(() => typeof window !== "undefined" && window.localStorage.getItem(OPPORTUNITIES_SAVED_VIEW_KEY) === "screener" ? "screener" : "episodes");
  const [query, setQuery] = useState("");
  const [includeEmpty, setIncludeEmpty] = useState(false);
  const [sort, setSort] = useState("source");
  const [lane, setLane] = useState("all");
  const [requestError, setRequestError] = useState<string | null>(null);
  const rankedRows = useMemo(() => dedupeOpportunityEpisodes(opportunityDecisionRows(data.opportunitiesRanked)), [data.opportunitiesRanked]);
  const table = view === "screener" ? data.screener : data.opportunitiesRanked;
  const loadedCount = rows(table).length;
  const total = table?.count ?? loadedCount;
  const needle = query.trim().toLowerCase();
  const researchRows = rankedRows.filter((row) => row.reference_signal || row.rationale?.trim() || row.countercase?.trim() || (row.presentation_state && row.presentation_state !== "research") || (row.selected_expression_kind && row.selected_expression_kind.toUpperCase() !== "CASH"));
  const visibleRanks = (includeEmpty || needle ? rankedRows : researchRows).filter((row) =>
    `${row.ticker} ${row.company_name ?? ""} ${row.rationale ?? ""}`.toLowerCase().includes(needle) &&
    (lane === "all" || (row.presentation_state ?? "research") === lane)).slice().sort((a, b) =>
    (a.trade_rank ?? Number.MAX_SAFE_INTEGER) - (b.trade_rank ?? Number.MAX_SAFE_INTEGER) ||
    (a.research_rank ?? Number.MAX_SAFE_INTEGER) - (b.research_rank ?? Number.MAX_SAFE_INTEGER));
  const visibleScreen = rows(data.screener).filter((row) => `${row.symbol} ${row.name}`.toLowerCase().includes(needle)).slice().sort((a, b) => {
    if (sort === "source") return 0;
    if (sort === "symbol") return textField(a, ["symbol"]).localeCompare(textField(b, ["symbol"]));
    const left = a[sort], right = b[sort];
    if (typeof left !== "number") return typeof right === "number" ? 1 : 0;
    return typeof right === "number" ? right - left : -1;
  });
  async function request(action: () => Promise<void>) {
    setRequestError(null);
    try { await action(); } catch { setRequestError("The list could not refresh. Try again."); }
  }
  useEffect(() => { window.localStorage.setItem(OPPORTUNITIES_SAVED_VIEW_KEY, view); }, [view]);
  useEffect(() => { void request(shouldLoadScreener(view) ? onLoadScreener : () => onRefresh(false)); }, [onLoadScreener, onRefresh, view]);
  return <section>
    <header className="mb-4 flex flex-wrap items-end justify-between gap-3 border-b border-border pb-4">
      <div><h1 className="text-2xl font-semibold">Opportunities</h1><p className="mt-1 text-sm text-muted-foreground">Compare published decisions, trade terms, risks, and evidence. Research ideas are kept separate from paper-qualified plans.</p></div>
      <div className="flex flex-wrap gap-2">
        <Button disabled={loading} variant={view === "episodes" ? "default" : "outline"} onClick={() => setView("episodes")}>Decision comparison</Button>
        <Button disabled={loading} variant={view === "screener" ? "default" : "outline"} onClick={() => setView("screener")}><SlidersHorizontal />Dense screener</Button>
        <Button variant="outline" disabled={loading} onClick={() => void request(() => onRefresh(view === "screener"))}><RefreshCw />Refresh</Button>
      </div>
    </header>
    <ScopeStatusNotice status={scopeStatus} onRetry={() => void request(() => onRefresh(view === "screener"))} />
    {requestError ? <p role="alert">{requestError}</p> : null}
    <div className="mb-4 flex flex-wrap items-center gap-3">
      <Input className="max-w-sm" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter loaded tickers or companies" aria-label="Filter opportunities" />
      {view === "screener" ? <label className="text-sm">Sort loaded rows <select className="ml-2 rounded border border-input bg-background p-2" value={sort} onChange={(event) => setSort(event.target.value)}><option value="source">Portfolio and research order</option><option value="symbol">Ticker A–Z</option>{METRICS.map(([key, label]) => <option key={key} value={key}>{label}: high to low</option>)}</select></label> : null}
      {view === "episodes" ? <label className="text-sm">Decision state <select aria-label="Filter by decision state" className="ml-2 rounded border border-input bg-background p-2" value={lane} onChange={e => setLane(e.target.value)}>{[["all", "All assessments"], ["paper_review", "Published paper terms"], ["review", "Advisory plans"], ["watch", "Awaiting trigger"], ["research", "Research only"], ["blocked", "Data / account blocked"]].map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label> : null}
      <span className="text-xs text-muted-foreground">{loadedCount} of {total} loaded · filters and ordering apply to loaded rows</span>
    </div>
    {view === "episodes" && loadedCount > 0 ? <div className="mb-4 space-y-2 text-sm text-muted-foreground"><p>{researchRows.length} loaded assessments contain research or a trade expression. Research hypotheses are not trade approvals.</p><label className="flex items-center gap-2"><input type="checkbox" checked={includeEmpty} onChange={(event) => setIncludeEmpty(event.target.checked)} />Show tickers without research ({rankedRows.length - researchRows.length} loaded)</label></div> : null}
    {loading && !loadedCount ? <p role="status">Loading ideas…</p> : view === "episodes" ? <DataTableFrame title="Decision comparison">
      <p className="px-4 py-3 text-xs text-muted-foreground">Ordering uses the published backend trade rank, then research rank. A published paper plan still requires current quotes and fill-time risk checks. No probability is invented for research-only ideas.</p>
      <div className="overflow-x-auto"><table className="w-full min-w-[1000px] text-left text-sm"><thead className="bg-muted/50 text-xs text-muted-foreground"><tr>{["Company / state", "Expression / horizon", "Entry and size", "Planned loss", "Lower-confidence net P&L", "Evidence / next step"].map(label => <th key={label} className="p-3">{label}</th>)}</tr></thead><tbody>{visibleRanks.map(row => {
        const plan = row.trade_plan;
        const terms = plan?.eligibility === "ACTIONABLE" && row.presentation_state !== "blocked";
        if (row.reference_signal) return <tr key={row.rank_id} className="border-t border-border align-top"><td className="p-3"><Button variant="link" className="h-auto p-0 font-semibold" onClick={() => onOpenTicker(row.ticker)}>{row.ticker}</Button><p className="mt-1 text-xs text-muted-foreground">{row.company_name}</p><Button variant="link" className="mt-2 h-auto p-0 text-xs" onClick={() => onOpenTicker(row.ticker)}>Open signal →</Button></td><td className="p-3" colSpan={5}><ReferenceSignalCard signal={row.reference_signal} plan={plan} compact /></td></tr>;
        return <tr key={row.opportunity_episode_id} className="border-t border-border align-top">
          <td className="p-3"><Button variant="link" className="h-auto p-0 font-semibold" onClick={() => onOpenTicker(row.ticker)}>{row.ticker}</Button><p className="mt-1 text-xs text-muted-foreground">{row.company_name}</p><span className="mt-2 inline-block rounded border px-2 py-1 text-xs">{opportunityStateLabel(row.presentation_state)}</span>{row.trade_rank ? <p className="mt-1 text-xs">Published trade rank {row.trade_rank}</p> : null}</td>
          <td className="p-3"><p>{humanize(plan?.selected_expression_kind ?? row.selected_expression_kind, "Research only")}</p><p className="mt-1 text-xs text-muted-foreground">{row.horizon || "Horizon not specified"}</p></td>
          {terms ? <><td className="p-3 tabular-nums">{money(plan.entry_limit)}<p className="mt-1 text-xs">{plan.quantity} units</p>{plan.expiry ? <p className="mt-1 text-xs text-muted-foreground">Expiry / time exit {String(plan.expiry).slice(0, 10)}</p> : null}</td><td className="p-3 tabular-nums">{money(plan.planned_loss)}<p className="mt-1 text-xs text-muted-foreground">Max loss / unit {money(plan.max_loss_per_unit)}</p></td></> : <td className="p-3 text-xs text-muted-foreground" colSpan={2}>No executable terms in this assessment.<p className="mt-1">{row.plan_read_status === "read_failed" ? "Trade-plan read failed; inspect publication health." : opportunityReason(row)}</p></td>}
          <td className="p-3 tabular-nums">{typeof row.lower_confidence_expected_net_pnl === "number" && Number.isFinite(row.lower_confidence_expected_net_pnl) ? money(row.lower_confidence_expected_net_pnl, true) : <span className="text-xs text-muted-foreground">Not established</span>}</td>
          <td className="max-w-md p-3"><p className="text-xs">{row.presentation_next_action ?? plan?.next_action ?? opportunityReason(row)}</p><p className="mt-2 text-xs text-muted-foreground">Decision cutoff {dateTime(row.cutoff)}</p><details className="mt-2 text-xs"><summary className="cursor-pointer font-medium">Thesis and countercase</summary>{row.rationale ? <p className="mt-2 leading-5">{row.rationale.replace(/\s*Full detail follows\.?/gi, "")}</p> : <p className="mt-2">No research thesis recorded.</p>}{row.countercase ? <p className="mt-2 leading-5 text-muted-foreground"><strong>Countercase: </strong>{row.countercase}</p> : null}</details><Button variant="link" className="mt-2 h-auto p-0 text-xs" onClick={() => onOpenTicker(row.ticker)}>Review {row.ticker} →</Button></td>
        </tr>;
      })}</tbody></table></div>
    </DataTableFrame> : <DataTableFrame title="Company fundamentals"><div className="overflow-x-auto"><table className="w-full min-w-[850px] text-sm"><thead><tr><th className="p-3 text-left">Company</th>{METRICS.map(([key,label]) => <th key={key} className="p-3 text-right">{label}</th>)}<th className="p-3 text-left">Evidence dates</th></tr></thead><tbody>{visibleScreen.map((row) => <tr key={textField(row,["symbol"])} className="border-t border-border"><td className="p-3"><Button variant="link" className="h-auto p-0 font-semibold" onClick={() => onOpenTicker(textField(row,["symbol"]))}>{textField(row,["symbol"])}</Button><p className="text-xs text-muted-foreground">{textField(row,["name"])}</p></td>{METRICS.map(([key,,factor,unit]) => <td key={key} className="p-3 text-right tabular-nums" title={typeof row[key] === "number" ? undefined : "Not reported in the current source data"}>{screenerMetric(row[key],factor,unit)}</td>)}<td className="p-3 text-xs text-muted-foreground">{[["Price",row.observed_at],["Financials",row.market_metrics_observed_at],["SEC period",row.sec_fundamentals_observed_at]].map(([label,date]) => typeof date === "string" ? <p key={String(label)}>{String(label)}: {new Date(date).toLocaleDateString()}</p> : null)}</td></tr>)}</tbody></table></div><p className="p-3 text-xs text-muted-foreground">A dash means the current source has not reported the metric. ROIC and forward P/E do not apply to every asset. These metrics are research context, not trade approval.</p></DataTableFrame>}
    {!loading && scopeStatus?.state !== "failed" && !(view === "episodes" ? visibleRanks.length : visibleScreen.length) ? <EmptyState title={needle ? "No loaded tickers match" : lane === "paper_review" ? "No published paper-qualified plans in the loaded population" : lane === "all" ? "No research assessments in these loaded rows" : "No assessments in this view"} detail={needle && loadedCount < total ? "Load more rows to search more of the universe." : "Try another filter or refresh the list."} /> : null}
    {loadedCount < total && onLoadMore ? <Button className="mt-4" variant="outline" disabled={loading} onClick={() => void request(() => onLoadMore(view === "screener"))}>Load more ({total - loadedCount} remaining)</Button> : null}
  </section>;
}

export function opportunityStateLabel(value: string | undefined): string {
  return ({ paper_review: "Published paper terms", review: "Advisory plan", watch: "Awaiting trigger",
    blocked: "Input / account blocked", research: "Research only" } as Record<string, string>)[value ?? "research"] ?? "Research only";
}
