import { ExternalLink } from "lucide-react";

import { resolveTradingViewSymbol, tradingViewEmbedUrl } from "@/adapters/tradingView";
import { DataTableFrame, EvidenceList, StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import type { PanelData, RowRecord, TickerPayload } from "@/types";
import { rows } from "@/utils";
import { DataGridSection } from "./dataGridSection";
import { displayField, fullField, listField, symbolList, textField, toneFromText } from "./rowFormat";
import { WorkspacePage, type OpenTicker } from "./workspacePage";

export function TickerPage({ symbol, ticker, data, onOpenTicker }: { symbol: string; ticker: TickerPayload | null; data: PanelData; onOpenTicker: OpenTicker }) {
  const tickerSections = tickerTableSections(ticker);
  const thesisRows = rows(data.thesisMonitor).filter((row) => symbolList(row).includes(symbol));
  const title = ticker?.found === false ? `${symbol} not found` : symbol;
  return (
    <WorkspacePage eyebrow="Ticker dossier" title={title} subtitle="Ticker evidence, thesis state, decision context, and source-backed facts.">
      <TradingViewChart symbol={symbol} ticker={ticker} />
      {ticker?.decision_brief || ticker?.decision_snapshot ? (
        <div className="mb-4 grid min-w-0 gap-3 lg:grid-cols-2 [&>*]:min-w-0">
          {ticker.decision_brief && <DossierCard title="Decision Brief" row={ticker.decision_brief} />}
          {ticker.decision_snapshot && <DossierCard title="Decision Snapshot" row={ticker.decision_snapshot} />}
        </div>
      ) : null}
      <DataGridSection title="Thesis State" rows={thesisRows} onOpenTicker={onOpenTicker} />
      {tickerSections.map(([sectionTitle, sectionRows]) => <DataGridSection key={sectionTitle} title={sectionTitle} rows={sectionRows} onOpenTicker={onOpenTicker} />)}
    </WorkspacePage>
  );
}

function TradingViewChart({ symbol, ticker }: { symbol: string; ticker: TickerPayload | null }) {
  const tradingViewSymbol = resolveTradingViewSymbol(symbol, ticker);
  const chartUrl = tradingViewEmbedUrl(tradingViewSymbol);
  const externalUrl = `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(tradingViewSymbol)}`;
  return (
    <DataTableFrame
      title="Chart"
      action={
        <Button asChild type="button" variant="outline" size="sm">
          <a href={externalUrl} target="_blank" rel="noreferrer"><ExternalLink /> Open TradingView</a>
        </Button>
      }
    >
      <div className="h-[360px] w-full bg-muted/30 sm:h-[440px]">
        <iframe
          title={`${symbol} TradingView chart`}
          src={chartUrl}
          className="h-full w-full border-0"
          loading="lazy"
          referrerPolicy="no-referrer-when-downgrade"
          sandbox="allow-scripts allow-same-origin allow-popups allow-forms"
        />
      </div>
    </DataTableFrame>
  );
}

function DossierCard({ title, row }: { title: string; row: RowRecord }) {
  const evidence = listField(row, ["evidence", "evidence_links", "sources"]);
  const status = displayField(row, ["status", "decision", "action_grade", "grade"], "Loaded");
  return (
    <div className="min-w-0 rounded-lg border border-border bg-card p-4">
      <div className="mb-2 flex items-start justify-between gap-3">
        <h2 className="text-base font-semibold">{title}</h2>
        <StatusBadge tone={toneFromText(status)}>{status}</StatusBadge>
      </div>
      <p className="break-words text-sm leading-6">{dossierSummary(row)}</p>
      <div className="mt-3 text-sm text-muted-foreground"><EvidenceList items={evidence.slice(0, 4)} /></div>
    </div>
  );
}

function dossierSummary(row: RowRecord): string {
  const raw = fullField(row, ["summary", "thesis", "reason", "decision_basis"], "No summary");
  const parsed = parseJsonObject(raw);
  if (parsed) {
    const parts = [
      textField(parsed, ["summary", "decision_basis", "reason"]),
      textField(parsed, ["action_grade", "decision", "status"]),
      displayField(parsed, ["score", "decision_score"]),
      textField(parsed, ["next_action", "nextAction"]),
    ].filter((part) => part && part !== "-");
    return truncateText(parts.join(" | ") || JSON.stringify(parsed as Record<string, unknown>), 280);
  }
  return truncateText(raw, 280);
}

function parseJsonObject(value: string): RowRecord | null {
  if (!value.trim().startsWith("{")) return null;
  try {
    const parsed = JSON.parse(value) as unknown;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as RowRecord : null;
  } catch {
    return null;
  }
}

function truncateText(value: string, maxLength: number): string {
  return value.length > maxLength ? `${value.slice(0, maxLength - 1)}...` : value;
}

const tickerSectionOrder: Array<[string, keyof NonNullable<TickerPayload["tables"]>]> = [
  ["Decision Context", "decision_queue"],
  ["Candidate Screen", "candidates"],
  ["Universe Context", "universe_screen"],
  ["Quote", "quotes"],
  ["Technicals", "technicals"],
  ["SEPA", "sepa"],
  ["Liquidity", "liquidity"],
  ["Valuations", "valuations"],
  ["Earnings Setup", "earnings_setups"],
  ["Earnings", "earnings"],
  ["Analyst Estimates", "analyst_estimates"],
  ["Source Signals", "ticker_source_signals"],
  ["News", "news"],
  ["Disclosures", "disclosures"],
  ["Ownership Consensus", "ownership_consensus"],
  ["Options Payoff", "options_payoff_scenarios"],
  ["Options Chain", "options_chain"],
  ["TradingView Watchlists", "tradingview_watchlists"],
  ["TradingView Alerts", "tradingview_alerts"],
];

function tickerTableSections(ticker: TickerPayload | null): Array<[string, RowRecord[]]> {
  const tables = ticker?.tables;
  if (!tables) return [];
  return tickerSectionOrder
    .map(([title, key]) => [title, compactRows(tables[key])] as [string, RowRecord[]])
    .filter(([, sectionRows]) => sectionRows.length > 0);
}

function compactRows(sectionRows: RowRecord[] | undefined): RowRecord[] {
  return (sectionRows ?? [])
    .map((row) => Object.fromEntries(Object.entries(row).filter(([, value]) => !isEmptyCell(value))) as RowRecord)
    .filter((row) => Object.keys(row).length > 0);
}

function isEmptyCell(value: RowRecord[string]): boolean {
  if (value === undefined || value === null || value === "") return true;
  if (Array.isArray(value)) return value.length === 0;
  return typeof value === "object" && Object.keys(value).length === 0;
}
