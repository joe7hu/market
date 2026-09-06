import { RefreshCw, SlidersHorizontal } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { components } from "@/generated/apiSchema";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { decisionReason } from "@/components/market/dataFieldState";
import { DataTableFrame, EmptyState } from "@/components/market/workstation";
import { ScopeStatusNotice } from "@/components/market/scopeStatus";
import { rows } from "@/utils";
import { textField } from "./rowFormat";
import type { PanelData, RowRecord, ScopeSnapshotStatus } from "@/types";
import type { OpenTicker } from "./workspacePage";

type SavedView = "episodes" | "screener";
export type OpportunityDecisionRow = components["schemas"]["OpportunityRank"] & {
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
  const [sort, setSort] = useState("source");
  const [requestError, setRequestError] = useState<string | null>(null);
  const rankedRows = useMemo(() => dedupeOpportunityEpisodes(opportunityDecisionRows(data.opportunitiesRanked)), [data.opportunitiesRanked]);
  const table = view === "screener" ? data.screener : data.opportunitiesRanked;
  const loadedCount = rows(table).length;
  const total = table?.count ?? loadedCount;
  const needle = query.trim().toLowerCase();
  const visibleRanks = rankedRows.filter((row) => `${row.ticker} ${row.company_name ?? ""} ${row.rationale ?? ""}`.toLowerCase().includes(needle));
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
      <div><h1 className="text-2xl font-semibold">Opportunities</h1><p className="mt-1 text-sm text-muted-foreground">Research ideas to investigate. Open a ticker for its evidence, countercase, trade plan and portfolio fit.</p></div>
      <div className="flex flex-wrap gap-2">
        <Button disabled={loading} variant={view === "episodes" ? "default" : "outline"} onClick={() => setView("episodes")}>Decision brief</Button>
        <Button disabled={loading} variant={view === "screener" ? "default" : "outline"} onClick={() => setView("screener")}><SlidersHorizontal />Dense screener</Button>
        <Button variant="outline" disabled={loading} onClick={() => void request(() => onRefresh(view === "screener"))}><RefreshCw />Refresh</Button>
      </div>
    </header>
    <ScopeStatusNotice status={scopeStatus} onRetry={() => void request(() => onRefresh(view === "screener"))} />
    {requestError ? <p role="alert">{requestError}</p> : null}
    <div className="mb-4 flex flex-wrap items-center gap-3">
      <Input className="max-w-sm" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter loaded tickers or companies" aria-label="Filter opportunities" />
      {view === "screener" ? <label className="text-sm">Sort loaded rows <select className="ml-2 rounded border border-input bg-background p-2" value={sort} onChange={(event) => setSort(event.target.value)}><option value="source">Portfolio and research order</option><option value="symbol">Ticker A–Z</option>{METRICS.map(([key, label]) => <option key={key} value={key}>{label}: high to low</option>)}</select></label> : null}
      <span className="text-xs text-muted-foreground">{loadedCount} of {total} loaded</span>
    </div>
    {loading && !loadedCount ? <p role="status">Loading ideas…</p> : view === "episodes" ? <DataTableFrame title="Research ideas">
      <div className="divide-y divide-border">{visibleRanks.map((row) => <article key={row.opportunity_episode_id} className="space-y-2 p-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2"><Button variant="link" className="h-auto p-0 text-base font-semibold" onClick={() => onOpenTicker(row.ticker)}>{row.ticker}{row.company_name ? ` · ${row.company_name}` : ""}</Button><span className="text-xs text-muted-foreground">Research hypothesis{row.research_as_of ? ` · ${new Date(row.research_as_of).toLocaleDateString()}` : ""}</span></div>
        {row.rationale ? <p className="text-sm">{decisionReason(row.rationale.split(/(?<=[.!?])\s+/)[0])}</p> : null}
        {row.countercase ? <p className="text-sm text-muted-foreground"><strong>Countercase: </strong>{decisionReason(row.countercase)}</p> : null}
        <p className="text-sm text-muted-foreground"><strong>{row.selected_expression_kind?.toUpperCase() === "CASH" ? "No new trade: " : "Review: "}</strong>{opportunityReason(row)}</p>
        <Button variant="outline" size="sm" onClick={() => onOpenTicker(row.ticker)}>Review {row.ticker}</Button>
      </article>)}</div>
    </DataTableFrame> : <DataTableFrame title="Company fundamentals"><div className="overflow-x-auto"><table className="w-full min-w-[850px] text-sm"><thead><tr><th className="p-3 text-left">Company</th>{METRICS.map(([key,label]) => <th key={key} className="p-3 text-right">{label}</th>)}<th className="p-3 text-left">Evidence dates</th></tr></thead><tbody>{visibleScreen.map((row) => <tr key={textField(row,["symbol"])} className="border-t border-border"><td className="p-3"><Button variant="link" className="h-auto p-0 font-semibold" onClick={() => onOpenTicker(textField(row,["symbol"]))}>{textField(row,["symbol"])}</Button><p className="text-xs text-muted-foreground">{textField(row,["name"])}</p></td>{METRICS.map(([key,,factor,unit]) => <td key={key} className="p-3 text-right tabular-nums" title={typeof row[key] === "number" ? undefined : "Not reported in the current source data"}>{screenerMetric(row[key],factor,unit)}</td>)}<td className="p-3 text-xs text-muted-foreground">{[["Price",row.observed_at],["Financials",row.market_metrics_observed_at],["SEC period",row.sec_fundamentals_observed_at]].map(([label,date]) => typeof date === "string" ? <p key={String(label)}>{String(label)}: {new Date(date).toLocaleDateString()}</p> : null)}</td></tr>)}</tbody></table></div><p className="p-3 text-xs text-muted-foreground">A dash means the current source has not reported the metric. ROIC and forward P/E do not apply to every asset. These metrics are research context, not trade approval.</p></DataTableFrame>}
    {!loading && scopeStatus?.state !== "failed" && !(view === "episodes" ? visibleRanks.length : visibleScreen.length) ? <EmptyState title={needle ? "No loaded tickers match" : "No current research rows"} detail={needle && loadedCount < total ? "Load more rows to search more of the universe." : "Try another filter or refresh the list."} /> : null}
    {loadedCount < total && onLoadMore ? <Button className="mt-4" variant="outline" disabled={loading} onClick={() => void request(() => onLoadMore(view === "screener"))}>Load more ({total - loadedCount} remaining)</Button> : null}
  </section>;
}
