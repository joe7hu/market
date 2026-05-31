import { Activity, BarChart3, Gauge, ShieldAlert } from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { DataGridSection } from "@/views/dataGridSection";
import { WorkspacePage, type MetricSpec } from "@/views/workspacePage";
import type { JsonValue, RowRecord } from "@/types";
import { rows } from "@/utils";
import { displayField, formatMoney, formatPct, numberField, textField, toneFromText } from "@/views/rowFormat";

type ChartPoint = {
  date: string;
  price?: number;
  fair_value?: number;
  discount_pct?: number;
};

export function MarketRoute() {
  const { data } = useMarketData();
  usePanelScope("market");

  const valuationRows = rows(data.marketValuationCharts);
  const environmentRows = rows(data.marketEnvironmentModel);
  const marketRow = valuationRows.find((row) => textField(row, ["scope"]) === "whole_market");
  const tickerRows = valuationRows.filter((row) => textField(row, ["scope"]) !== "whole_market");
  const overall = environmentRows.find((row) => textField(row, ["category"]) === "Overall");
  const buckets = environmentRows.filter((row) => textField(row, ["category"]) !== "Overall");
  const discounted = tickerRows.filter((row) => textField(row, ["valuation_posture"]) === "discounted").length;
  const stretched = tickerRows.filter((row) => textField(row, ["valuation_posture"]) === "stretched").length;
  const metrics: MetricSpec[] = [
    ["Environment", titleCase(textField(overall, ["posture"], "Not loaded")), textField(overall, ["next_action"], "Refresh market scope"), toneFromText(textField(overall, ["posture"]))],
    ["Valuation", formatMaybePct(numberField(marketRow, ["upside_pct"], Number.NaN)), "median fair-value gap", valuationTone(numberField(marketRow, ["upside_pct"], Number.NaN))],
    ["Coverage", `${numberField(marketRow, ["component_count"], tickerRows.length)}`, "covered market and watchlist names", tickerRows.length ? "info" : "warn"],
    ["Outliers", `${discounted}/${stretched}`, "discounted / stretched", stretched > discounted ? "warn" : "good"],
  ];

  return (
    <WorkspacePage
      eyebrow="Market stance"
      title="Where the Market Stands"
      subtitle="Valuation, trend, breadth, liquidity, event risk, and portfolio exposure rolled into sizing posture."
      metrics={metrics}
    >
      <section className="grid gap-4 xl:grid-cols-[1.15fr_0.85fr]">
        <MarketCompositeCard row={marketRow} />
        <EnvironmentCard overall={overall} rows={buckets} />
      </section>

      <TickerValuationGrid rows={tickerRows} />

      <section className="grid gap-4 xl:grid-cols-2">
        <DataGridSection title="Portfolio Effects" rows={rows(data.marketContext)} />
        <DataGridSection title="Risk Overrides" rows={rows(data.portfolioRiskCards)} />
      </section>
    </WorkspacePage>
  );
}

function MarketCompositeCard({ row }: { row?: RowRecord }) {
  const history = historyPoints(row);
  const posture = textField(row, ["valuation_posture"], "not loaded");
  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-3 p-4 pb-2">
        <div>
          <CardTitle className="flex items-center gap-2 text-base">
            <BarChart3 className="size-4 text-muted-foreground" />
            Whole-Market Valuation
          </CardTitle>
          <p className="mt-1 text-xs text-muted-foreground">{textField(row, ["next_action"], "Refresh valuation rows.")}</p>
        </div>
        <Badge variant={postureBadge(posture)}>{titleCase(posture)}</Badge>
      </CardHeader>
      <CardContent className="space-y-4 p-4 pt-2">
        <div className="grid gap-3 sm:grid-cols-3">
          <MiniMetric label="Forward P/E" value={formatMultiple(numberField(row, ["forward_pe"], Number.NaN))} />
          <MiniMetric label="P/S" value={formatMultiple(numberField(row, ["ps_ratio"], Number.NaN))} />
          <MiniMetric label="Fair-Value Gap" value={formatMaybePct(numberField(row, ["upside_pct"], Number.NaN))} />
        </div>
        <div className="h-72 min-h-72">
          {history.length ? (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={history} margin={{ left: 0, right: 8, top: 12, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="date" minTickGap={42} tickLine={false} axisLine={false} tick={{ fontSize: 11 }} />
                <YAxis domain={["dataMin", "dataMax"]} tickLine={false} axisLine={false} tick={{ fontSize: 11 }} width={44} />
                <Tooltip formatter={(value) => formatChartNumber(value)} labelFormatter={(value) => String(value)} />
                <Line type="monotone" dataKey="price" stroke="var(--primary)" strokeWidth={2.5} dot={false} name="Indexed price" />
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <EmptyChart label={displayField(row, ["coverage"], "No valuation history")} />
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function EnvironmentCard({ overall, rows }: { overall?: RowRecord; rows: RowRecord[] }) {
  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-3 p-4 pb-2">
        <div>
          <CardTitle className="flex items-center gap-2 text-base">
            <Gauge className="size-4 text-muted-foreground" />
            Environment Model
          </CardTitle>
          <p className="mt-1 text-xs text-muted-foreground">{textField(overall, ["portfolio_effect"], "Sizing model is waiting for data.")}</p>
        </div>
        <ScorePill value={numberField(overall, ["score"], Number.NaN)} posture={textField(overall, ["posture"])} />
      </CardHeader>
      <CardContent className="space-y-3 p-4 pt-2">
        <div className="h-36 min-h-36">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={rows} layout="vertical" margin={{ left: 4, right: 24, top: 2, bottom: 2 }}>
              <XAxis type="number" domain={[0, 100]} hide />
              <YAxis dataKey="category" type="category" width={92} tickLine={false} axisLine={false} tick={{ fontSize: 11 }} />
              <Tooltip formatter={(value) => formatChartNumber(value)} />
              <Bar dataKey="score" radius={[0, 4, 4, 0]}>
                {rows.map((row) => <Cell key={textField(row, ["category"])} fill={scoreColor(numberField(row, ["score"], Number.NaN))} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
        <div className="space-y-2">
          {rows.map((row) => (
            <div key={textField(row, ["category"])} className="rounded-md border border-border bg-background px-3 py-2">
              <div className="flex items-center justify-between gap-3">
                <span className="text-sm font-medium">{textField(row, ["category"])}</span>
                <Badge variant={postureBadge(textField(row, ["posture"]))}>{titleCase(textField(row, ["posture"]))}</Badge>
              </div>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">{textField(row, ["evidence"])}</p>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

function TickerValuationGrid({ rows }: { rows: RowRecord[] }) {
  const sorted = [...rows].sort((left, right) => numberField(right, ["valuation_score"], -1) - numberField(left, ["valuation_score"], -1));
  if (!sorted.length) {
    return (
      <Card>
        <CardContent className="flex min-h-40 items-center justify-center p-6">
          <EmptyChart label="No watchlist valuation rows" />
        </CardContent>
      </Card>
    );
  }
  return (
    <section>
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="flex items-center gap-2 text-sm font-semibold uppercase text-muted-foreground">
          <Activity className="size-4" />
          Watchlist Valuation
        </h2>
        <Badge variant="outline">{sorted.length} names</Badge>
      </div>
      <div className="grid gap-4 lg:grid-cols-2 2xl:grid-cols-3">
        {sorted.map((row) => <TickerValuationCard key={textField(row, ["symbol"])} row={row} />)}
      </div>
    </section>
  );
}

function TickerValuationCard({ row }: { row: RowRecord }) {
  const history = historyPoints(row);
  const posture = textField(row, ["valuation_posture"], "not loaded");
  const upside = numberField(row, ["upside_pct"], Number.NaN);
  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-3 p-4 pb-2">
        <div className="min-w-0">
          <CardTitle className="truncate text-base">{textField(row, ["symbol"])}</CardTitle>
          <p className="truncate text-xs text-muted-foreground">{textField(row, ["name"])}</p>
        </div>
        <Badge variant={postureBadge(posture)}>{titleCase(posture)}</Badge>
      </CardHeader>
      <CardContent className="space-y-3 p-4 pt-2">
        <div className="grid grid-cols-3 gap-2">
          <MiniMetric label="Price" value={formatMoney(numberField(row, ["latest_price"], Number.NaN))} />
          <MiniMetric label="Fair Value" value={formatMoney(numberField(row, ["fair_value"], Number.NaN))} />
          <MiniMetric label="Gap" value={formatMaybePct(upside)} />
        </div>
        <div className="h-32 min-h-32">
          {history.length ? (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={history} margin={{ left: 0, right: 4, top: 6, bottom: 0 }}>
                <XAxis dataKey="date" hide />
                <YAxis domain={["dataMin", "dataMax"]} hide />
                <Tooltip formatter={(value, name) => [formatChartNumber(value), name === "fair_value" ? "Fair value" : "Price"]} labelFormatter={(value) => String(value)} />
                <Line type="monotone" dataKey="price" stroke="var(--primary)" strokeWidth={2} dot={false} name="Price" />
                <Line type="monotone" dataKey="fair_value" stroke="var(--muted-foreground)" strokeDasharray="4 4" strokeWidth={1.5} dot={false} name="Fair value" />
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <EmptyChart label={displayField(row, ["coverage"], "No price history")} compact />
          )}
        </div>
        <div className="rounded-md border border-border bg-background px-3 py-2 text-xs leading-5 text-muted-foreground">
          <div className="mb-1 flex items-center gap-2 font-medium text-foreground">
            <ShieldAlert className="size-3.5" />
            {formatMultiple(numberField(row, ["forward_pe"], Number.NaN))} P/E · {formatMultiple(numberField(row, ["ps_ratio"], Number.NaN))} P/S
          </div>
          {textField(row, ["next_action"])}
        </div>
      </CardContent>
    </Card>
  );
}

function MiniMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-md border border-border bg-background px-3 py-2">
      <p className="truncate text-[11px] font-medium uppercase text-muted-foreground">{label}</p>
      <p className="mt-1 truncate text-sm font-semibold">{value}</p>
    </div>
  );
}

function ScorePill({ value, posture }: { value: number; posture: string }) {
  return <Badge variant={postureBadge(posture)}>{Number.isFinite(value) ? Math.round(value) : "--"}</Badge>;
}

function EmptyChart({ label, compact = false }: { label: string; compact?: boolean }) {
  return (
    <div className={`flex ${compact ? "h-28" : "h-full min-h-32"} items-center justify-center rounded-md border border-dashed border-border bg-muted/30 px-4 text-center text-xs text-muted-foreground`}>
      {label}
    </div>
  );
}

function historyPoints(row: RowRecord | undefined): ChartPoint[] {
  const value = row?.history;
  if (!Array.isArray(value)) return [];
  return value
    .filter((point): point is Record<string, JsonValue> => typeof point === "object" && point !== null && !Array.isArray(point))
    .map((point) => ({
      date: typeof point.date === "string" ? point.date.slice(0, 10) : "",
      price: numeric(point.price),
      fair_value: numeric(point.fair_value),
      discount_pct: numeric(point.discount_pct),
    }))
    .filter((point) => point.date && Number.isFinite(point.price));
}

function numeric(value: JsonValue | undefined): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value !== "string") return undefined;
  const parsed = Number(value.replace(/[$,%_,]/g, ""));
  return Number.isFinite(parsed) ? parsed : undefined;
}

function titleCase(value: string): string {
  return value.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatMultiple(value: number): string {
  return Number.isFinite(value) ? `${value.toFixed(1)}x` : "-";
}

function formatMaybePct(value: number): string {
  return Number.isFinite(value) ? formatPct(value) : "-";
}

function formatChartNumber(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "-";
}

function postureBadge(value: string): "default" | "secondary" | "outline" | "destructive" | "success" | "warning" | "info" {
  const normalized = value.toLowerCase();
  if (normalized.includes("constructive") || normalized.includes("discounted") || normalized.includes("attractive")) return "success";
  if (normalized.includes("defensive") || normalized.includes("stretched")) return "warning";
  if (normalized.includes("not enough") || normalized.includes("missing")) return "outline";
  return "info";
}

function valuationTone(value: number): "good" | "warn" | "info" | "muted" {
  if (!Number.isFinite(value)) return "muted";
  if (value >= 10) return "good";
  if (value <= -10) return "warn";
  return "info";
}

function scoreColor(value: number): string {
  if (!Number.isFinite(value)) return "var(--muted-foreground)";
  if (value >= 70) return "var(--success)";
  if (value >= 45) return "var(--primary)";
  return "var(--warning)";
}
