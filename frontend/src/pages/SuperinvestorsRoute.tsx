import { useEffect, useMemo, useState } from "react";
import { ExternalLink, Search } from "lucide-react";
import { loadSuperinvestorPortfolio } from "@/api/panel";
import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { DataGridSection } from "@/views/dataGridSection";
import { WorkspacePage } from "@/views/workspacePage";
import { DataTableFrame } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { rows } from "@/utils";
import type { RowRecord } from "@/types";

type Portfolio = RowRecord & { investor_key?: string; investor?: string; holdings?: RowRecord[]; filing_history?: RowRecord[]; latest_allocation_changes?: RowRecord[] };

export function selectPortfolio(portfolios: Portfolio[], selected: string | null): Portfolio | null {
  return portfolios.find((row) => portfolioIdentity(row) === selected) ?? portfolios[0] ?? null;
}

export function SuperinvestorsRoute() {
  const { data, openTicker } = useMarketData();
  usePanelScope("superinvestors");
  const portfolios = rows(data.superinvestorPortfolios) as Portfolio[];
  const [query, setQuery] = useState("");
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const selectedSummary = selectPortfolio(portfolios, selectedName);
  const selectedKey = portfolioIdentity(selectedSummary);
  const [detail, setDetail] = useState<Portfolio | null>(null);
  const [detailError, setDetailError] = useState("");
  useEffect(() => {
    if (!selectedKey) {
      setDetail(null);
      return;
    }
    const controller = new AbortController();
    setDetail(null);
    setDetailError("");
    loadSuperinvestorPortfolio(selectedKey, controller.signal)
      .then((row) => setDetail(row as Portfolio))
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setDetailError(error instanceof Error ? error.message : "Unable to load investor detail.");
      });
    return () => controller.abort();
  }, [selectedKey]);
  const selected = portfolioIdentity(detail) === selectedKey ? detail : selectedSummary;
  const directory = useMemo(() => portfolios.filter((row) => {
    const needle = query.trim().toLowerCase();
    if (!needle) return true;
    const searchable = [row.investor, row.filer_name];
    return searchable.some((value) => String(value ?? "").toLowerCase().includes(needle));
  }), [portfolios, query]);
  const holdings = Array.isArray(selected?.holdings) ? selected.holdings : [];
  const [holdingQuery, setHoldingQuery] = useState("");
  const visibleHoldings = useMemo(() => {
    const needle = holdingQuery.trim().toLowerCase();
    return holdings.filter((row) => !needle || [row.symbol, row.issuer, row.cusip].some((value) => String(value ?? "").toLowerCase().includes(needle))).slice(0, 250);
  }, [holdings, holdingQuery]);
  const [holdingKey, setHoldingKey] = useState<string | null>(null);
  const holding = holdings.find((row) => holdingIdentity(row) === holdingKey) ?? holdings[0] ?? null;
  const history = Array.isArray(holding?.["history"]) ? holding["history"] as RowRecord[] : [];

  return <WorkspacePage eyebrow="Disclosure tracking" title="Superinvestors" subtitle="SEC 13F portfolio snapshots. Disclosure history is context, not a real-time trade record." metrics={[
    ["Investors", portfolios.length.toLocaleString(), "13F portfolios", portfolios.length ? "good" : "muted"],
    ["Current holdings", holdings.length.toLocaleString(), selected ? String(selected.investor) : "select an investor", "info"],
    ["Portfolio date", String(selected?.event_date ?? "-"), "reported quarter end", "muted"],
  ]}>
    <div className="grid gap-4 xl:grid-cols-[280px_minmax(0,1fr)]">
      <DataTableFrame title="Investor directory" action={<div className="relative w-40"><Search className="pointer-events-none absolute left-2 top-2.5 size-3.5 text-muted-foreground" /><Input className="h-8 pl-7 text-xs" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search" /></div>}>
        <div className="max-h-[620px] overflow-auto p-2">{directory.map((row) => <button key={portfolioIdentity(row)} onClick={() => { setSelectedName(portfolioIdentity(row)); setHoldingKey(null); }} className={`w-full rounded-md px-3 py-3 text-left text-sm hover:bg-accent ${portfolioIdentity(selected) === portfolioIdentity(row) ? "bg-accent" : ""}`}><div className="font-medium">{String(row.investor)}</div><div className="mt-1 text-xs text-muted-foreground">{String(row.holdings_count ?? 0)} holdings · {String(row.event_date ?? "no filing")}</div></button>)}{!directory.length && <p className="p-3 text-sm text-muted-foreground">No tracked portfolios.</p>}</div>
      </DataTableFrame>
      <div className="space-y-4">{selected ? <>
        <DataTableFrame title={String(selected.investor)} action={selected.source_url ? <a className="inline-flex items-center gap-1 text-xs text-primary hover:underline" href={String(selected.source_url)} target="_blank" rel="noreferrer">SEC source <ExternalLink className="size-3" /></a> : undefined}>
          <div className="grid gap-4 p-4 md:grid-cols-3"><Metric label="Reported value" value={formatMoney(selected.reported_portfolio_value_usd)} /><Metric label="Quarter end" value={String(selected.event_date ?? "-")} /><Metric label="Filed" value={String(selected.filed_date ?? "-")} /></div>
          <PortfolioTimeline history={Array.isArray(selected.filing_history) ? selected.filing_history : []} />
        </DataTableFrame>
        {detailError ? <p className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">{detailError}</p> : null}
        <DataTableFrame title={`Holdings distribution and detail (${holdings.length.toLocaleString()})`} action={<div className="relative w-48"><Search className="pointer-events-none absolute left-2 top-2.5 size-3.5 text-muted-foreground" /><Input className="h-8 pl-7 text-xs" value={holdingQuery} onChange={(event) => setHoldingQuery(event.target.value)} placeholder="Ticker, issuer, or CUSIP" /></div>}><div className="grid gap-0 lg:grid-cols-[minmax(0,1fr)_minmax(320px,0.9fr)]"><div className="max-h-[540px] overflow-auto border-b lg:border-b-0 lg:border-r">{visibleHoldings.map((row, index) => <button key={`${holdingIdentity(row)}:${index}`} onClick={() => setHoldingKey(holdingIdentity(row))} className={`flex w-full items-center justify-between gap-3 border-b px-4 py-3 text-left text-sm hover:bg-accent ${holdingIdentity(holding) === holdingIdentity(row) ? "bg-accent" : ""}`}><span className="min-w-0"><strong>{row.symbol ? String(row.symbol) : String(row.issuer ?? row.cusip ?? "Unresolved")}</strong><span className="ml-2 text-xs text-muted-foreground">{String(row.symbol ? row.issuer ?? row.cusip ?? "" : row.cusip ?? "")}</span></span><span className="shrink-0 tabular-nums text-muted-foreground">{formatMoney(row.reported_value_usd)}</span></button>)}{holdings.length && !visibleHoldings.length ? <p className="p-4 text-sm text-muted-foreground">No holdings match this filter.</p> : null}{visibleHoldings.length === 250 ? <p className="p-3 text-xs text-muted-foreground">Showing the first 250 matches. Refine the filter to find another holding.</p> : null}</div><div className="p-4"><HoldingDetail holding={holding} onOpenTicker={openTicker} history={history} /></div></div></DataTableFrame>
        <DataGridSection title="Latest allocation changes" rows={(selected.latest_allocation_changes ?? []) as RowRecord[]} onOpenTicker={openTicker} />
        <p className="rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-xs leading-5 text-muted-foreground">{String(selected.methodology_caveat ?? "13F data is delayed and incomplete.")} Estimated retained-share price effect is not profit or investment return.</p>
      </> : <DataTableFrame title="Investor workspace"><p className="p-4 text-sm text-muted-foreground">No SEC 13F portfolios have been loaded yet.</p></DataTableFrame>}</div>
    </div>
    <DataGridSection title="Ownership consensus (top 25, secondary context)" rows={rows(data.ownershipConsensus).slice(0, 25)} onOpenTicker={openTicker} />
  </WorkspacePage>;
}

function HoldingDetail({ holding, history, onOpenTicker }: { holding: RowRecord | null; history: RowRecord[]; onOpenTicker: (symbol: string) => void; }) {
  if (!holding) return <p className="text-sm text-muted-foreground">Select a holding.</p>;
  const effect = holding["estimated_retained_share_price_effect"] as RowRecord | null | undefined;
  return <><div className="flex items-center justify-between gap-3"><div className="min-w-0"><h3 className="font-semibold">{String(holding.symbol ?? holding.issuer ?? holding.cusip ?? "Holding")}</h3><p className="text-xs text-muted-foreground">{String(holding.issuer ?? "Unresolved issuer")} · CUSIP {String(holding.cusip ?? "-")}</p></div>{holding.symbol ? <Button variant="outline" size="sm" onClick={() => onOpenTicker(String(holding.symbol))}>Open ticker</Button> : null}</div><dl className="mt-4 grid grid-cols-2 gap-3 text-sm"><Metric label="Shares" value={Number(holding.shares ?? 0).toLocaleString()} /><Metric label="Reported value" value={formatMoney(holding.reported_value_usd)} /><Metric label="Implied price" value={formatMoney(holding.implied_price)} /><Metric label="Retained-share effect" value={effect ? formatMoney(effect.usd) : "Unavailable"} /></dl><div className="mt-5"><p className="text-xs font-medium uppercase text-muted-foreground">Holding history over time</p><div className="mt-2 overflow-x-auto"><table className="w-full min-w-[430px] text-xs"><thead className="text-left text-muted-foreground"><tr><th className="py-2 pr-3">Quarter</th><th className="py-2 pr-3 text-right">Shares</th><th className="py-2 pr-3 text-right">Value</th><th className="py-2 pr-3 text-right">Implied price</th><th className="py-2 text-right">Cumulative effect</th></tr></thead><tbody>{history.map((point, index) => <tr key={index} className="border-t"><td className="py-2 pr-3">{String(point.event_date ?? "-")}</td><td className="py-2 pr-3 text-right tabular-nums">{Number(point.shares ?? 0).toLocaleString()}</td><td className="py-2 pr-3 text-right tabular-nums">{formatMoney(point.reported_value_usd)}</td><td className="py-2 pr-3 text-right tabular-nums">{formatMoney(point.implied_price)}</td><td className="py-2 text-right tabular-nums">{point.estimated_retained_share_price_effect_usd === null || point.estimated_retained_share_price_effect_usd === undefined ? "-" : formatMoney(point.estimated_retained_share_price_effect_usd)}</td></tr>)}</tbody></table></div></div><p className="mt-4 text-xs leading-5 text-muted-foreground">{effect ? `${String(effect.method ?? "")} This estimate is not realized profit or total return.` : "Estimated retained-share price effect unavailable: fewer than two comparable disclosures or incomplete reported values/shares."}</p></>;
}
function PortfolioTimeline({ history }: { history: RowRecord[] }) { return <div className="border-t p-4"><p className="mb-3 text-xs font-medium uppercase text-muted-foreground">Reported filing history</p><div className="flex gap-2 overflow-x-auto">{history.map((point, index) => <div key={index} className="min-w-36 rounded border p-3 text-sm"><div className="font-medium">{formatMoney(point.reported_value_usd)}</div><div className="text-xs text-muted-foreground">{String(point.event_date ?? "-")}</div><div className="text-xs text-muted-foreground">{String(point.holdings_count ?? 0)} holdings</div></div>)}</div></div>; }
function Metric({ label, value }: { label: string; value: string }) { return <div><dt className="text-xs uppercase text-muted-foreground">{label}</dt><dd className="mt-1 font-medium">{value}</dd></div>; }
function portfolioIdentity(row: Portfolio | null | undefined) { return String(row?.investor_key ?? row?.investor ?? ""); }
function holdingIdentity(row: RowRecord | null | undefined) { return `${row?.symbol ?? ""}:${row?.cusip ?? ""}:${row?.issuer ?? ""}:${row?.put_call ?? ""}`; }
function formatMoney(dollars: unknown) { if (dollars === null || dollars === undefined || dollars === "") return "-"; const value = Number(dollars); return Number.isFinite(value) ? new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1 }).format(value) : "-"; }
