import { RefreshCw, SlidersHorizontal } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { components } from "@/generated/apiSchema";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { DataFieldStateNotice, missingFieldState } from "@/components/market/dataFieldState";
import { DataTableFrame, EmptyState, StatusBadge } from "@/components/market/workstation";
import { ScopeStatusNotice } from "@/components/market/scopeStatus";
import { rows } from "@/utils";
import { displayField, textField, titleLabel, toneFromText } from "./rowFormat";
import type { PanelData, RowRecord, ScopeSnapshotStatus } from "@/types";
import type { OpenTicker } from "./workspacePage";

type SavedView = "episodes" | "screener";
export type OpportunityDecisionRow = components["schemas"]["OpportunityRank"];

export const OPPORTUNITIES_SAVED_VIEW_KEY = "market.opportunities.saved-view";
export const EXPRESSION_KINDS = ["stock", "option/spread", "CSP", "crypto", "hedge", "cash"] as const;

export function opportunityDecisionRows(input: { rows?: RowRecord[] } | undefined): OpportunityDecisionRow[] {
  return rows(input).flatMap((row) => isOpportunityDecisionRow(row) ? [row as unknown as OpportunityDecisionRow] : []);
}

export function dedupeOpportunityEpisodes(input: OpportunityDecisionRow[]): OpportunityDecisionRow[] {
  const seen = new Set<string>();
  return input.filter((row) => {
    const identity = row.opportunity_episode_id;
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
  const [selected, setSelected] = useState<OpportunityDecisionRow | null>(null);
  const rankedRows = useMemo(() => dedupeOpportunityEpisodes(opportunityDecisionRows(data.opportunitiesRanked)), [data.opportunitiesRanked]);
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

function EpisodeTable({ rows: episodeRows, onSelect }: { rows: OpportunityDecisionRow[]; onSelect: (row: OpportunityDecisionRow) => void }) {
  return (
    <DataTableFrame title="Current opportunity episodes">
      <div className="divide-y divide-border md:hidden">
        {episodeRows.map((row, index) => <EpisodeCard key={episodeIdentity(row, index)} row={row} onSelect={onSelect} />)}
      </div>
      <div className="hidden overflow-x-auto md:block">
        <table className="w-full min-w-[1420px] text-sm">
          <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground"><tr>{["Episode", "Ticker", "Research rank", "Trade rank", "Availability", "Selected expression", "Trade utility", "Primary blocker", "Decision revision"].map((label) => <th key={label} className="whitespace-nowrap px-3 py-3">{label}</th>)}</tr></thead>
          <tbody>{episodeRows.map((row, index) => <EpisodeRow key={episodeIdentity(row, index)} row={row} onSelect={onSelect} />)}</tbody>
        </table>
      </div>
      {!episodeRows.length ? <p className="px-4 py-6 text-sm text-muted-foreground">No episodes match this filter.</p> : null}
    </DataTableFrame>
  );
}

function EpisodeRow({ row, onSelect }: { row: OpportunityDecisionRow; onSelect: (row: OpportunityDecisionRow) => void }) {
  return <tr className="cursor-pointer border-b border-border align-top hover:bg-accent/40" onClick={() => onSelect(row)}><td className="px-3 py-3 font-semibold">{row.opportunity_episode_id}</td><td className="px-3 py-3 font-semibold">{row.ticker}</td><td className="px-3 py-3 tabular-nums">{rank(row.research_rank)}</td><td className="px-3 py-3 tabular-nums">{rank(row.trade_rank)}</td><td className="px-3 py-3"><StatusBadge tone={toneFromText(row.availability_status)}>{titleLabel(row.availability_status)}</StatusBadge></td><td className="px-3 py-3">{row.selected_expression_kind ?? "CASH"}</td><td className="px-3 py-3">{utilityRange(row.trade_utility)}</td><td className="max-w-56 px-3 py-3 text-muted-foreground">{row.primary_blocker ?? row.trade_rank_unavailable_reason ?? "Clear"}</td><td className="px-3 py-3 text-xs text-muted-foreground">{row.decision_revision}</td></tr>;
}

function EpisodeCard({ row, onSelect }: { row: OpportunityDecisionRow; onSelect: (row: OpportunityDecisionRow) => void }) {
  return <button type="button" className="p-4 text-left" onClick={() => onSelect(row)}><div className="flex items-start justify-between gap-3"><span className="font-semibold">{row.opportunity_episode_id} · {row.ticker}</span><StatusBadge tone={toneFromText(row.availability_status)}>{titleLabel(row.availability_status)}</StatusBadge></div><p className="mt-1 text-sm text-muted-foreground">Research {rank(row.research_rank)} · trade {rank(row.trade_rank)}</p><p className="mt-3 text-sm">{row.selected_expression_kind ?? "CASH"} · utility {utilityRange(row.trade_utility)}</p><p className="mt-1 text-xs text-muted-foreground">Blocker: {row.primary_blocker ?? row.trade_rank_unavailable_reason ?? "Clear"}</p></button>;
}

function SceenerCell({ row, keys }: { row: RowRecord; keys: string[] }) { return <>{displayField(row, keys)}</>; }

function ScreenerTable({ rows: screenerRows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: OpenTicker }) {
  return <DataTableFrame title="Dense screener (secondary saved view)"><div className="overflow-x-auto"><table className="w-full min-w-[900px] text-sm"><thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground"><tr>{["Ticker", "Price", "Quality", "Value", "Momentum", "Research", "Options", "Open"].map((label) => <th key={label} className="px-3 py-3">{label}</th>)}</tr></thead><tbody>{screenerRows.map((row, index) => { const symbol = textField(row, ["symbol", "ticker"]).toUpperCase(); return <tr key={`${symbol}:${index}`} className="border-b border-border"><td className="px-3 py-3 font-semibold">{symbol || "—"}</td><td className="px-3 py-3 tabular-nums"><SceenerCell row={row} keys={["price", "close"]} /></td><td className="px-3 py-3"><SceenerCell row={row} keys={["quality_score", "quality", "roic"]} /></td><td className="px-3 py-3"><SceenerCell row={row} keys={["value_signal", "forward_pe", "valuation_percentile"]} /></td><td className="px-3 py-3"><SceenerCell row={row} keys={["momentum", "technical_score", "return_3m"]} /></td><td className="px-3 py-3"><SceenerCell row={row} keys={["research_status", "research_rank"]} /></td><td className="px-3 py-3"><SceenerCell row={row} keys={["options_status", "options_iv_regime"]} /></td><td className="px-3 py-2">{symbol ? <Button type="button" variant="ghost" size="sm" onClick={() => onOpenTicker(symbol)}>Open</Button> : "—"}</td></tr>; })}</tbody></table>{!screenerRows.length ? <p className="p-4 text-sm text-muted-foreground">No screener rows available.</p> : null}</div></DataTableFrame>;
}

function OpportunityDetail({ row, onClose, onOpenTicker }: { row: OpportunityDecisionRow | null; onClose: () => void; onOpenTicker: OpenTicker }) {
  return <Sheet open={Boolean(row)} onOpenChange={(open) => !open && onClose()}><SheetContent side="right" className="w-full overflow-y-auto sm:max-w-2xl"><SheetHeader><SheetTitle>{row?.opportunity_episode_id ?? "Opportunity episode"}</SheetTitle><SheetDescription>Canonical rank fields for one published episode. Open the ticker decision for the immutable plan and expression evidence.</SheetDescription></SheetHeader>{row ? <div className="space-y-5 py-5"><Card><CardHeader><CardTitle>Current ranked decision</CardTitle></CardHeader><CardContent className="space-y-3 text-sm"><div className="flex flex-wrap gap-2"><StatusBadge tone={toneFromText(row.availability_status)}>{titleLabel(row.availability_status)}</StatusBadge>{row.selected_expression_kind ? <StatusBadge tone="info">{row.selected_expression_kind}</StatusBadge> : null}</div><p><strong>Ticker:</strong> {row.ticker}</p><p><strong>Trade utility:</strong> {utilityRange(row.trade_utility)}</p><p><strong>Research rank:</strong> {rank(row.research_rank)} · <strong>Trade rank:</strong> {rank(row.trade_rank)}</p><p><strong>Blocker:</strong> {row.primary_blocker ?? row.trade_rank_unavailable_reason ?? "None recorded."}</p>{row.selected_expression_kind ? null : <DataFieldStateNotice state={missingFieldState({ field: "selected_expression_kind", source: "opportunity_rank", reason: "selected_expression_missing", nextAction: "Open the canonical ticker decision and refresh the published rank." })} />}{row.ticker ? <Button type="button" variant="outline" onClick={() => onOpenTicker(row.ticker)}>Open canonical ticker</Button> : null}</CardContent></Card><DataTableFrame title="Expression comparison"><div className="p-4"><DataFieldStateNotice state={missingFieldState({ field: "expression_comparison", source: "ticker_decision", reason: "opportunity_rank_does_not_own_expression_alternatives", nextAction: "Open the canonical ticker decision to inspect stored expression evidence." })} /></div></DataTableFrame></div> : null}</SheetContent></Sheet>;
}

function readSavedView(): SavedView { if (typeof window === "undefined") return "episodes"; return window.localStorage.getItem(OPPORTUNITIES_SAVED_VIEW_KEY) === "screener" ? "screener" : "episodes"; }
function episodeIdentity(row: OpportunityDecisionRow, index: number): string { return `${row.opportunity_episode_id}:${index}`; }
function rank(value: number | null | undefined): string { return typeof value === "number" && Number.isFinite(value) ? `#${value}` : "—"; }
function utilityRange(value: number | null | undefined): string { return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString(undefined, { maximumFractionDigits: 4 }) : "—"; }
function isOpportunityDecisionRow(row: RowRecord): boolean {
  return typeof row.ticker === "string"
    && typeof row.opportunity_episode_id === "string"
    && typeof row.rank_id === "string"
    && typeof row.decision_revision === "string"
    && typeof row.availability_status === "string"
    && Array.isArray(row.blockers);
}
