import { Database, FileText, Mic, Newspaper, Radio, Search, TrendingDown, TrendingUp } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import type { JsonValue, RowRecord } from "@/types";
import { DataTableFrame, StatusBadge } from "@/components/market/workstation";
import { rows, tickerSymbol } from "@/utils";
import { displayField, numberField, textField, titleLabel, toneFromText } from "@/views/rowFormat";
import { WorkspacePage, type MetricSpec } from "@/views/workspacePage";

type SourceFamily = "all" | "filing" | "transcript" | "podcast" | "blog" | "private_graph" | "market_data" | "other";
type RankingMode = "discussed" | "bullish" | "bearish" | "conviction";

const FAMILY_FILTERS: Array<{ id: SourceFamily; label: string; icon: typeof Database }> = [
  { id: "all", label: "All", icon: Database },
  { id: "filing", label: "Filings", icon: FileText },
  { id: "transcript", label: "Transcripts", icon: Newspaper },
  { id: "podcast", label: "Podcasts", icon: Radio },
  { id: "blog", label: "Blogs", icon: Newspaper },
  { id: "private_graph", label: "X / Arco", icon: Mic },
  { id: "market_data", label: "Market Data", icon: TrendingUp },
];

const RANKING_MODES: Array<{ id: RankingMode; label: string; icon: typeof TrendingUp }> = [
  { id: "discussed", label: "Most Discussed", icon: Database },
  { id: "bullish", label: "Bullish", icon: TrendingUp },
  { id: "bearish", label: "Bearish", icon: TrendingDown },
  { id: "conviction", label: "High Conviction", icon: Radio },
];

export function SourcesRoute() {
  const { data, openTicker } = useMarketData();
  const [rankingMode, setRankingMode] = useState<RankingMode>("discussed");
  const [family, setFamily] = useState<SourceFamily>("all");
  const [query, setQuery] = useState("");
  usePanelScope("sources");

  const rankingRows = useMemo(() => rows(data.sourceTickerRankings), [data.sourceTickerRankings]);
  const sourceRows = useMemo(() => rows(data.sources), [data.sources]);
  const sourceConsensus = useMemo(() => rows(data.sourceConsensus), [data.sourceConsensus]);
  const rankedTickers = useMemo(() => rankTickerRows(rankingRows, rankingMode), [rankingRows, rankingMode]);
  const filteredSources = useMemo(() => filterSources(sourceRows, family, query), [sourceRows, family, query]);

  const metrics: MetricSpec[] = [
    ["Ranked Tickers", rankingRows.length.toLocaleString(), "symbols with source consensus", rankingRows.length ? "good" : "warn"],
    ["Source Consensus", sourceConsensus.length.toLocaleString(), "source-level views", sourceConsensus.length ? "good" : "warn"],
    ["Ticker Links", rows(data.tickerSourceSignals).length.toLocaleString(), "loaded in current scope", rows(data.tickerSourceSignals).length ? "good" : "muted"],
    ["Sources", sourceRows.length.toLocaleString(), `${sourceFamilies(sourceRows)} families configured`, sourceRows.length ? "good" : "muted"],
  ];

  return (
    <WorkspacePage
      eyebrow="Evidence"
      title="Sources"
      subtitle="Ticker rankings and source consensus from independent source contributions."
      metrics={metrics}
      actions={
        <div className="relative w-full min-w-64 sm:w-80">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input className="pl-9" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter source list" aria-label="Filter source list" />
        </div>
      }
    >
      <div className="flex gap-2 overflow-x-auto pb-1">
        {RANKING_MODES.map((item) => {
          const Icon = item.icon;
          return (
            <Button key={item.id} type="button" variant={rankingMode === item.id ? "default" : "outline"} size="sm" onClick={() => setRankingMode(item.id)} className="shrink-0">
              <Icon />
              {item.label}
            </Button>
          );
        })}
      </div>

      <TickerRankingTable mode={rankingMode} rows={rankedTickers.slice(0, 40)} onOpenTicker={openTicker} />
      <SourceConsensusTable rows={sourceConsensus.slice(0, 60)} onOpenTicker={openTicker} />

      <div className="flex gap-2 overflow-x-auto pb-1 pt-2">
        {FAMILY_FILTERS.map((item) => {
          const Icon = item.icon;
          const count = item.id === "all" ? sourceRows.length : sourceRows.filter((row) => normalizeFamily(textField(row, ["source_family", "content_type"], "other")) === item.id).length;
          return (
            <Button key={item.id} type="button" variant={family === item.id ? "default" : "outline"} size="sm" onClick={() => setFamily(item.id)} className="shrink-0">
              <Icon />
              {item.label}
              <span className="tabular-nums text-xs opacity-75">{count.toLocaleString()}</span>
            </Button>
          );
        })}
      </div>
      <SourceDirectoryTable rows={filteredSources.slice(0, 120)} />
    </WorkspacePage>
  );
}

function TickerRankingTable({ mode, rows: rankingRows, onOpenTicker }: { mode: RankingMode; rows: RowRecord[]; onOpenTicker: (symbol: string) => void }) {
  return (
    <DataTableFrame title={`${titleLabel(mode)} Ticker Ranking`}>
      <table className="w-full min-w-[980px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <th className="px-3 py-3">Rank</th>
            <th className="px-3 py-3">Ticker</th>
            <th className="px-3 py-3">Sources</th>
            <th className="px-3 py-3">Signals</th>
            <th className="px-3 py-3">Bullish</th>
            <th className="px-3 py-3">Bearish</th>
            <th className="px-3 py-3">Net</th>
            <th className="px-3 py-3">Confidence</th>
            <th className="px-3 py-3">Source Names</th>
            <th className="px-3 py-3">Latest</th>
            <th className="px-3 py-3">Open</th>
          </tr>
        </thead>
        <tbody>
          {rankingRows.map((row, index) => {
            const symbol = textField(row, ["symbol"], "");
            const net = contractNumber(row, "net_consensus");
            const netValue = net.kind === "value" ? net.value : 0;
            return (
              <tr key={symbol || index} className="border-b border-border align-top hover:bg-accent/40">
                <td className="px-3 py-3 text-muted-foreground tabular-nums">#{index + 1}</td>
                <td className="px-3 py-3 font-semibold">{symbol}</td>
                <td className="px-3 py-3 tabular-nums">{renderContractNumber(row, "source_count")}</td>
                <td className="px-3 py-3 tabular-nums">{renderContractNumber(row, "signal_count")}</td>
                <td className="px-3 py-3 tabular-nums">{renderContractNumber(row, "bullish_count")}</td>
                <td className="px-3 py-3 tabular-nums">{renderContractNumber(row, "bearish_count")}</td>
                <td className="px-3 py-3"><StatusBadge tone={netValue > 0 ? "good" : netValue < 0 ? "bad" : net.kind === "value" ? "muted" : "warn"}>{renderContractSigned(net)}</StatusBadge></td>
                <td className="px-3 py-3 tabular-nums">{renderContractConfidence(row, "avg_confidence")}</td>
                <td className="max-w-[320px] px-3 py-3 text-muted-foreground">{stringsFromValue(row.source_names).slice(0, 5).join(", ") || "-"}</td>
                <td className="px-3 py-3 whitespace-nowrap text-muted-foreground">{formatDate(textField(row, ["latest_at"], ""))}</td>
                <td className="px-3 py-2">
                  {symbol ? <Button type="button" variant="ghost" size="sm" onClick={() => onOpenTicker(symbol)}>{symbol}</Button> : <span className="text-muted-foreground">-</span>}
                </td>
              </tr>
            );
          })}
          {!rankingRows.length ? <EmptyRow colSpan={11} text="No ticker ranking rows available." /> : null}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function SourceConsensusTable({ rows: consensusRows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: (symbol: string) => void }) {
  return (
    <DataTableFrame title="Source Consensus">
      <table className="w-full min-w-[980px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <th className="px-3 py-3">Source</th>
            <th className="px-3 py-3">Type</th>
            <th className="px-3 py-3">Items</th>
            <th className="px-3 py-3">Tickers</th>
            <th className="px-3 py-3">Net</th>
            <th className="px-3 py-3">Bullish Tickers</th>
            <th className="px-3 py-3">Bearish Tickers</th>
            <th className="px-3 py-3">State</th>
          </tr>
        </thead>
        <tbody>
          {consensusRows.map((row, index) => {
            const bullish = stringsFromValue(row.bullish_symbols).slice(0, 6);
            const bearish = stringsFromValue(row.bearish_symbols).slice(0, 6);
            const net = numberField(row, ["net_consensus"], bullish.length - bearish.length);
            return (
              <tr key={`${textField(row, ["source_id", "source_name"], "source")}-${index}`} className="border-b border-border align-top hover:bg-accent/40">
                <td className="max-w-[260px] px-3 py-3 font-medium">{textField(row, ["source_name", "source"], "Source")}</td>
                <td className="px-3 py-3 text-muted-foreground">{titleLabel(textField(row, ["content_type", "source_family", "source_type"], "source"))}</td>
                <td className="px-3 py-3 tabular-nums">{numberField(row, ["items_count", "mentions"], 0).toLocaleString()}</td>
                <td className="px-3 py-3 tabular-nums">{numberField(row, ["tickers_count", "symbols_count"], 0).toLocaleString()}</td>
                <td className="px-3 py-3"><StatusBadge tone={net > 0 ? "good" : net < 0 ? "bad" : "muted"}>{formatSigned(net)}</StatusBadge></td>
                <td className="px-3 py-3"><SymbolButtons symbols={bullish} onOpenTicker={onOpenTicker} /></td>
                <td className="px-3 py-3"><SymbolButtons symbols={bearish} onOpenTicker={onOpenTicker} /></td>
                <td className="px-3 py-3"><StatusBadge tone={textField(row, ["recommendation"], "") === "loaded" ? "good" : "info"}>{displayField(row, ["recommendation", "freshness", "origin"], "loaded")}</StatusBadge></td>
              </tr>
            );
          })}
          {!consensusRows.length ? <EmptyRow colSpan={8} text="No source consensus rows available." /> : null}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function SourceDirectoryTable({ rows: sourceRows }: { rows: RowRecord[] }) {
  return (
    <DataTableFrame title="Source Directory">
      <table className="w-full min-w-[980px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <th className="px-3 py-3">Source</th>
            <th className="px-3 py-3">Family</th>
            <th className="px-3 py-3">Mode</th>
            <th className="px-3 py-3">Items</th>
            <th className="px-3 py-3">Tickers</th>
            <th className="px-3 py-3">Signals</th>
            <th className="px-3 py-3">Access</th>
            <th className="px-3 py-3">State</th>
            <th className="px-3 py-3">Health</th>
          </tr>
        </thead>
        <tbody>
          {sourceRows.map((row, index) => {
            const enabled = booleanValue(row.enabled) || booleanValue(row.is_followed);
            const signalCount = numberField(row, ["signals_count", "signal_count"], 0);
            const itemCount = numberField(row, ["items_count", "item_count"], 0);
            const health = displayField(row, ["latest_run_status", "freshness", "health", "status"], "not_loaded");
            return (
              <tr key={`${textField(row, ["source_id", "source_name"], "source")}-${index}`} className="border-b border-border align-top hover:bg-accent/40">
                <td className="max-w-[300px] px-3 py-3">
                  <div className="font-medium">{textField(row, ["source_name", "source"], "Source")}</div>
                  <div className="line-clamp-1 text-xs text-muted-foreground">{textField(row, ["notes", "detail"], "")}</div>
                </td>
                <td className="px-3 py-3 text-muted-foreground">{titleLabel(textField(row, ["source_family", "content_type"], "source"))}</td>
                <td className="px-3 py-3 text-muted-foreground">{titleLabel(textField(row, ["ingestion_mode", "source_kind"], "source"))}</td>
                <td className="px-3 py-3 tabular-nums">{itemCount.toLocaleString()}</td>
                <td className="px-3 py-3 tabular-nums">{numberField(row, ["tickers_count", "ticker_count"], 0).toLocaleString()}</td>
                <td className="px-3 py-3 tabular-nums">{signalCount.toLocaleString()}</td>
                <td className="px-3 py-3 text-muted-foreground">{titleLabel(textField(row, ["raw_access", "origin"], "local"))}</td>
                <td className="px-3 py-3"><StatusBadge tone={enabled ? "good" : "muted"}>{enabled ? "followed" : "candidate"}</StatusBadge></td>
                <td className="px-3 py-3"><StatusBadge tone={toneFromText(health)}>{titleLabel(health)}</StatusBadge></td>
              </tr>
            );
          })}
          {!sourceRows.length ? <EmptyRow colSpan={9} text="No sources match the current filter." /> : null}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function SymbolButtons({ symbols, onOpenTicker }: { symbols: string[]; onOpenTicker: (symbol: string) => void }) {
  if (!symbols.length) return <span className="text-muted-foreground">-</span>;
  return (
    <div className="flex flex-wrap gap-1">
      {symbols.map((symbol) => (
        <Button key={symbol} type="button" variant="outline" size="sm" className="h-8 min-h-8 px-2" onClick={() => onOpenTicker(symbol)}>
          {symbol}
        </Button>
      ))}
    </div>
  );
}

function EmptyRow({ colSpan, text }: { colSpan: number; text: string }) {
  return (
    <tr>
      <td className="px-4 py-6 text-sm text-muted-foreground" colSpan={colSpan}>{text}</td>
    </tr>
  );
}

function rankTickerRows(rankingRows: RowRecord[], mode: RankingMode): RowRecord[] {
  const copy = [...rankingRows];
  return copy.sort((a, b) => {
    if (mode === "bullish") {
      return contractNumberValue(b, "bullish_count") - contractNumberValue(a, "bullish_count")
        || contractNumberValue(b, "net_consensus") - contractNumberValue(a, "net_consensus")
        || contractNumberValue(b, "signal_count") - contractNumberValue(a, "signal_count")
        || textField(a, ["symbol"], "").localeCompare(textField(b, ["symbol"], ""));
    }
    if (mode === "bearish") {
      return contractNumberValue(b, "bearish_count") - contractNumberValue(a, "bearish_count")
        || contractNumberValue(a, "net_consensus") - contractNumberValue(b, "net_consensus")
        || contractNumberValue(b, "signal_count") - contractNumberValue(a, "signal_count")
        || textField(a, ["symbol"], "").localeCompare(textField(b, ["symbol"], ""));
    }
    if (mode === "conviction") {
      return contractNumberValue(b, "avg_confidence") - contractNumberValue(a, "avg_confidence")
        || contractNumberValue(b, "source_count") - contractNumberValue(a, "source_count")
        || contractNumberValue(b, "signal_count") - contractNumberValue(a, "signal_count")
        || textField(a, ["symbol"], "").localeCompare(textField(b, ["symbol"], ""));
    }
    return contractNumberValue(b, "source_count") - contractNumberValue(a, "source_count")
      || contractNumberValue(b, "signal_count") - contractNumberValue(a, "signal_count")
      || contractNumberValue(b, "net_consensus") - contractNumberValue(a, "net_consensus")
      || textField(a, ["symbol"], "").localeCompare(textField(b, ["symbol"], ""));
  });
}

function filterSources(sourceRows: RowRecord[], family: SourceFamily, query: string): RowRecord[] {
  const needle = query.trim().toLowerCase();
  return sourceRows.filter((row) => {
    const sourceFamily = normalizeFamily(textField(row, ["source_family", "content_type"], "other"));
    if (family !== "all" && sourceFamily !== family) return false;
    if (!needle) return true;
    return [
      textField(row, ["source_name", "source"], ""),
      textField(row, ["source_family", "content_type"], ""),
      textField(row, ["source_kind", "ingestion_mode"], ""),
      textField(row, ["raw_access", "origin"], ""),
      textField(row, ["notes", "detail"], ""),
    ].join(" ").toLowerCase().includes(needle);
  });
}

function stringsFromValue(value: JsonValue | undefined): string[] {
  if (Array.isArray(value)) {
    return value.map((item) => typeof item === "string" ? (tickerSymbol(item) || item) : String(item)).filter(Boolean);
  }
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return [];
    if (trimmed.startsWith("[")) {
      try {
        return stringsFromValue(JSON.parse(trimmed) as JsonValue);
      } catch {
        return [];
      }
    }
    return trimmed.split(/[;|,]/).map((item) => tickerSymbol(item) || item.trim()).filter(Boolean);
  }
  return [];
}

function normalizeFamily(value: string): SourceFamily {
  const normalized = value.toLowerCase();
  if (normalized.includes("filing")) return "filing";
  if (normalized.includes("transcript")) return "transcript";
  if (normalized.includes("podcast")) return "podcast";
  if (normalized.includes("blog") || normalized.includes("newsletter") || normalized.includes("memo")) return "blog";
  if (normalized.includes("private") || normalized.includes("social") || normalized.includes("tweet")) return "private_graph";
  if (normalized.includes("market") || normalized.includes("provider") || normalized.includes("estimate") || normalized.includes("event")) return "market_data";
  return "other";
}

function booleanValue(value: JsonValue | undefined): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") return ["true", "yes", "1", "followed", "enabled"].includes(value.trim().toLowerCase());
  return false;
}

function sourceFamilies(sourceRows: RowRecord[]): number {
  return new Set(sourceRows.map((row) => normalizeFamily(textField(row, ["source_family", "content_type"], "other")))).size;
}

function formatSigned(value: number): ReactNode {
  if (!value) return "0";
  return value > 0 ? `+${value}` : String(value);
}

function formatDate(value: string): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

type ContractNumber = { kind: "value"; value: number } | { kind: "missing" } | { kind: "invalid" };

function contractNumber(row: RowRecord, key: string): ContractNumber {
  if (!(key in row)) return { kind: "missing" };
  const value = row[key];
  if (typeof value === "number" && Number.isFinite(value)) return { kind: "value", value };
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value.trim().replace(/[$,%_,]/g, ""));
    if (Number.isFinite(parsed)) return { kind: "value", value: parsed };
  }
  if (value === null || value === undefined || value === "") return { kind: "invalid" };
  return { kind: "invalid" };
}

function contractNumberValue(row: RowRecord, key: string): number {
  const result = contractNumber(row, key);
  return result.kind === "value" ? result.value : 0;
}

function renderContractNumber(row: RowRecord, key: string): ReactNode {
  const result = contractNumber(row, key);
  if (result.kind === "missing") return <StatusBadge tone="warn">missing</StatusBadge>;
  if (result.kind === "invalid") return "-";
  return result.value.toLocaleString();
}

function renderContractSigned(result: ContractNumber): ReactNode {
  if (result.kind === "missing") return "missing";
  if (result.kind === "invalid") return "-";
  return formatSigned(result.value);
}

function renderContractConfidence(row: RowRecord, key: string): ReactNode {
  const result = contractNumber(row, key);
  if (result.kind === "missing") return <StatusBadge tone="warn">missing</StatusBadge>;
  if (result.kind === "invalid") return "-";
  return result.value.toFixed(2);
}
