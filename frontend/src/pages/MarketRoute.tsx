import { useEffect, useMemo, useRef, useState } from "react";
import { Activity, BarChart3, Gauge } from "lucide-react";
import {
  ColorType,
  CrosshairMode,
  createChart,
  LineSeries,
  PriceScaleMode,
  type LineData,
  type PriceFormat,
  type Time,
} from "lightweight-charts";

import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { WorkspacePage } from "@/views/workspacePage";
import type { JsonValue, RowRecord } from "@/types";
import { rows } from "@/utils";
import { formatPct, numberField, textField } from "@/views/rowFormat";

type MetricPoint = {
  date: string;
  value?: number;
  index_price?: number;
};

type PeriodOption = {
  key: string;
  years: number;
};

const DEFAULT_MARKET_PERIODS: PeriodOption[] = [
  { key: "5Y", years: 5 },
  { key: "10Y", years: 10 },
  { key: "20Y", years: 20 },
  { key: "30Y", years: 30 },
  { key: "50Y", years: 50 },
  { key: "All", years: 0 },
];

const FORWARD_PE_PERIODS: PeriodOption[] = [
  { key: "1Y", years: 1 },
  { key: "2Y", years: 2 },
  { key: "5Y", years: 5 },
  { key: "10Y", years: 10 },
  { key: "20Y", years: 20 },
  { key: "All", years: 0 },
];

export function MarketRoute() {
  const { data } = useMarketData();
  usePanelScope("market");

  const referenceRows = rows(data.marketValuationReferenceCharts);
  const assetRows = rows(data.marketEnvironmentAssets);
  const environmentRows = rows(data.marketEnvironmentModel);
  const drivers = environmentRows.filter((row) => textField(row, ["category"]) !== "Overall");
  const marketDrivers = drivers.filter((row) => isMarketDriver(textField(row, ["category"])));

  return (
    <WorkspacePage
      eyebrow="Market stance"
      title="Where the Market Stands"
      subtitle="Broad market valuation, trend, breadth, risk appetite, and leadership."
    >
      <MarketEnvironmentPanel rows={marketDrivers} referenceRows={referenceRows} assetRows={assetRows} />

      <ReferenceValuationCharts rows={referenceRows} />

      <MarketAssetMatrix rows={assetRows} />
    </WorkspacePage>
  );
}

function MarketEnvironmentPanel({ rows, referenceRows, assetRows }: { rows: RowRecord[]; referenceRows: RowRecord[]; assetRows: RowRecord[] }) {
  const score = weightedDriverScore(rows);
  const valuation = rows.find((row) => textField(row, ["category"]) === "Valuation");
  const trend = rows.find((row) => textField(row, ["category"]) === "Price Trend");
  const breadth = rows.find((row) => textField(row, ["category"]) === "Market Breadth");
  const risk = rows.find((row) => textField(row, ["category"]) === "Risk Appetite");

  return (
    <Card className="min-w-0">
      <CardHeader className="flex-row items-start justify-between gap-3 p-4 pb-2">
        <div>
          <CardTitle className="flex items-center gap-2 text-base">
            <Gauge className="size-4 text-muted-foreground" />
            Market Environment
          </CardTitle>
          <p className="mt-1 text-xs text-muted-foreground">Broad market inputs only: valuation, trend, breadth, risk appetite, and leadership.</p>
        </div>
        <ScorePill value={score} posture={postureFromScore(score)} />
      </CardHeader>
      <CardContent className="space-y-4 p-4 pt-2">
        <div className="grid gap-2 sm:grid-cols-4">
          <MiniMetric label="Valuation" value={formatScore(numberField(valuation, ["score"], Number.NaN))} />
          <MiniMetric label="Trend" value={formatScore(numberField(trend, ["score"], Number.NaN))} />
          <MiniMetric label="Breadth" value={formatScore(numberField(breadth, ["score"], Number.NaN))} />
          <MiniMetric label="Risk" value={formatScore(numberField(risk, ["score"], Number.NaN))} />
        </div>
        <DriverRows rows={rows} />
        <div className="grid gap-2 sm:grid-cols-2">
          <MiniMetric label="Valuation Series" value={`${referenceRows.length}`} />
          <MiniMetric label="Market Asset Rows" value={`${assetRows.length}`} />
        </div>
      </CardContent>
    </Card>
  );
}

function DriverRows({ rows }: { rows: RowRecord[] }) {
  if (!rows.length) {
    return <EmptyChart label="No model rows loaded" />;
  }
  return (
    <div className="space-y-2">
      {rows.map((row) => {
        const score = numberField(row, ["score"], Number.NaN);
        return (
          <div key={textField(row, ["category"])} className="grid gap-2 rounded-md border border-border bg-background px-3 py-3 sm:grid-cols-[116px_1fr_104px] sm:items-center">
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold">{textField(row, ["category"])}</p>
              <p className="text-xs text-muted-foreground">
                {Number.isFinite(score) ? Math.round(score) : "--"} / 100
                {Number.isFinite(numberField(row, ["weight"], Number.NaN)) ? ` / ${Math.round(numberField(row, ["weight"], Number.NaN) * 100)}% weight` : ""}
              </p>
            </div>
            <div className="min-w-0">
              <div className="h-2 overflow-hidden rounded-full bg-muted">
                <div className="h-full rounded-full" style={{ width: `${normalizeScore(score)}%`, background: scoreColor(score) }} />
              </div>
              <p className="mt-2 line-clamp-2 text-xs leading-5 text-muted-foreground">{textField(row, ["evidence"])}</p>
              <p className="mt-1 truncate text-[11px] uppercase text-muted-foreground/75">{textField(row, ["source"])}</p>
            </div>
            <Badge className="justify-self-start sm:justify-self-end" variant={postureBadge(textField(row, ["posture"]))}>
              {titleCase(textField(row, ["posture"]))}
            </Badge>
          </div>
        );
      })}
    </div>
  );
}

function ReferenceValuationCharts({ rows }: { rows: RowRecord[] }) {
  if (!rows.length) {
    return (
      <Card>
        <CardContent className="flex min-h-40 items-center justify-center p-6">
          <EmptyChart label="No broad-market valuation series loaded" />
        </CardContent>
      </Card>
    );
  }
  return (
    <section>
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="flex items-center gap-2 text-sm font-semibold uppercase text-muted-foreground">
          <BarChart3 className="size-4" />
          Market Valuation Charts
        </h2>
        <Badge variant="outline">{rows.length} series</Badge>
      </div>
      <div className="mx-auto grid w-full max-w-5xl min-w-0 gap-4">
        {rows.map((row) => <ReferenceValuationCard key={textField(row, ["metric"])} row={row} />)}
      </div>
    </section>
  );
}

function ReferenceValuationCard({ row }: { row: RowRecord }) {
  const history = metricHistoryPoints(row);
  const metric = textField(row, ["metric"]);
  const periods = metric === "sp500_forward_pe" ? FORWARD_PE_PERIODS : DEFAULT_MARKET_PERIODS;
  const [selectedPeriod, setSelectedPeriod] = useState("All");
  const period = periods.find((option) => option.key === selectedPeriod) ?? periods[periods.length - 1];
  const visibleHistory = useMemo(() => filterMetricPeriod(history, period.years), [history, period.years]);
  const latest = numberField(row, ["latest_value"], Number.NaN);
  const percentile = numberField(row, ["percentile"], Number.NaN);
  const suffix = textField(row, ["suffix"]);
  const label = textField(row, ["label"]);
  return (
    <Card className="min-w-0">
      <CardHeader className="flex-row items-start justify-between gap-3 p-4 pb-2">
        <div>
          <CardTitle className="text-base">{label}</CardTitle>
          <p className="mt-1 text-sm text-muted-foreground">
            Latest: <span className="font-medium text-foreground">{formatMetricValue(latest, suffix)}</span>
            {Number.isFinite(percentile) ? <span className={percentileTone(row, percentile)}> {Math.round(percentile)}th percentile</span> : null}
            <span className="text-xs"> ({textField(row, ["latest_date"])})</span>
          </p>
        </div>
        <Badge variant={postureBadge(textField(row, ["posture"]))}>{titleCase(textField(row, ["posture"]))}</Badge>
      </CardHeader>
      <CardContent className="p-4 pt-2">
        <div className="mb-3 flex flex-wrap gap-1.5">
          {periods.map((option) => (
            <button
              key={option.key}
              type="button"
              onClick={() => setSelectedPeriod(option.key)}
              className={option.key === selectedPeriod ? "rounded-full bg-slate-900 px-2.5 py-0.5 text-xs font-semibold text-white" : "rounded-full bg-muted px-2.5 py-0.5 text-xs font-medium text-muted-foreground hover:bg-muted/80"}
            >
              {option.key}
            </button>
          ))}
          <span className="ml-auto inline-flex items-center gap-2 text-xs text-muted-foreground">
            <span className="h-0.5 w-4 rounded-full bg-slate-500" />
            Valuation
            <span className="h-0.5 w-4 rounded-full bg-blue-500/50" />
            S&P 500
          </span>
        </div>
        <div className="h-[360px] min-h-[360px]">
          {visibleHistory.length ? (
            <LightweightValuationChart data={visibleHistory} suffix={suffix} metricLabel={label} />
          ) : (
            <EmptyChart label="No valuation history" />
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function LightweightValuationChart({ data, suffix, metricLabel }: { data: MetricPoint[]; suffix: string; metricLabel: string }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const metricData = useMemo(() => metricLineData(data, "value"), [data]);
  const overlayData = useMemo(() => metricLineData(data, "index_price"), [data]);

  useEffect(() => {
    const element = containerRef.current;
    if (!element || !metricData.length) return;

    const chart = createChart(element, {
      autoSize: true,
      height: 360,
      layout: {
        background: { type: ColorType.Solid, color: "#ffffff" },
        textColor: "#64748b",
        fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif",
      },
      grid: {
        vertLines: { color: "#f1f5f9" },
        horzLines: { color: "#f1f5f9" },
      },
      leftPriceScale: {
        visible: overlayData.length > 0,
        mode: PriceScaleMode.Logarithmic,
        borderColor: "#e2e8f0",
      },
      rightPriceScale: {
        borderColor: "#e2e8f0",
      },
      timeScale: {
        borderColor: "#e2e8f0",
        rightOffset: 2,
        minBarSpacing: 0.001,
        timeVisible: false,
      },
      crosshair: {
        mode: CrosshairMode.Normal,
      },
      handleScroll: false,
      handleScale: false,
    });

    if (overlayData.length) {
      const overlay = chart.addSeries(LineSeries, {
        color: "rgba(59, 130, 246, 0.35)",
        lineWidth: 1,
        priceScaleId: "left",
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: true,
        crosshairMarkerRadius: 3,
        priceFormat: {
          type: "price",
          precision: 0,
          minMove: 1,
        },
      });
      overlay.setData(overlayData);
    }

    const metric = chart.addSeries(LineSeries, {
      color: "#64748b",
      lineWidth: 2,
      priceScaleId: "right",
      priceLineVisible: false,
      lastValueVisible: true,
      crosshairMarkerVisible: true,
      crosshairMarkerRadius: 4,
      title: metricLabel,
      priceFormat: metricPriceFormat(suffix),
    });
    metric.setData(metricData);
    chart.timeScale().fitContent();

    return () => chart.remove();
  }, [metricData, metricLabel, overlayData, suffix]);

  return <div ref={containerRef} className="h-[360px] min-h-[360px] w-full overflow-hidden rounded-lg" />;
}

function MarketAssetMatrix({ rows }: { rows: RowRecord[] }) {
  const featured = featuredAssetRows(rows);
  if (!featured.length) {
    return (
      <Card>
        <CardContent className="flex min-h-40 items-center justify-center p-6">
          <EmptyChart label="No market environment asset rows loaded" />
        </CardContent>
      </Card>
    );
  }
  return (
    <Card className="min-w-0 overflow-hidden">
      <CardHeader className="flex-row items-center justify-between gap-3 p-4 pb-2">
        <CardTitle className="flex items-center gap-2 text-base">
          <Activity className="size-4 text-muted-foreground" />
          Market Environment Asset Matrix
        </CardTitle>
        <Badge variant="outline">{rows.length} rows</Badge>
      </CardHeader>
      <CardContent className="overflow-x-auto p-0">
        <table className="w-full min-w-[1260px] text-sm">
          <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
            <tr>
              <th className="px-4 py-3">Group</th>
              <th className="px-3 py-3">Symbol</th>
              <th className="px-3 py-3">Return Profile</th>
              <th className="px-3 py-3 text-right">% 1D</th>
              <th className="px-3 py-3 text-right">% YTD</th>
              <th className="px-3 py-3 text-right">% 1M</th>
              <th className="px-3 py-3 text-right">% 1Y</th>
              <th className="px-3 py-3 text-right">52W Gap</th>
              <th className="px-3 py-3 text-center">20 SMA</th>
              <th className="px-3 py-3 text-center">50 SMA</th>
              <th className="px-3 py-3 text-center">200 SMA</th>
              <th className="px-4 py-3 text-center">50 &gt; 200</th>
            </tr>
          </thead>
          <tbody>
            {featured.map((row, index) => (
              <tr key={`${textField(row, ["group_name"])}-${textField(row, ["symbol"])}`} className={assetRowClass(featured, row, index)}>
                <td className="px-4 py-3 text-xs font-medium uppercase">
                  <GroupPill value={textField(row, ["group_name"])} />
                </td>
                <td className="px-3 py-3">
                  <p className="font-semibold">{textField(row, ["symbol"])}</p>
                  <p className="max-w-44 truncate text-xs text-muted-foreground">{textField(row, ["name"])}</p>
                </td>
                <td className="px-3 py-3"><ReturnProfile row={row} /></td>
                <ReturnCell value={numberField(row, ["return_1d"], Number.NaN)} />
                <ReturnCell value={numberField(row, ["return_ytd"], Number.NaN)} />
                <ReturnCell value={numberField(row, ["return_1m"], Number.NaN)} />
                <ReturnCell value={numberField(row, ["return_1y"], Number.NaN)} />
                <td className="px-3 py-3"><RangeCell value={numberField(row, ["pct_from_52w_high"], Number.NaN)} /></td>
                <td className="px-3 py-3 text-center"><TrendMark value={row.sma_20_up} /></td>
                <td className="px-3 py-3 text-center"><TrendMark value={row.sma_50_up} /></td>
                <td className="px-3 py-3 text-center"><TrendMark value={row.sma_200_up} /></td>
                <td className="px-4 py-3 text-center"><TrendMark value={row.sma_50_gt_200} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

function featuredAssetRows(inputRows: RowRecord[]): RowRecord[] {
  const groupLimits: Record<string, number> = {
    Market: 12,
    Macro: 8,
    Sectors: 12,
    Industries: 12,
    "Managed ETFs": 8,
    Countries: 8,
    Others: 8,
  };
  return Object.entries(groupLimits)
    .flatMap(([group, limit]) => inputRows.filter((row) => textField(row, ["group_name"]) === group).slice(0, limit))
    .slice(0, 48);
}

function assetRowClass(rows: RowRecord[], row: RowRecord, index: number): string {
  const group = textField(row, ["group_name"]);
  const previous = index > 0 ? textField(rows[index - 1], ["group_name"]) : "";
  const separator = index > 0 && group !== previous ? "border-t-2 border-t-border" : "";
  return `border-b border-border align-middle transition-colors hover:bg-accent/45 ${separator} ${groupBackgroundClass(group)}`;
}

function GroupPill({ value }: { value: string }) {
  return <span className={`inline-flex min-w-24 justify-center rounded-md border px-2 py-1 text-[11px] font-semibold uppercase ${groupPillClass(value)}`}>{value}</span>;
}

function ReturnCell({ value }: { value: number }) {
  const tone = returnToneClass(value);
  return (
    <td className="px-3 py-3 text-right">
      <span className={`inline-flex min-w-16 justify-end rounded-md px-2 py-1 font-semibold tabular-nums ${tone}`}>
        {formatMaybePct(value)}
      </span>
    </td>
  );
}

function RangeCell({ value }: { value: number }) {
  if (!Number.isFinite(value)) return <span className="block text-right text-muted-foreground">-</span>;
  const distance = Math.max(0, Math.min(100, Math.abs(value)));
  const width = `${100 - distance}%`;
  return (
    <div className="min-w-28">
      <div className="mb-1 text-right text-xs font-medium tabular-nums text-muted-foreground">{formatMaybePct(-distance)}</div>
      <div className="h-1.5 overflow-hidden rounded-full bg-muted">
        <div className="h-full rounded-full bg-primary/70" style={{ width }} />
      </div>
    </div>
  );
}

function ReturnProfile({ row }: { row: RowRecord }) {
  const horizons = [
    { label: "1W", value: numberField(row, ["return_1w"], Number.NaN) },
    { label: "1M", value: numberField(row, ["return_1m"], Number.NaN) },
    { label: "YTD", value: numberField(row, ["return_ytd"], Number.NaN) },
    { label: "1Y", value: numberField(row, ["return_1y"], Number.NaN) },
  ];
  const valid = horizons.filter((horizon) => Number.isFinite(horizon.value));
  if (!valid.length) return <span className="text-muted-foreground">-</span>;
  const maxAbs = Math.max(1, ...valid.map((horizon) => Math.abs(horizon.value)));
  return (
    <div className="min-w-56 space-y-1.5" title="1W one-week return; 1M one-month return; YTD year-to-date return; 1Y one-year return">
      {horizons.map((horizon) => (
        <div key={horizon.label} className="grid grid-cols-[30px_1fr_54px] items-center gap-2">
          <span className="text-[11px] font-medium text-muted-foreground">{horizon.label}</span>
          <div className="relative h-2 overflow-hidden rounded-full bg-muted">
            <span className="absolute left-1/2 top-0 h-full w-px bg-border" />
            {Number.isFinite(horizon.value) ? (
              <span
                className={`absolute top-0 h-full rounded-full ${horizon.value >= 0 ? "bg-green-600" : "bg-red-600"}`}
                style={returnBarStyle(horizon.value, maxAbs)}
              />
            ) : null}
          </div>
          <span className={`text-right text-[11px] font-semibold tabular-nums ${returnTextClass(horizon.value)}`}>
            {formatMaybePct(horizon.value)}
          </span>
        </div>
      ))}
    </div>
  );
}

function returnBarStyle(value: number, maxAbs: number): { left?: string; right?: string; width: string } {
  const width = `${Math.max(3, Math.min(50, (Math.abs(value) / maxAbs) * 50))}%`;
  return value >= 0 ? { left: "50%", width } : { right: "50%", width };
}

function returnTextClass(value: number): string {
  if (!Number.isFinite(value)) return "text-muted-foreground";
  if (value > 0) return "text-green-700";
  if (value < 0) return "text-red-700";
  return "text-muted-foreground";
}

function TrendMark({ value }: { value: RowRecord[string] }) {
  if (typeof value !== "boolean") return <span className="text-muted-foreground">-</span>;
  return <span className={value ? "rounded-md bg-green-50 px-2 py-1 font-semibold text-green-800" : "rounded-md bg-red-50 px-2 py-1 font-semibold text-red-800"}>{value ? "▲" : "▼"}</span>;
}

function MiniMetric({ label, value, dark = false }: { label: string; value: string; dark?: boolean }) {
  return (
    <div className={dark ? "min-w-0 rounded-md border border-white/12 bg-white/[0.08] px-3 py-2" : "min-w-0 rounded-md border border-border bg-background px-3 py-2"}>
      <p className={dark ? "truncate text-[11px] font-medium uppercase text-white/56" : "truncate text-[11px] font-medium uppercase text-muted-foreground"}>{label}</p>
      <p className={dark ? "mt-1 truncate text-sm font-semibold text-white" : "mt-1 truncate text-sm font-semibold"}>{value}</p>
    </div>
  );
}

function ScorePill({ value, posture }: { value: number; posture: string }) {
  return <Badge className="w-fit justify-self-end" variant={postureBadge(posture)}>{Number.isFinite(value) ? Math.round(value) : "--"}</Badge>;
}

function EmptyChart({ label, dark = false }: { label: string; dark?: boolean }) {
  return (
    <div className={dark ? "flex h-full min-h-32 items-center justify-center rounded-md border border-dashed border-white/18 bg-white/[0.05] px-4 text-center text-xs text-white/62" : "flex h-full min-h-32 items-center justify-center rounded-md border border-dashed border-border bg-muted/30 px-4 text-center text-xs text-muted-foreground"}>
      {label}
    </div>
  );
}

function metricHistoryPoints(row: RowRecord): MetricPoint[] {
  const value = row.history;
  if (!Array.isArray(value)) return [];
  const points = value
    .filter((point): point is Record<string, JsonValue> => typeof point === "object" && point !== null && !Array.isArray(point))
    .map((point) => ({
      date: typeof point.date === "string" ? point.date.slice(0, 10) : "",
      value: numeric(point.value),
      index_price: numeric(point.index_price),
    }))
    .filter((point) => point.date && Number.isFinite(point.value));
  const stride = Math.max(1, Math.ceil(points.length / 420));
  return points.filter((_, index) => index % stride === 0 || index === points.length - 1);
}

function filterMetricPeriod(points: MetricPoint[], years: number): MetricPoint[] {
  if (!years) return points;
  const cutoff = new Date();
  cutoff.setFullYear(cutoff.getFullYear() - years);
  const cutoffDate = cutoff.toISOString().slice(0, 10);
  return points.filter((point) => point.date >= cutoffDate);
}

function metricLineData(points: MetricPoint[], key: "value" | "index_price"): LineData<Time>[] {
  return points
    .filter((point) => typeof point[key] === "number" && Number.isFinite(point[key]) && (key !== "index_price" || Number(point[key]) > 0))
    .map((point) => ({ time: point.date as Time, value: Number(point[key]) }));
}

function metricPriceFormat(suffix: string): PriceFormat {
  if (suffix === "%") {
    return {
      type: "custom",
      formatter: (value) => `${Number(value).toFixed(2)}%`,
      minMove: 0.01,
    };
  }
  if (suffix === "x") {
    return {
      type: "custom",
      formatter: (value) => `${Number(value).toFixed(2)}x`,
      minMove: 0.01,
    };
  }
  return {
    type: "price",
    precision: 2,
    minMove: 0.01,
  };
}

function isMarketDriver(category: string): boolean {
  return ["Valuation", "Price Trend", "Market Breadth", "Risk Appetite", "Sector / Theme Leadership"].includes(category);
}

function weightedDriverScore(inputRows: RowRecord[]): number {
  let weighted = 0;
  let total = 0;
  for (const row of inputRows) {
    const score = numberField(row, ["score"], Number.NaN);
    const weight = numberField(row, ["weight"], Number.NaN);
    if (!Number.isFinite(score) || !Number.isFinite(weight) || weight <= 0) continue;
    weighted += score * weight;
    total += weight;
  }
  return total > 0 ? weighted / total : Number.NaN;
}

function postureFromScore(value: number): string {
  if (!Number.isFinite(value)) return "not enough data";
  if (value >= 70) return "constructive";
  if (value >= 45) return "mixed";
  return "defensive";
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

function formatMaybePct(value: number): string {
  return Number.isFinite(value) ? formatPct(value) : "-";
}

function formatMetricValue(value: number, suffix: string): string {
  if (!Number.isFinite(value)) return "-";
  if (suffix === "%") return `${value.toFixed(2)}%`;
  if (suffix === "x") return `${value.toFixed(2)}x`;
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function formatScore(value: number): string {
  return Number.isFinite(value) ? `${Math.round(value)} / 100` : "-";
}

function percentileTone(row: RowRecord, percentile: number): string {
  const higherIsBetter = row.higher_is_better === true;
  const good = higherIsBetter ? percentile >= 70 : percentile <= 30;
  const bad = higherIsBetter ? percentile <= 30 : percentile >= 70;
  if (good) return "ml-2 font-medium text-green-700";
  if (bad) return "ml-2 font-medium text-red-700";
  return "ml-2 font-medium text-amber-700";
}

function returnToneClass(value: number): string {
  if (!Number.isFinite(value)) return "bg-muted text-muted-foreground";
  if (value >= 10) return "bg-green-100 text-green-900";
  if (value > 0) return "bg-green-50 text-green-800";
  if (value <= -10) return "bg-red-100 text-red-900";
  if (value < 0) return "bg-red-50 text-red-800";
  return "bg-muted text-muted-foreground";
}

function groupBackgroundClass(group: string): string {
  if (group === "Market") return "bg-blue-50/30";
  if (group === "Macro") return "bg-amber-50/35";
  if (group === "Sectors") return "bg-green-50/25";
  if (group === "Industries") return "bg-violet-50/25";
  if (group === "Managed ETFs") return "bg-sky-50/25";
  if (group === "Countries") return "bg-cyan-50/25";
  return "bg-background";
}

function groupPillClass(group: string): string {
  if (group === "Market") return "border-blue-200 bg-blue-50 text-blue-800";
  if (group === "Macro") return "border-amber-200 bg-amber-50 text-amber-800";
  if (group === "Sectors") return "border-green-200 bg-green-50 text-green-800";
  if (group === "Industries") return "border-violet-200 bg-violet-50 text-violet-800";
  if (group === "Managed ETFs") return "border-sky-200 bg-sky-50 text-sky-800";
  if (group === "Countries") return "border-cyan-200 bg-cyan-50 text-cyan-800";
  return "border-border bg-muted text-muted-foreground";
}

function postureBadge(value: string): "default" | "secondary" | "outline" | "destructive" | "success" | "warning" | "info" {
  const normalized = value.toLowerCase();
  if (normalized.includes("constructive") || normalized.includes("discounted") || normalized.includes("attractive")) return "success";
  if (normalized.includes("defensive") || normalized.includes("stretched")) return "warning";
  if (normalized.includes("not enough") || normalized.includes("missing")) return "outline";
  return "info";
}

function scoreColor(value: number): string {
  if (!Number.isFinite(value)) return "var(--muted-foreground)";
  if (value >= 70) return "var(--success)";
  if (value >= 45) return "var(--primary)";
  return "var(--warning)";
}

function normalizeScore(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, value));
}
