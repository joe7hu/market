import { Database, FileText, Mic, Newspaper, Radio, Search, TrendingUp } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import type { JsonValue, RowRecord } from "@/types";
import { StatusBadge, DataTableFrame } from "@/components/market/workstation";
import { rows, tickerSymbol } from "@/utils";
import { displayField, numberField, textField, titleLabel } from "@/views/rowFormat";
import { WorkspacePage, type MetricSpec } from "@/views/workspacePage";

type SourceFamily = "all" | "filing" | "transcript" | "podcast" | "blog" | "private_graph" | "market_data" | "other";

type SourceView = {
  id: string;
  name: string;
  family: SourceFamily;
  kind: string;
  enabled: boolean;
  origin: string;
  items: number;
  tickers: number;
  signals: number;
  freshness: string;
  access: string;
  mode: string;
  notes: string;
  topSymbols: string[];
};

type TickerConsensus = {
  symbol: string;
  sources: number;
  mentions: number;
  bullish: number;
  bearish: number;
  net: number;
  sourceNames: string[];
};

const FAMILY_FILTERS: Array<{ id: SourceFamily; label: string; icon: typeof Database }> = [
  { id: "all", label: "All", icon: Database },
  { id: "filing", label: "Filings", icon: FileText },
  { id: "transcript", label: "Transcripts", icon: Newspaper },
  { id: "podcast", label: "Podcasts", icon: Radio },
  { id: "blog", label: "Blogs", icon: Newspaper },
  { id: "private_graph", label: "X / Arco", icon: Mic },
  { id: "market_data", label: "Market Data", icon: TrendingUp },
];

export function SourcesRoute() {
  const { data, openTicker } = useMarketData();
  const [family, setFamily] = useState<SourceFamily>("all");
  const [query, setQuery] = useState("");
  usePanelScope("sources");

  const sourceViews = useMemo(() => buildSourceViews(rows(data.sources), rows(data.tickerSourceSignals), rows(data.sourceConsensus)), [data.sources, data.tickerSourceSignals, data.sourceConsensus]);
  const tickerConsensus = useMemo(() => buildTickerConsensus(rows(data.tickerSourceSignals), rows(data.sourceConsensus)), [data.tickerSourceSignals, data.sourceConsensus]);
  const visibleSources = useMemo(() => filterSources(sourceViews, family, query), [sourceViews, family, query]);

  const familiesLoaded = FAMILY_FILTERS.filter((item) => item.id !== "all" && sourceViews.some((source) => source.family === item.id)).length;
  const metrics: MetricSpec[] = [
    ["Sources", sourceViews.length.toLocaleString(), `${familiesLoaded} families configured`, sourceViews.length ? "good" : "warn"],
    ["Loaded Items", sum(sourceViews, "items").toLocaleString(), "canonical source items", sum(sourceViews, "items") ? "good" : "muted"],
    ["Ticker Links", sum(sourceViews, "signals").toLocaleString(), "per-source ticker signals", sum(sourceViews, "signals") ? "good" : "warn"],
    ["Consensus Names", tickerConsensus.length.toLocaleString(), "symbols with source support", tickerConsensus.length ? "good" : "muted"],
  ];

  return (
    <WorkspacePage
      eyebrow="Evidence"
      title="Sources"
      subtitle="Source directory, coverage, and ticker consensus organized around independent source contributions."
      metrics={metrics}
      actions={
        <div className="relative w-full min-w-64 sm:w-80">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input className="pl-9" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter sources" aria-label="Filter sources" />
        </div>
      }
    >
      <div className="flex gap-2 overflow-x-auto pb-1">
        {FAMILY_FILTERS.map((item) => {
          const Icon = item.icon;
          const count = item.id === "all" ? sourceViews.length : sourceViews.filter((source) => source.family === item.id).length;
          return (
            <Button key={item.id} type="button" variant={family === item.id ? "default" : "outline"} size="sm" onClick={() => setFamily(item.id)} className="shrink-0">
              <Icon />
              {item.label}
              <span className="tabular-nums text-xs opacity-75">{count.toLocaleString()}</span>
            </Button>
          );
        })}
      </div>

      <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {visibleSources.slice(0, 36).map((source, index) => <SourceCard key={source.id || source.name} source={source} rank={index + 1} />)}
        {!visibleSources.length ? (
          <Card className="md:col-span-2 xl:col-span-3">
            <CardContent className="p-6 text-sm text-muted-foreground">No sources match the current filter.</CardContent>
          </Card>
        ) : null}
      </section>

      <TickerConsensusTable rows={tickerConsensus.slice(0, 24)} onOpenTicker={openTicker} />
      <SourceConsensusTable rows={rows(data.sourceConsensus).slice(0, 32)} onOpenTicker={openTicker} />
    </WorkspacePage>
  );
}

function SourceCard({ source, rank }: { source: SourceView; rank: number }) {
  const tone = source.signals || source.items ? "good" : source.enabled ? "warn" : "muted";
  return (
    <Card className={cn("min-w-0 overflow-hidden", source.enabled && "border-l-4 border-l-primary")}>
      <CardContent className="p-4">
        <div className="mb-3 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="mb-1 flex items-center gap-2">
              <span className="text-xs font-semibold tabular-nums text-muted-foreground">#{rank}</span>
              <StatusBadge tone={tone}>{source.enabled ? "followed" : "candidate"}</StatusBadge>
            </div>
            <h2 className="truncate text-base font-semibold tracking-normal">{source.name}</h2>
            <p className="mt-1 truncate text-xs text-muted-foreground">{titleLabel(source.family)} / {titleLabel(source.kind || source.mode || "source")}</p>
          </div>
          <Badge variant="outline" className="shrink-0">{titleLabel(source.access || source.origin || "local")}</Badge>
        </div>

        <div className="grid grid-cols-3 gap-2 text-sm">
          <SourceCount label="Items" value={source.items} />
          <SourceCount label="Tickers" value={source.tickers} />
          <SourceCount label="Signals" value={source.signals} />
        </div>

        <div className="mt-3 flex min-h-7 flex-wrap gap-1.5">
          {source.topSymbols.length ? source.topSymbols.map((symbol) => <Badge key={symbol} variant="secondary">{symbol}</Badge>) : <span className="text-xs text-muted-foreground">No ticker links yet</span>}
        </div>

        {source.notes ? <p className="mt-3 line-clamp-2 text-xs leading-5 text-muted-foreground">{source.notes}</p> : null}
      </CardContent>
    </Card>
  );
}

function SourceCount({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-md border border-border bg-muted/30 p-2">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 font-semibold tabular-nums">{value.toLocaleString()}</div>
    </div>
  );
}

function TickerConsensusTable({ rows: consensusRows, onOpenTicker }: { rows: TickerConsensus[]; onOpenTicker: (symbol: string) => void }) {
  return (
    <DataTableFrame title="Top Ticker Consensus">
      <table className="w-full min-w-[780px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <th className="px-3 py-3">Symbol</th>
            <th className="px-3 py-3">Sources</th>
            <th className="px-3 py-3">Mentions</th>
            <th className="px-3 py-3">Net</th>
            <th className="px-3 py-3">Source Names</th>
            <th className="px-3 py-3">Open</th>
          </tr>
        </thead>
        <tbody>
          {consensusRows.map((row) => (
            <tr key={row.symbol} className="border-b border-border align-top hover:bg-accent/40">
              <td className="px-3 py-3 font-semibold">{row.symbol}</td>
              <td className="px-3 py-3 tabular-nums">{row.sources}</td>
              <td className="px-3 py-3 tabular-nums">{row.mentions}</td>
              <td className="px-3 py-3"><StatusBadge tone={row.net > 0 ? "good" : row.net < 0 ? "bad" : "muted"}>{formatSigned(row.net)}</StatusBadge></td>
              <td className="max-w-[520px] px-3 py-3 text-muted-foreground">{row.sourceNames.slice(0, 5).join(", ") || "-"}</td>
              <td className="px-3 py-2">
                <Button type="button" variant="ghost" size="sm" onClick={() => onOpenTicker(row.symbol)}>{row.symbol}</Button>
              </td>
            </tr>
          ))}
          {!consensusRows.length ? (
            <tr>
              <td className="px-4 py-6 text-sm text-muted-foreground" colSpan={6}>No ticker consensus rows available.</td>
            </tr>
          ) : null}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function SourceConsensusTable({ rows: consensusRows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: (symbol: string) => void }) {
  return (
    <DataTableFrame title="Source Consensus">
      <table className="w-full min-w-[900px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <th className="px-3 py-3">Source</th>
            <th className="px-3 py-3">Type</th>
            <th className="px-3 py-3">Items</th>
            <th className="px-3 py-3">Tickers</th>
            <th className="px-3 py-3">Bullish</th>
            <th className="px-3 py-3">Bearish</th>
            <th className="px-3 py-3">State</th>
          </tr>
        </thead>
        <tbody>
          {consensusRows.map((row, index) => {
            const bullish = stringsFromValue(row.bullish_symbols).slice(0, 5);
            const bearish = stringsFromValue(row.bearish_symbols).slice(0, 5);
            return (
              <tr key={`${textField(row, ["source_id", "source_name"], "source")}-${index}`} className="border-b border-border align-top hover:bg-accent/40">
                <td className="max-w-[260px] px-3 py-3 font-medium">{textField(row, ["source_name", "source"], "Source")}</td>
                <td className="px-3 py-3 text-muted-foreground">{titleLabel(textField(row, ["content_type", "source_family", "source_type"], "source"))}</td>
                <td className="px-3 py-3 tabular-nums">{numberField(row, ["items_count", "mentions"], 0).toLocaleString()}</td>
                <td className="px-3 py-3 tabular-nums">{numberField(row, ["tickers_count", "symbols_count"], 0).toLocaleString()}</td>
                <td className="px-3 py-3"><SymbolBadges symbols={bullish} onOpenTicker={onOpenTicker} /></td>
                <td className="px-3 py-3"><SymbolBadges symbols={bearish} onOpenTicker={onOpenTicker} /></td>
                <td className="px-3 py-3"><StatusBadge tone={textField(row, ["recommendation"], "") === "loaded" ? "good" : "info"}>{displayField(row, ["recommendation", "freshness", "origin"], "loaded")}</StatusBadge></td>
              </tr>
            );
          })}
          {!consensusRows.length ? (
            <tr>
              <td className="px-4 py-6 text-sm text-muted-foreground" colSpan={7}>No source consensus rows available.</td>
            </tr>
          ) : null}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

function SymbolBadges({ symbols, onOpenTicker }: { symbols: string[]; onOpenTicker: (symbol: string) => void }) {
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

function buildSourceViews(sourceRows: RowRecord[], signalRows: RowRecord[], consensusRows: RowRecord[]): SourceView[] {
  const symbolsBySource = new Map<string, Map<string, number>>();
  for (const row of signalRows) {
    const sourceId = textField(row, ["source_id", "source_name"], "");
    const symbol = tickerSymbol(row.symbol);
    if (!sourceId || !symbol) continue;
    const bucket = symbolsBySource.get(sourceId) ?? new Map<string, number>();
    bucket.set(symbol, (bucket.get(symbol) ?? 0) + 1);
    symbolsBySource.set(sourceId, bucket);
  }
  for (const row of consensusRows) {
    const sourceId = textField(row, ["source_id", "source_name"], "");
    if (!sourceId) continue;
    const bucket = symbolsBySource.get(sourceId) ?? new Map<string, number>();
    for (const symbol of [...stringsFromValue(row.bullish_symbols), ...stringsFromValue(row.bearish_symbols)]) {
      bucket.set(symbol, (bucket.get(symbol) ?? 0) + 1);
    }
    symbolsBySource.set(sourceId, bucket);
  }

  return sourceRows
    .map((row) => {
      const id = textField(row, ["source_id", "source_name"], "");
      const family = normalizeFamily(textField(row, ["source_family", "content_type"], "other"));
      const symbols = [...(symbolsBySource.get(id)?.entries() ?? [])].sort((a, b) => b[1] - a[1]).map(([symbol]) => symbol);
      return {
        id,
        name: textField(row, ["source_name", "source"], "Source"),
        family,
        kind: textField(row, ["source_kind", "content_type"], ""),
        enabled: booleanValue(row.enabled) || booleanValue(row.is_followed),
        origin: textField(row, ["origin"], ""),
        items: numberField(row, ["items_count", "item_count"], 0),
        tickers: numberField(row, ["tickers_count", "ticker_count"], 0),
        signals: numberField(row, ["signals_count", "signal_count"], 0),
        freshness: textField(row, ["freshness"], ""),
        access: textField(row, ["raw_access"], ""),
        mode: textField(row, ["ingestion_mode"], ""),
        notes: textField(row, ["notes", "detail"], ""),
        topSymbols: symbols.slice(0, 6),
      };
    })
    .sort((a, b) => Number(b.enabled) - Number(a.enabled) || b.items - a.items || b.signals - a.signals || a.name.localeCompare(b.name));
}

function buildTickerConsensus(signalRows: RowRecord[], consensusRows: RowRecord[]): TickerConsensus[] {
  const bySymbol = new Map<string, TickerConsensus & { sourceSet: Set<string> }>();
  const entry = (symbol: string) => {
    const current = bySymbol.get(symbol) ?? { symbol, sources: 0, mentions: 0, bullish: 0, bearish: 0, net: 0, sourceNames: [], sourceSet: new Set<string>() };
    bySymbol.set(symbol, current);
    return current;
  };

  for (const row of signalRows) {
    const symbol = tickerSymbol(row.symbol);
    if (!symbol) continue;
    const item = entry(symbol);
    const sourceName = textField(row, ["source_name", "source_id"], "Source");
    item.mentions += 1;
    item.sourceSet.add(sourceName);
    const stance = `${textField(row, ["sentiment", "recommendation", "signal_type"], "")} ${displayField(row, ["score", "confidence"], "")}`.toLowerCase();
    if (stance.includes("bear") || stance.includes("short") || stance.includes("risk")) item.bearish += 1;
    else item.bullish += 1;
  }

  for (const row of consensusRows) {
    const sourceName = textField(row, ["source_name", "source"], "Source");
    for (const symbol of stringsFromValue(row.bullish_symbols)) {
      const item = entry(symbol);
      item.bullish += 1;
      item.mentions += 1;
      item.sourceSet.add(sourceName);
    }
    for (const symbol of stringsFromValue(row.bearish_symbols)) {
      const item = entry(symbol);
      item.bearish += 1;
      item.mentions += 1;
      item.sourceSet.add(sourceName);
    }
  }

  return [...bySymbol.values()]
    .map(({ sourceSet, ...row }) => ({ ...row, sources: sourceSet.size, sourceNames: [...sourceSet].sort(), net: row.bullish - row.bearish }))
    .sort((a, b) => b.sources - a.sources || b.mentions - a.mentions || b.net - a.net || a.symbol.localeCompare(b.symbol));
}

function filterSources(sourceViews: SourceView[], family: SourceFamily, query: string): SourceView[] {
  const needle = query.trim().toLowerCase();
  return sourceViews.filter((source) => {
    const familyMatch = family === "all" || source.family === family;
    if (!familyMatch) return false;
    if (!needle) return true;
    return [source.name, source.family, source.kind, source.origin, source.mode, source.access, ...source.topSymbols].join(" ").toLowerCase().includes(needle);
  });
}

function stringsFromValue(value: JsonValue | undefined): string[] {
  if (Array.isArray(value)) return value.map((item) => tickerSymbol(item)).filter(Boolean);
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
    return trimmed.split(/[,\s;|]+/).map((item) => tickerSymbol(item)).filter(Boolean);
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

function sum(rows: SourceView[], key: "items" | "signals"): number {
  return rows.reduce((total, row) => total + row[key], 0);
}

function formatSigned(value: number): ReactNode {
  if (!value) return "0";
  return value > 0 ? `+${value}` : String(value);
}
