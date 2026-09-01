import { RefreshCw, SlidersHorizontal } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { components } from "@/generated/apiSchema";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { DataTableFrame, EmptyState, StatusBadge } from "@/components/market/workstation";
import { ScopeStatusNotice } from "@/components/market/scopeStatus";
import { rows } from "@/utils";
import { displayField, numberField, textField, titleLabel, toneFromText } from "./rowFormat";
import type { PanelData, RowRecord, ScopeSnapshotStatus } from "@/types";
import type { OpenTicker } from "./workspacePage";
import { TradePlanCard } from "./TradePlanCard";

type SavedView = "episodes" | "screener";
type TradePlan = components["schemas"]["TradePlan"];

export const OPPORTUNITIES_SAVED_VIEW_KEY = "market.opportunities.saved-view";
export const EXPRESSION_KINDS = ["stock", "option/spread", "CSP", "crypto", "hedge", "cash"] as const;

export function dedupeOpportunityEpisodes(input: RowRecord[]): RowRecord[] {
  const seen = new Set<string>();
  return input.filter((row) => {
    const symbol = textField(row, ["symbol", "ticker"]).toUpperCase();
    const identity = textField(row, ["episode_id", "opportunity_id", "episode", "id"], `${symbol}:${textField(row, ["horizon"], "unknown")}`);
    if (seen.has(identity)) return false;
    seen.add(identity);
    return true;
  });
}

export function shouldLoadScreener(view: SavedView): boolean {
  return view === "screener";
}

export function OpportunitiesPage({ data, loading, scopeStatus, onOpenTicker, onLoadScreener, onRefresh }: { data: PanelData; loading: boolean; scopeStatus?: ScopeSnapshotStatus; onOpenTicker: OpenTicker; onLoadScreener: () => Promise<void>; onRefresh: (includeScreener?: boolean) => Promise<void> }) {
  const [view, setView] = useState<SavedView>(() => readSavedView());
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<RowRecord | null>(null);
  const rankedRows = useMemo(() => dedupeOpportunityEpisodes(rows(data.opportunitiesRanked)), [data.opportunitiesRanked]);
  const visibleRows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return needle ? rankedRows.filter((row) => JSON.stringify(row).toLowerCase().includes(needle)) : rankedRows;
  }, [query, rankedRows]);

  useEffect(() => {
    window.localStorage.setItem(OPPORTUNITIES_SAVED_VIEW_KEY, view);
  }, [view]);

  useEffect(() => {
    if (!shouldLoadScreener(view)) return;
    void onLoadScreener();
  }, [onLoadScreener, view]);

  return (
    <section>
      <header className="mb-4 flex flex-col gap-3 border-b border-border pb-4 md:flex-row md:items-end md:justify-between">
        <div>
          <p className="mb-1 text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">Decision surface</p>
          <h1 className="text-2xl font-semibold md:text-3xl">Opportunities</h1>
          <p className="mt-1 max-w-4xl text-sm leading-6 text-muted-foreground">One row per opportunity episode. Open a row to compare expressions and see the current recommendation and blocker.</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button type="button" variant={view === "episodes" ? "default" : "outline"} size="sm" onClick={() => setView("episodes")}>Episodes</Button>
          <Button type="button" variant={view === "screener" ? "default" : "outline"} size="sm" onClick={() => setView("screener")}><SlidersHorizontal /> Dense screener</Button>
          <Button type="button" variant="outline" size="sm" disabled={loading} onClick={() => void onRefresh(view === "screener")}><RefreshCw className={loading ? "animate-spin" : undefined} /> Refresh</Button>
        </div>
      </header>
      <ScopeStatusNotice status={scopeStatus} onRetry={() => void onRefresh(view === "screener")} />
      <div className="mb-4 flex flex-wrap items-center gap-2 rounded-lg border border-border bg-card p-2">
        <Input className="max-w-sm" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter episodes" aria-label="Filter opportunities" />
        <StatusBadge tone={rankedRows.length ? "info" : "warn"}>{rankedRows.length} episodes</StatusBadge>
        <span className="text-xs text-muted-foreground">Saved view: {view === "episodes" ? "Episodes" : "Dense screener"}</span>
      </div>
      {view === "episodes" ? <EpisodeTable rows={visibleRows} onSelect={setSelected} /> : <ScreenerTable rows={rows(data.screener).slice(0, 120)} onOpenTicker={onOpenTicker} />}
      {!rankedRows.length && view === "episodes" ? <EmptyState title="No opportunity episodes loaded" detail="The PostgreSQL opportunity publication is empty or unavailable. No recommendation is inferred from screener rows." /> : null}
      <OpportunityDetail row={selected} onClose={() => setSelected(null)} onOpenTicker={onOpenTicker} />
    </section>
  );
}

function EpisodeTable({ rows: episodeRows, onSelect }: { rows: RowRecord[]; onSelect: (row: RowRecord) => void }) {
  return (
    <DataTableFrame title="Current opportunity episodes">
      <div className="divide-y divide-border md:hidden">
        {episodeRows.map((row, index) => <EpisodeCard key={episodeIdentity(row, index)} row={row} onSelect={onSelect} />)}
      </div>
      <div className="hidden overflow-x-auto md:block">
        <table className="w-full min-w-[1420px] text-sm">
          <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground"><tr>{["Episode", "Horizon", "Thesis delta", "Research rank", "Trade rank", "Action", "Best expression", "Utility range", "Primary blocker", "Catalyst / TTL", "Portfolio impact"].map((label) => <th key={label} className="whitespace-nowrap px-3 py-3">{label}</th>)}</tr></thead>
          <tbody>{episodeRows.map((row, index) => <EpisodeRow key={episodeIdentity(row, index)} row={row} onSelect={onSelect} />)}</tbody>
        </table>
      </div>
      {!episodeRows.length ? <p className="px-4 py-6 text-sm text-muted-foreground">No episodes match this filter.</p> : null}
    </DataTableFrame>
  );
}

function EpisodeRow({ row, onSelect }: { row: RowRecord; onSelect: (row: RowRecord) => void }) {
  const symbol = textField(row, ["symbol", "ticker"], "—").toUpperCase();
  const blocker = displayField(row, ["primary_blocker", "blocker", "trade_rank_unavailable_reason"], "Clear");
  return <tr className="cursor-pointer border-b border-border align-top hover:bg-accent/40" onClick={() => onSelect(row)}><td className="px-3 py-3 font-semibold">{textField(row, ["episode", "episode_id", "opportunity_id"], symbol)}</td><td className="px-3 py-3">{displayField(row, ["horizon", "horizon_label"])}</td><td className="max-w-56 px-3 py-3">{displayField(row, ["thesis_delta", "thesis_change", "thesis"])}</td><td className="px-3 py-3 tabular-nums">{rank(row, ["research_rank"])}</td><td className="px-3 py-3 tabular-nums">{rank(row, ["trade_rank"])}</td><td className="px-3 py-3"><StatusBadge tone={toneFromText(textField(row, ["action", "decision"], "NO_TRADE"))}>{titleLabel(textField(row, ["action", "decision"], "NO_TRADE"))}</StatusBadge></td><td className="px-3 py-3">{displayField(row, ["best_expression", "selected_expression", "selected_expression_kind"])}</td><td className="px-3 py-3">{utilityRange(row)}</td><td className="max-w-56 px-3 py-3 text-muted-foreground">{blocker}</td><td className="px-3 py-3">{displayField(row, ["catalyst", "catalyst_or_ttl", "expires_at", "ttl"])}</td><td className="max-w-56 px-3 py-3">{displayField(row, ["portfolio_impact", "impact", "portfolio_fit"])}</td></tr>;
}

function EpisodeCard({ row, onSelect }: { row: RowRecord; onSelect: (row: RowRecord) => void }) {
  const symbol = textField(row, ["symbol", "ticker"], "Opportunity").toUpperCase();
  return <button type="button" className="p-4 text-left" onClick={() => onSelect(row)}><div className="flex items-start justify-between gap-3"><span className="font-semibold">{textField(row, ["episode", "episode_id", "opportunity_id"], symbol)}</span><StatusBadge tone={toneFromText(textField(row, ["action", "decision"], "NO_TRADE"))}>{titleLabel(textField(row, ["action", "decision"], "NO_TRADE"))}</StatusBadge></div><p className="mt-1 text-sm text-muted-foreground">{displayField(row, ["horizon", "horizon_label"])} · {displayField(row, ["thesis_delta", "thesis_change", "thesis"])}</p><p className="mt-3 text-sm">{displayField(row, ["best_expression", "selected_expression_kind"])} · utility {utilityRange(row)}</p><p className="mt-1 text-xs text-muted-foreground">Blocker: {displayField(row, ["primary_blocker", "blocker"], "Clear")}</p></button>;
}

function SceenerCell({ row, keys }: { row: RowRecord; keys: string[] }) { return <>{displayField(row, keys)}</>; }

function ScreenerTable({ rows: screenerRows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: OpenTicker }) {
  return <DataTableFrame title="Dense screener (secondary saved view)"><div className="overflow-x-auto"><table className="w-full min-w-[900px] text-sm"><thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground"><tr>{["Ticker", "Price", "Quality", "Value", "Momentum", "Research", "Options", "Open"].map((label) => <th key={label} className="px-3 py-3">{label}</th>)}</tr></thead><tbody>{screenerRows.map((row, index) => { const symbol = textField(row, ["symbol", "ticker"]).toUpperCase(); return <tr key={`${symbol}:${index}`} className="border-b border-border"><td className="px-3 py-3 font-semibold">{symbol || "—"}</td><td className="px-3 py-3 tabular-nums"><SceenerCell row={row} keys={["price", "close"]} /></td><td className="px-3 py-3"><SceenerCell row={row} keys={["quality_score", "quality", "roic"]} /></td><td className="px-3 py-3"><SceenerCell row={row} keys={["value_signal", "forward_pe", "valuation_percentile"]} /></td><td className="px-3 py-3"><SceenerCell row={row} keys={["momentum", "technical_score", "return_3m"]} /></td><td className="px-3 py-3"><SceenerCell row={row} keys={["research_status", "research_rank"]} /></td><td className="px-3 py-3"><SceenerCell row={row} keys={["options_status", "options_iv_regime"]} /></td><td className="px-3 py-2">{symbol ? <Button type="button" variant="ghost" size="sm" onClick={() => onOpenTicker(symbol)}>Open</Button> : "—"}</td></tr>; })}</tbody></table>{!screenerRows.length ? <p className="p-4 text-sm text-muted-foreground">No screener rows available.</p> : null}</div></DataTableFrame>;
}

function OpportunityDetail({ row, onClose, onOpenTicker }: { row: RowRecord | null; onClose: () => void; onOpenTicker: OpenTicker }) {
  const symbol = textField(row ?? undefined, ["symbol", "ticker"]).toUpperCase();
  const plan = recordField(row, "trade_plan") as unknown as TradePlan | null;
  return <Sheet open={Boolean(row)} onOpenChange={(open) => !open && onClose()}><SheetContent side="right" className="w-full overflow-y-auto sm:max-w-2xl"><SheetHeader><SheetTitle>{textField(row ?? undefined, ["episode", "episode_id", "opportunity_id"], symbol || "Opportunity episode")}</SheetTitle><SheetDescription>One canonical episode with the current recommendation, reason, and expression comparison.</SheetDescription></SheetHeader>{row ? <div className="space-y-5 py-5"><Card><CardHeader><CardTitle>Current recommendation</CardTitle></CardHeader><CardContent className="space-y-2 text-sm"><div className="flex flex-wrap gap-2"><StatusBadge tone={toneFromText(textField(row, ["action", "decision"], "NO_TRADE"))}>{titleLabel(textField(row, ["action", "decision"], "NO_TRADE"))}</StatusBadge><StatusBadge tone="info">{displayField(row, ["best_expression", "selected_expression_kind"], "CASH")}</StatusBadge></div><p><strong>Reason:</strong> {displayField(row, ["reason", "rationale", "thesis_delta"], "No reason recorded.")}</p><p><strong>Blocker:</strong> {displayField(row, ["primary_blocker", "blocker"], "None recorded.")}</p><p><strong>Portfolio impact:</strong> {displayField(row, ["portfolio_impact", "impact", "portfolio_fit"], "Unavailable")}</p>{symbol ? <Button type="button" variant="outline" onClick={() => onOpenTicker(symbol)}>Open canonical ticker</Button> : null}</CardContent></Card><ExpressionComparison row={row} />{plan ? <TradePlanCard plan={plan} /> : null}</div> : null}</SheetContent></Sheet>;
}

function ExpressionComparison({ row }: { row: RowRecord }) {
  const source = arrayField(row, ["expressions", "expression_comparison", "alternatives"]);
  const byKind = new Map(source.map((item) => [textField(item, ["kind", "expression", "expression_kind"]).toLowerCase(), item]));
  const selected = textField(row, ["best_expression", "selected_expression_kind"]).toLowerCase();
  return <DataTableFrame title="Expression comparison"><div className="overflow-x-auto"><table className="w-full min-w-[560px] text-sm"><thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground"><tr><th className="px-3 py-3">Expression</th><th className="px-3 py-3">State</th><th className="px-3 py-3">Utility</th><th className="px-3 py-3">Blocker</th></tr></thead><tbody>{EXPRESSION_KINDS.map((kind) => { const item = byKind.get(kind.toLowerCase()) ?? byKind.get(kind === "option/spread" ? "option" : kind); const state = item ? displayField(item, ["status", "state", "eligibility"], kind.toLowerCase() === selected ? "SELECTED" : "AVAILABLE") : "Unavailable"; return <tr key={kind} className="border-b border-border"><td className="px-3 py-3 font-medium">{kind}</td><td className="px-3 py-3"><StatusBadge tone={toneFromText(state)}>{titleLabel(state)}</StatusBadge></td><td className="px-3 py-3">{item ? displayField(item, ["utility", "trade_utility", "expectancy", "utility_range"]) : "Unavailable"}</td><td className="px-3 py-3 text-muted-foreground">{item ? displayField(item, ["primary_blocker", "blocker"], "None") : "Unavailable"}</td></tr>; })}</tbody></table></div></DataTableFrame>;
}

function readSavedView(): SavedView { if (typeof window === "undefined") return "episodes"; return window.localStorage.getItem(OPPORTUNITIES_SAVED_VIEW_KEY) === "screener" ? "screener" : "episodes"; }
function episodeIdentity(row: RowRecord, index: number): string { return textField(row, ["episode_id", "opportunity_id", "episode", "id"], `${textField(row, ["symbol", "ticker"], "episode")}:${index}`); }
function rank(row: RowRecord, keys: string[]): string { const value = numberField(row, keys, Number.NaN); return Number.isFinite(value) ? `#${value}` : "—"; }
function utilityRange(row: RowRecord): string { return displayField(row, ["utility_range", "trade_utility_range", "utility", "trade_utility"], "—"); }
function recordField(row: RowRecord | null, key: string): Record<string, unknown> | null { const value = row?.[key]; return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null; }
function arrayField(row: RowRecord, keys: string[]): RowRecord[] { for (const key of keys) { const value = row[key]; if (Array.isArray(value)) return value.filter((item) => Boolean(item) && typeof item === "object" && !Array.isArray(item)) as RowRecord[]; } return []; }
