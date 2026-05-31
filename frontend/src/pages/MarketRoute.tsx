import { Activity, ArrowUpRight, BarChart3, Gauge, ListChecks, ShieldAlert } from "lucide-react";
import type { ReactNode } from "react";
import {
  Bar,
  BarChart,
  Area,
  CartesianGrid,
  Cell,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { WorkspacePage, type MetricSpec } from "@/views/workspacePage";
import type { JsonValue, RowRecord } from "@/types";
import { rows } from "@/utils";
import { formatPct, numberField, textField, toneFromText } from "@/views/rowFormat";

type ValuationBucket = {
  label: string;
  count: number;
  tone: "success" | "warning" | "info" | "outline";
};

type MetricPoint = {
  date: string;
  value?: number;
  index_price?: number;
};

export function MarketRoute() {
  const { data } = useMarketData();
  usePanelScope("market");

  const referenceRows = rows(data.marketValuationReferenceCharts);
  const valuationRows = rows(data.marketValuationCharts);
  const assetRows = rows(data.marketEnvironmentAssets);
  const environmentRows = rows(data.marketEnvironmentModel);
  const marketRow = valuationRows.find((row) => textField(row, ["scope"]) === "whole_market");
  const tickerRows = valuationRows.filter((row) => textField(row, ["scope"]) !== "whole_market");
  const overall = environmentRows.find((row) => textField(row, ["category"]) === "Overall");
  const drivers = environmentRows.filter((row) => textField(row, ["category"]) !== "Overall");
  const buckets = valuationBuckets(tickerRows);
  const marketScore = numberField(marketRow, ["valuation_score"], Number.NaN);
  const metrics: MetricSpec[] = [
    ["Regime", titleCase(textField(overall, ["posture"], "Not loaded")), textField(overall, ["next_action"], "Refresh market scope"), toneFromText(textField(overall, ["posture"]))],
    ["Valuation", Number.isFinite(marketScore) ? `${Math.round(marketScore)} / 100` : "-", valuationCaption(marketRow), scoreTone(marketScore)],
    ["Series", `${referenceRows.length}`, "broad valuation charts loaded", referenceRows.length >= 4 ? "good" : "warn"],
    ["Assets", `${assetRows.length}`, "environment matrix rows loaded", assetRows.length ? "info" : "warn"],
  ];

  return (
    <WorkspacePage
      eyebrow="Market stance"
      title="Where the Market Stands"
      subtitle="The model turns valuation, trend, breadth, liquidity, event risk, and portfolio exposure into an action posture."
      metrics={metrics}
    >
      <StanceHero overall={overall} marketRow={marketRow} drivers={drivers} tickerRows={tickerRows} buckets={buckets} referenceRows={referenceRows} assetRows={assetRows} />

      <ReferenceValuationCharts rows={referenceRows} />

      <section className="grid min-w-0 gap-4 xl:grid-cols-[0.95fr_1.05fr]">
        <EnvironmentModel overall={overall} rows={drivers} />
        <MarketValuationMap marketRow={marketRow} tickerRows={tickerRows} buckets={buckets} />
      </section>

      <MarketAssetMatrix rows={assetRows} />
    </WorkspacePage>
  );
}

function StanceHero({
  overall,
  marketRow,
  drivers,
  tickerRows,
  buckets,
  referenceRows,
  assetRows,
}: {
  overall?: RowRecord;
  marketRow?: RowRecord;
  drivers: RowRecord[];
  tickerRows: RowRecord[];
  buckets: ValuationBucket[];
  referenceRows: RowRecord[];
  assetRows: RowRecord[];
}) {
  const score = numberField(overall, ["score"], Number.NaN);
  const posture = textField(overall, ["posture"], "not loaded");
  const pressure = drivers
    .filter((row) => textField(row, ["posture"]).includes("defensive"))
    .sort((left, right) => numberField(left, ["score"], 100) - numberField(right, ["score"], 100));
  const support = drivers
    .filter((row) => textField(row, ["posture"]).includes("constructive"))
    .sort((left, right) => numberField(right, ["score"], -1) - numberField(left, ["score"], -1));
  const ranked = rankTickerRows(tickerRows);
  const best = ranked[0];
  const worst = [...ranked].reverse()[0];
  const valuationDriverScore = numberField(drivers.find((row) => textField(row, ["category"]) === "Valuation"), ["score"], Number.NaN);
  const valuationScore = Number.isFinite(valuationDriverScore) ? valuationDriverScore : numberField(marketRow, ["valuation_score"], Number.NaN);

  return (
    <section className="min-w-0 overflow-hidden rounded-lg border border-border bg-[linear-gradient(135deg,#14261f_0%,#203226_46%,#36412a_100%)] text-white shadow-sm">
      <div className="grid gap-0 xl:grid-cols-[1.2fr_0.8fr]">
        <div className="min-w-0 space-y-4 p-4 sm:p-5">
          <div className="flex flex-wrap items-center gap-3">
            <Badge variant={postureBadge(posture)}>{titleCase(posture)}</Badge>
            <span className="text-sm text-white/70">{Number.isFinite(score) ? `${Math.round(score)} / 100 environment score` : "Environment score unavailable"}</span>
          </div>
          <div>
            <h2 className="max-w-3xl break-words text-xl font-semibold tracking-normal sm:text-2xl">
              {stanceHeadline(posture, pressure, support)}
            </h2>
            <p className="mt-2 max-w-3xl break-words text-sm leading-6 text-white/72">
              {textField(overall, ["next_action"], "Separate cheap-but-weak names from expensive leaders before adding exposure.")}
            </p>
          </div>
          <div className="grid gap-3 md:grid-cols-3">
            <HeroSignal
              icon={<ShieldAlert className="size-4" />}
              label="Pressure"
              value={pressure.length ? pressure.map((row) => textField(row, ["category"])).join(" + ") : "None flagged"}
              detail={pressure[0] ? textField(pressure[0], ["portfolio_effect"]) : "No defensive model driver is currently dominant."}
            />
            <HeroSignal
              icon={<ArrowUpRight className="size-4" />}
              label="Support"
              value={support.length ? support.map((row) => textField(row, ["category"])).join(" + ") : "No strong support"}
              detail={support[0] ? textField(support[0], ["portfolio_effect"]) : "Constructive drivers are not strong enough to lift sizing."}
            />
            <HeroSignal
              icon={<ListChecks className="size-4" />}
              label="Action"
              value={marketActionLabel(marketRow, best, worst)}
              detail={`${referenceRows.length} valuation series, ${assetRows.length} asset rows loaded.`}
            />
          </div>
        </div>
        <div className="min-w-0 border-t border-white/12 bg-white/[0.06] p-4 sm:p-5 xl:border-l xl:border-t-0">
          <div className="mb-4 flex items-center justify-between gap-3">
            <div>
              <p className="text-xs font-semibold uppercase text-white/58">Market valuation score</p>
              <p className="mt-1 text-xl font-semibold">{formatScore(valuationScore)}</p>
            </div>
            <Badge variant={postureBadge(textField(marketRow, ["valuation_posture"]))}>{titleCase(textField(marketRow, ["valuation_posture"], "not loaded"))}</Badge>
          </div>
          <ValuationScorePanel row={marketRow} buckets={buckets} />
          <div className="mt-4 grid grid-cols-3 gap-2">
            <MiniMetric dark label="P/S" value={formatMultiple(numberField(marketRow, ["ps_ratio"], Number.NaN))} />
            <MiniMetric dark label="Stretched" value={`${buckets.find((bucket) => bucket.label === "Stretched")?.count ?? 0}`} />
            <MiniMetric dark label="Coverage" value={`${numberField(marketRow, ["component_count"], tickerRows.length)}`} />
          </div>
        </div>
      </div>
    </section>
  );
}

function HeroSignal({ icon, label, value, detail }: { icon: ReactNode; label: string; value: string; detail: string }) {
  return (
    <div className="min-w-0 rounded-md border border-white/12 bg-white/[0.08] px-3 py-3">
      <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase text-white/58">
        {icon}
        {label}
      </div>
      <p className="break-words text-sm font-semibold text-white">{value}</p>
      <p className="mt-1 line-clamp-2 text-xs leading-5 text-white/62">{detail}</p>
    </div>
  );
}

function EnvironmentModel({ overall, rows }: { overall?: RowRecord; rows: RowRecord[] }) {
  return (
    <Card className="min-w-0">
      <CardHeader className="flex-row items-start justify-between gap-3 p-4 pb-2">
        <div>
          <CardTitle className="flex items-center gap-2 text-base">
            <Gauge className="size-4 text-muted-foreground" />
            Market Environment Model
          </CardTitle>
          <p className="mt-1 text-xs text-muted-foreground">{textField(overall, ["evidence"], "Model is waiting for market evidence.")}</p>
        </div>
        <ScorePill value={numberField(overall, ["score"], Number.NaN)} posture={textField(overall, ["posture"])} />
      </CardHeader>
      <CardContent className="space-y-4 p-4 pt-2">
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
      </CardContent>
    </Card>
  );
}

function MarketValuationMap({ marketRow, tickerRows, buckets }: { marketRow?: RowRecord; tickerRows: RowRecord[]; buckets: ValuationBucket[] }) {
  const chartRows = buckets.map((bucket) => ({ ...bucket, fill: bucketColor(bucket.label) }));

  return (
    <Card className="min-w-0">
      <CardHeader className="flex-row items-start justify-between gap-3 p-4 pb-2">
        <div>
          <CardTitle className="flex items-center gap-2 text-base">
            <BarChart3 className="size-4 text-muted-foreground" />
            Valuation Distribution
          </CardTitle>
          <p className="mt-1 text-xs text-muted-foreground">{textField(marketRow, ["next_action"], "Use the outliers as the first review queue.")}</p>
        </div>
        <Badge variant={postureBadge(textField(marketRow, ["valuation_posture"]))}>{titleCase(textField(marketRow, ["valuation_posture"], "not loaded"))}</Badge>
      </CardHeader>
      <CardContent className="space-y-4 p-4 pt-2">
        <div className="grid gap-3 sm:grid-cols-3">
          <MiniMetric label="P/S" value={formatMultiple(numberField(marketRow, ["ps_ratio"], Number.NaN))} />
          <MiniMetric label="Score" value={formatScore(numberField(marketRow, ["valuation_score"], Number.NaN))} />
          <MiniMetric label="Fair Gap" value={formatMaybePct(numberField(marketRow, ["upside_pct"], Number.NaN))} />
        </div>
        <div className="h-48 min-h-48">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={chartRows} margin={{ left: 0, right: 12, top: 14, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="label" tickLine={false} axisLine={false} tick={{ fontSize: 11 }} />
              <YAxis allowDecimals={false} tickLine={false} axisLine={false} tick={{ fontSize: 11 }} width={28} />
              <Tooltip formatter={(value) => [formatChartNumber(value), "Names"]} />
              <Bar dataKey="count" radius={[4, 4, 0, 0]}>
                {chartRows.map((row) => <Cell key={row.label} fill={row.fill} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </CardContent>
    </Card>
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
      <div className="grid min-w-0 gap-4 xl:grid-cols-2">
        {rows.map((row) => <ReferenceValuationCard key={textField(row, ["metric"])} row={row} />)}
      </div>
    </section>
  );
}

function ReferenceValuationCard({ row }: { row: RowRecord }) {
  const history = metricHistoryPoints(row);
  const latest = numberField(row, ["latest_value"], Number.NaN);
  const percentile = numberField(row, ["percentile"], Number.NaN);
  const suffix = textField(row, ["suffix"]);
  return (
    <Card className="min-w-0">
      <CardHeader className="flex-row items-start justify-between gap-3 p-4 pb-2">
        <div>
          <CardTitle className="text-base">{textField(row, ["label"])}</CardTitle>
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
          {["5Y", "10Y", "20Y", "50Y", "All"].map((label) => (
            <span key={label} className="rounded-full bg-muted px-2.5 py-0.5 text-xs font-medium text-muted-foreground">{label}</span>
          ))}
          <span className="ml-auto inline-flex items-center gap-2 text-xs text-muted-foreground">
            <span className="h-0.5 w-4 rounded-full bg-red-700" />
            Valuation
            <span className="h-0.5 w-4 rounded-full bg-blue-700" />
            S&P 500
          </span>
        </div>
        <div className="h-72 min-h-72">
          {history.length ? (
            <ResponsiveContainer width="100%" height="100%">
              <ComposedChart data={history} margin={{ left: 0, right: 8, top: 12, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="date" minTickGap={42} tickLine={false} axisLine={false} tick={{ fontSize: 11 }} />
                <YAxis yAxisId="metric" domain={["dataMin", "dataMax"]} tickLine={false} axisLine={false} tick={{ fontSize: 11 }} width={46} />
                <YAxis yAxisId="index" orientation="right" domain={["dataMin", "dataMax"]} hide />
                <Tooltip
                  formatter={(value, name) => {
                    if (name === "S&P 500") return [formatChartNumber(value), "S&P 500"];
                    return [formatMetricValue(typeof value === "number" ? value : Number.NaN, suffix), textField(row, ["label"])];
                  }}
                  labelFormatter={(value) => String(value)}
                />
                <ReferenceLine yAxisId="metric" y={latest} stroke="var(--muted-foreground)" strokeDasharray="3 3" ifOverflow="extendDomain" />
                <Area yAxisId="metric" type="monotone" dataKey="value" stroke="#b42318" strokeWidth={3} fill="#b42318" fillOpacity={0.22} dot={false} name={textField(row, ["label"])} />
                <Line yAxisId="index" type="monotone" dataKey="index_price" stroke="#1d4ed8" strokeDasharray="5 3" strokeWidth={2.75} dot={false} name="S&P 500" connectNulls />
              </ComposedChart>
            </ResponsiveContainer>
          ) : (
            <EmptyChart label="No valuation history" />
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function ValuationScorePanel({ row, buckets }: { row?: RowRecord; buckets: ValuationBucket[] }) {
  const score = numberField(row, ["valuation_score"], Number.NaN);
  const discounted = buckets.find((bucket) => bucket.label === "Discounted")?.count ?? 0;
  const stretched = buckets.find((bucket) => bucket.label === "Stretched")?.count ?? 0;
  const fair = buckets.find((bucket) => bucket.label === "Fair")?.count ?? 0;
  return (
    <div className="rounded-md border border-white/12 bg-black/10 p-4">
      <div className="mb-3 flex items-center justify-between gap-3 text-sm">
        <span className="text-white/62">Cheap</span>
        <span className="text-white/62">Fair</span>
        <span className="text-white/62">Stretched</span>
      </div>
      <div className="relative h-4 overflow-hidden rounded-full bg-white/12">
        <div className="absolute inset-y-0 left-0 w-1/3 bg-emerald-300/55" />
        <div className="absolute inset-y-0 left-1/3 w-1/3 bg-sky-300/45" />
        <div className="absolute inset-y-0 right-0 w-1/3 bg-amber-300/60" />
        <div
          className="absolute top-1/2 size-5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white bg-white shadow"
          style={{ left: `${Number.isFinite(score) ? normalizeScore(score) : 50}%` }}
        />
      </div>
      <div className="mt-4 grid grid-cols-3 gap-2 text-center">
        <div>
          <p className="text-lg font-semibold">{discounted}</p>
          <p className="text-[11px] font-medium uppercase text-white/56">Discounted</p>
        </div>
        <div>
          <p className="text-lg font-semibold">{fair}</p>
          <p className="text-[11px] font-medium uppercase text-white/56">Fair</p>
        </div>
        <div>
          <p className="text-lg font-semibold">{stretched}</p>
          <p className="text-[11px] font-medium uppercase text-white/56">Stretched</p>
        </div>
      </div>
      <p className="mt-3 text-xs leading-5 text-white/62">{valuationCaption(row)}</p>
    </div>
  );
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
        <table className="w-full min-w-[1180px] text-sm">
          <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
            <tr>
              <th className="px-4 py-3">Group</th>
              <th className="px-3 py-3">Symbol</th>
              <th className="px-3 py-3">Trend</th>
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
                <td className="px-3 py-3"><TrendStrip row={row} /></td>
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

function TrendStrip({ row }: { row: RowRecord }) {
  const points = [
    numberField(row, ["return_1w"], Number.NaN),
    numberField(row, ["return_1m"], Number.NaN),
    numberField(row, ["return_ytd"], Number.NaN),
    numberField(row, ["return_1y"], Number.NaN),
  ];
  if (!points.some(Number.isFinite)) return <span className="text-muted-foreground">-</span>;
  const clean = points.map((value) => Number.isFinite(value) ? value : 0);
  const min = Math.min(...clean, 0);
  const max = Math.max(...clean, 0);
  const spread = max - min || 1;
  const path = clean.map((value, index) => {
    const x = 4 + index * 24;
    const y = 28 - ((value - min) / spread) * 22;
    return `${index === 0 ? "M" : "L"}${x},${y}`;
  }).join(" ");
  const last = clean[clean.length - 1];
  const stroke = last >= 0 ? "#15803d" : "#b42318";
  return (
    <div className="flex items-center gap-2">
      <svg className="h-8 w-20 overflow-visible" viewBox="0 0 80 32" aria-hidden="true">
        <path d="M4,28 L76,28" stroke="var(--border)" strokeWidth="1" />
        <path d={path} fill="none" stroke={stroke} strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" />
      </svg>
      <span className="text-[11px] text-muted-foreground">1W/1M/YTD/1Y</span>
    </div>
  );
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

function ScoreBar({ value }: { value: number }) {
  return (
    <div className="min-w-28">
      <div className="mb-1 flex items-center justify-between gap-3">
        <span className="font-medium tabular-nums">{Number.isFinite(value) ? Math.round(value) : "--"}</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-muted">
        <div className="h-full rounded-full" style={{ width: `${normalizeScore(value)}%`, background: scoreColor(value) }} />
      </div>
    </div>
  );
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

function rankTickerRows(inputRows: RowRecord[]): RowRecord[] {
  return [...inputRows].sort((left, right) => {
    const leftPosture = postureRank(textField(left, ["valuation_posture"]));
    const rightPosture = postureRank(textField(right, ["valuation_posture"]));
    if (leftPosture !== rightPosture) return leftPosture - rightPosture;
    return numberField(right, ["valuation_score"], -1) - numberField(left, ["valuation_score"], -1);
  });
}

function valuationBuckets(inputRows: RowRecord[]): ValuationBucket[] {
  const buckets: ValuationBucket[] = [
    { label: "Discounted", count: 0, tone: "success" },
    { label: "Fair", count: 0, tone: "info" },
    { label: "Stretched", count: 0, tone: "warning" },
    { label: "Missing", count: 0, tone: "outline" },
  ];
  for (const row of inputRows) {
    const posture = textField(row, ["valuation_posture"]).toLowerCase();
    if (posture === "discounted") buckets[0].count += 1;
    else if (posture === "stretched") buckets[2].count += 1;
    else if (isFairPosture(posture)) buckets[1].count += 1;
    else buckets[3].count += 1;
  }
  return buckets;
}

function postureRank(value: string): number {
  const normalized = value.toLowerCase();
  if (normalized.includes("discounted")) return 0;
  if (normalized.includes("fair")) return 1;
  if (normalized.includes("stretched")) return 2;
  return 3;
}

function stanceHeadline(posture: string, pressure: RowRecord[], support: RowRecord[]): string {
  const normalized = posture.toLowerCase();
  const pressureText = pressure.length ? pressure.map((row) => textField(row, ["category"])).join(" and ") : "no dominant risk driver";
  const supportText = support.length ? support.map((row) => textField(row, ["category"])).join(" and ") : "limited support";
  if (normalized.includes("defensive")) return `Defensive market: ${pressureText} should cap new risk.`;
  if (normalized.includes("constructive")) return `Constructive market: ${supportText} support normal sizing.`;
  return `Mixed market: ${pressureText} offset by ${supportText}.`;
}

function marketActionLabel(marketRow: RowRecord | undefined, best: RowRecord | undefined, worst: RowRecord | undefined): string {
  const posture = textField(marketRow, ["valuation_posture"]);
  if (best && worst) return `${textField(best, ["symbol"])} before ${textField(worst, ["symbol"])}`;
  return posture ? titleCase(posture) : "Wait for evidence";
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

function valuationCaption(row: RowRecord | undefined): string {
  const ps = numberField(row, ["ps_ratio"], Number.NaN);
  const pe = numberField(row, ["forward_pe"], Number.NaN);
  const gap = numberField(row, ["upside_pct"], Number.NaN);
  const parts = [];
  if (Number.isFinite(ps)) parts.push(`${formatMultiple(ps)} median P/S`);
  if (Number.isFinite(pe)) parts.push(`${formatMultiple(pe)} median forward P/E`);
  if (Number.isFinite(gap)) parts.push(`${formatMaybePct(gap)} median fair-value gap`);
  return parts.length ? parts.join("; ") : "Valuation uses available multiples until fair-value coverage improves.";
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

function scoreTone(value: number): "good" | "warn" | "info" | "muted" {
  if (!Number.isFinite(value)) return "muted";
  if (value >= 65) return "good";
  if (value <= 40) return "warn";
  return "info";
}

function isFairPosture(value: string): boolean {
  const normalized = value.toLowerCase();
  return normalized.includes("fair") || normalized.includes("attractive");
}

function scoreColor(value: number): string {
  if (!Number.isFinite(value)) return "var(--muted-foreground)";
  if (value >= 70) return "var(--success)";
  if (value >= 45) return "var(--primary)";
  return "var(--warning)";
}

function bucketColor(value: string): string {
  if (value === "Discounted") return "var(--success)";
  if (value === "Stretched") return "var(--warning)";
  if (value === "Missing") return "var(--muted-foreground)";
  return "var(--primary)";
}

function normalizeScore(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, value));
}
