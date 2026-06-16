import { useMemo, useState } from "react";
import { Activity, BarChart3, Gauge } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { RowRecord } from "@/types";
import { numberField, textField } from "@/views/rowFormat";

import {
  EmptyChart,
  GroupPill,
  MiniMetric,
  RangeCell,
  ReturnCell,
  ReturnProfile,
  ScorePill,
  TrendMark,
} from "./cells";
import { LightweightValuationChart } from "./chart";
import {
  assetRowClass,
  featuredAssetRows,
  filterMetricPeriod,
  formatMetricValue,
  formatScore,
  metricHistoryPoints,
  normalizeScore,
  percentileTone,
  postureBadge,
  postureFromScore,
  scoreColor,
  titleCase,
  weightedDriverScore,
} from "./format";
import { DEFAULT_MARKET_PERIODS, FORWARD_PE_PERIODS } from "./types";

export function MarketEnvironmentPanel({
  rows,
  referenceRows,
  assetRows,
  freshness,
}: {
  rows: RowRecord[];
  referenceRows: RowRecord[];
  assetRows: RowRecord[];
  freshness?: { status: string; reason: string };
}) {
  const score = weightedDriverScore(rows);
  const valuation = rows.find((row) => textField(row, ["category"]) === "Valuation");
  const trend = rows.find((row) => textField(row, ["category"]) === "Price Trend");
  const breadth = rows.find((row) => textField(row, ["category"]) === "Market Breadth");
  const risk = rows.find((row) => textField(row, ["category"]) === "Risk Appetite");
  const freshnessStatus = freshness?.status ?? "";
  const freshnessVariant = freshnessStatus === "stale" ? "destructive" : freshnessStatus === "fresh" ? "secondary" : "outline";

  return (
    <Card className="min-w-0">
      <CardHeader className="flex-row items-start justify-between gap-3 p-4 pb-2">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <CardTitle className="flex items-center gap-2 text-base">
              <Gauge className="size-4 text-muted-foreground" />
              Market Environment
            </CardTitle>
            {freshnessStatus ? <Badge variant={freshnessVariant}>{titleCase(freshnessStatus)}</Badge> : null}
          </div>
          <p className="mt-1 text-xs text-muted-foreground">Broad market inputs only: valuation, trend, breadth, risk appetite, and leadership.</p>
          {freshness?.reason ? <p className="mt-1 text-xs text-muted-foreground">{freshness.reason}</p> : null}
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

export function ReferenceValuationCharts({ rows }: { rows: RowRecord[] }) {
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

export function MarketAssetMatrix({ rows }: { rows: RowRecord[] }) {
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
