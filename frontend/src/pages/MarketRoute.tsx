import { Activity, ArrowDownRight, ArrowUpRight, BarChart3, Gauge, ListChecks, ShieldAlert } from "lucide-react";
import type { ReactNode } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
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

type ValuationBucket = {
  label: string;
  count: number;
  tone: "success" | "warning" | "info" | "outline";
};

export function MarketRoute() {
  const { data } = useMarketData();
  usePanelScope("market");

  const valuationRows = rows(data.marketValuationCharts);
  const environmentRows = rows(data.marketEnvironmentModel);
  const marketRow = valuationRows.find((row) => textField(row, ["scope"]) === "whole_market");
  const tickerRows = valuationRows.filter((row) => textField(row, ["scope"]) !== "whole_market");
  const overall = environmentRows.find((row) => textField(row, ["category"]) === "Overall");
  const drivers = environmentRows.filter((row) => textField(row, ["category"]) !== "Overall");
  const buckets = valuationBuckets(tickerRows);
  const discounted = buckets.find((bucket) => bucket.label === "Discounted")?.count ?? 0;
  const stretched = buckets.find((bucket) => bucket.label === "Stretched")?.count ?? 0;
  const marketScore = numberField(marketRow, ["valuation_score"], Number.NaN);
  const metrics: MetricSpec[] = [
    ["Regime", titleCase(textField(overall, ["posture"], "Not loaded")), textField(overall, ["next_action"], "Refresh market scope"), toneFromText(textField(overall, ["posture"]))],
    ["Valuation", Number.isFinite(marketScore) ? `${Math.round(marketScore)} / 100` : "-", valuationCaption(marketRow), scoreTone(marketScore)],
    ["Watchlist", `${tickerRows.length}`, "valuation rows with price history", tickerRows.length ? "info" : "warn"],
    ["Tilt", `${discounted}/${stretched}`, "discounted / stretched", stretched > discounted ? "warn" : discounted > stretched ? "good" : "info"],
  ];

  return (
    <WorkspacePage
      eyebrow="Market stance"
      title="Where the Market Stands"
      subtitle="The model turns valuation, trend, breadth, liquidity, event risk, and portfolio exposure into an action posture."
      metrics={metrics}
    >
      <StanceHero overall={overall} marketRow={marketRow} drivers={drivers} tickerRows={tickerRows} buckets={buckets} />

      <section className="grid min-w-0 gap-4 xl:grid-cols-[0.95fr_1.05fr]">
        <EnvironmentModel overall={overall} rows={drivers} />
        <MarketValuationMap marketRow={marketRow} tickerRows={tickerRows} buckets={buckets} />
      </section>

      <ActionLanes rows={tickerRows} />

      <WatchlistValuationBoard rows={tickerRows} />

      <section className="grid gap-4 xl:grid-cols-2">
        <DataGridSection title="Portfolio Effects" rows={rows(data.marketContext)} />
        <DataGridSection title="Risk Overrides" rows={rows(data.portfolioRiskCards)} />
      </section>
    </WorkspacePage>
  );
}

function StanceHero({ overall, marketRow, drivers, tickerRows, buckets }: { overall?: RowRecord; marketRow?: RowRecord; drivers: RowRecord[]; tickerRows: RowRecord[]; buckets: ValuationBucket[] }) {
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

  return (
    <section className="min-w-0 overflow-hidden rounded-lg border border-border bg-[linear-gradient(135deg,#14261f_0%,#203226_46%,#36412a_100%)] text-white shadow-sm">
      <div className="grid gap-0 xl:grid-cols-[1.2fr_0.8fr]">
        <div className="min-w-0 space-y-6 p-5 sm:p-6">
          <div className="flex flex-wrap items-center gap-3">
            <Badge variant={postureBadge(posture)}>{titleCase(posture)}</Badge>
            <span className="text-sm text-white/70">{Number.isFinite(score) ? `${Math.round(score)} / 100 environment score` : "Environment score unavailable"}</span>
          </div>
          <div>
            <h2 className="max-w-3xl break-words text-2xl font-semibold tracking-normal sm:text-3xl">
              {stanceHeadline(posture, pressure, support)}
            </h2>
            <p className="mt-3 max-w-3xl break-words text-sm leading-6 text-white/72">
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
              detail={textField(overall, ["portfolio_effect"], "Use staged sizing until the model has enough evidence.")}
            />
          </div>
        </div>
        <div className="min-w-0 border-t border-white/12 bg-white/[0.06] p-5 sm:p-6 xl:border-l xl:border-t-0">
          <div className="mb-4 flex items-center justify-between gap-3">
            <div>
              <p className="text-xs font-semibold uppercase text-white/58">Market valuation score</p>
              <p className="mt-1 text-xl font-semibold">{formatScore(numberField(marketRow, ["valuation_score"], Number.NaN))}</p>
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
                  <p className="text-xs text-muted-foreground">{Number.isFinite(score) ? Math.round(score) : "--"} / 100</p>
                </div>
                <div className="min-w-0">
                  <div className="h-2 overflow-hidden rounded-full bg-muted">
                    <div className="h-full rounded-full" style={{ width: `${normalizeScore(score)}%`, background: scoreColor(score) }} />
                  </div>
                  <p className="mt-2 line-clamp-2 text-xs leading-5 text-muted-foreground">{textField(row, ["evidence"])}</p>
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
  const discounted = rankTickerRows(tickerRows).filter((row) => textField(row, ["valuation_posture"]) === "discounted").slice(0, 4);
  const stretched = rankTickerRows(tickerRows).filter((row) => textField(row, ["valuation_posture"]) === "stretched").slice(-4).reverse();

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
        <div className="grid gap-3 md:grid-cols-2">
          <OutlierList icon={<ArrowDownRight className="size-4" />} title="Cheapest Queue" rows={discounted} emptyLabel="No discounted watchlist names" />
          <OutlierList icon={<ArrowUpRight className="size-4" />} title="Wait Queue" rows={stretched} emptyLabel="No stretched watchlist names" />
        </div>
      </CardContent>
    </Card>
  );
}

function OutlierList({ icon, title, rows, emptyLabel }: { icon: ReactNode; title: string; rows: RowRecord[]; emptyLabel: string }) {
  return (
    <div className="rounded-md border border-border bg-background">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2 text-xs font-semibold uppercase text-muted-foreground">
        {icon}
        {title}
      </div>
      <div className="divide-y divide-border">
        {rows.length ? rows.map((row) => (
          <div key={textField(row, ["symbol"])} className="grid grid-cols-[56px_1fr_auto] items-center gap-3 px-3 py-2">
            <span className="font-semibold">{textField(row, ["symbol"])}</span>
            <span className="truncate text-xs text-muted-foreground">{textField(row, ["next_action"])}</span>
            <span className="text-sm font-medium tabular-nums">{formatMaybePct(numberField(row, ["upside_pct"], Number.NaN))}</span>
          </div>
        )) : <div className="px-3 py-4 text-xs text-muted-foreground">{emptyLabel}</div>}
      </div>
    </div>
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

function ActionLanes({ rows }: { rows: RowRecord[] }) {
  const ranked = rankTickerRows(rows);
  const lanes = [
    {
      title: "Add Only With Discount",
      tone: "success" as const,
      rows: ranked.filter((row) => textField(row, ["valuation_posture"]) === "discounted").slice(0, 5),
    },
    {
      title: "Hold And Monitor",
      tone: "info" as const,
      rows: ranked.filter((row) => isFairPosture(textField(row, ["valuation_posture"]))).slice(0, 5),
    },
    {
      title: "Wait For Reset",
      tone: "warning" as const,
      rows: ranked.filter((row) => textField(row, ["valuation_posture"]) === "stretched").reverse().slice(0, 5),
    },
  ];

  return (
    <section>
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="flex items-center gap-2 text-sm font-semibold uppercase text-muted-foreground">
          <Activity className="size-4" />
          Action Lanes
        </h2>
        <Badge variant="outline">{rows.length} watchlist names</Badge>
      </div>
      <div className="grid gap-4 xl:grid-cols-3">
        {lanes.map((lane) => (
          <Card key={lane.title} className="min-w-0">
            <CardHeader className="flex-row items-center justify-between gap-3 p-4 pb-2">
              <CardTitle className="text-sm">{lane.title}</CardTitle>
              <Badge variant={lane.tone}>{lane.rows.length}</Badge>
            </CardHeader>
            <CardContent className="p-4 pt-2">
              <div className="divide-y divide-border rounded-md border border-border bg-background">
                {lane.rows.length ? lane.rows.map((row) => (
                  <div key={textField(row, ["symbol"])} className="grid grid-cols-[64px_1fr_auto] items-center gap-3 px-3 py-3">
                    <div className="min-w-0">
                      <p className="font-semibold">{textField(row, ["symbol"])}</p>
                      <p className="truncate text-xs text-muted-foreground">{textField(row, ["name"])}</p>
                    </div>
                    <p className="line-clamp-2 text-xs leading-5 text-muted-foreground">{textField(row, ["next_action"])}</p>
                    <span className="text-sm font-semibold tabular-nums">{formatMaybePct(numberField(row, ["upside_pct"], Number.NaN))}</span>
                  </div>
                )) : <div className="px-3 py-5 text-sm text-muted-foreground">No names in this lane.</div>}
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </section>
  );
}

function WatchlistValuationBoard({ rows }: { rows: RowRecord[] }) {
  const sorted = rankTickerRows(rows);
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
    <Card className="min-w-0 overflow-hidden">
      <CardHeader className="flex-row items-center justify-between gap-3 p-4 pb-2">
        <CardTitle className="flex items-center gap-2 text-base">
          <ListChecks className="size-4 text-muted-foreground" />
          Watchlist Valuation Board
        </CardTitle>
        <Badge variant="outline">{sorted.length} names</Badge>
      </CardHeader>
      <CardContent className="overflow-x-auto p-0">
        <table className="w-full min-w-[980px] text-sm">
          <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
            <tr>
              <th className="px-4 py-3">Ticker</th>
              <th className="px-3 py-3">Price vs fair value</th>
              <th className="px-3 py-3">Gap</th>
              <th className="px-3 py-3">P/E</th>
              <th className="px-3 py-3">P/S</th>
              <th className="px-3 py-3">Score</th>
              <th className="px-3 py-3">Posture</th>
              <th className="px-4 py-3">Next action</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => (
              <tr key={textField(row, ["symbol"])} className="border-b border-border align-middle transition-colors hover:bg-accent/40">
                <td className="px-4 py-3">
                  <p className="font-semibold">{textField(row, ["symbol"])}</p>
                  <p className="max-w-36 truncate text-xs text-muted-foreground">{textField(row, ["name"])}</p>
                </td>
                <td className="px-3 py-3">
                  <TickerSparkline row={row} />
                </td>
                <td className="px-3 py-3 font-medium tabular-nums">{formatMaybePct(numberField(row, ["upside_pct"], Number.NaN))}</td>
                <td className="px-3 py-3 tabular-nums text-muted-foreground">{formatMultiple(numberField(row, ["forward_pe"], Number.NaN))}</td>
                <td className="px-3 py-3 tabular-nums text-muted-foreground">{formatMultiple(numberField(row, ["ps_ratio"], Number.NaN))}</td>
                <td className="px-3 py-3">
                  <ScoreBar value={numberField(row, ["valuation_score"], Number.NaN)} />
                </td>
                <td className="px-3 py-3">
                  <Badge variant={postureBadge(textField(row, ["valuation_posture"]))}>{titleCase(textField(row, ["valuation_posture"]))}</Badge>
                </td>
                <td className="max-w-[340px] px-4 py-3 text-xs leading-5 text-muted-foreground">{textField(row, ["next_action"])}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

function WholeMarketChart({ row, dark = false }: { row?: RowRecord; dark?: boolean }) {
  const history = historyPoints(row);
  if (!history.length) return <EmptyChart label={displayField(row, ["coverage"], "No valuation history")} dark={dark} />;
  return (
    <div className="h-60 min-h-60">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={history} margin={{ left: 0, right: 8, top: 12, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" vertical={false} stroke={dark ? "rgba(255,255,255,0.18)" : "var(--border)"} />
          <XAxis dataKey="date" minTickGap={42} tickLine={false} axisLine={false} tick={{ fontSize: 11, fill: dark ? "rgba(255,255,255,0.58)" : "var(--muted-foreground)" }} />
          <YAxis domain={["dataMin", "dataMax"]} tickLine={false} axisLine={false} tick={{ fontSize: 11, fill: dark ? "rgba(255,255,255,0.58)" : "var(--muted-foreground)" }} width={44} />
          <Tooltip formatter={(value) => formatChartNumber(value)} labelFormatter={(value) => String(value)} />
          <Line type="monotone" dataKey="price" stroke={dark ? "#a7f3d0" : "var(--primary)"} strokeWidth={2.5} dot={false} name="Indexed price" />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function TickerSparkline({ row }: { row: RowRecord }) {
  const history = historyPoints(row);
  if (!history.length) return <span className="text-xs text-muted-foreground">{displayField(row, ["coverage"], "No history")}</span>;
  return (
    <div className="h-16 w-56">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={history} margin={{ left: 2, right: 2, top: 4, bottom: 4 }}>
          <YAxis domain={["dataMin", "dataMax"]} hide />
          <XAxis dataKey="date" hide />
          <ReferenceLine y={numberField(row, ["fair_value"], Number.NaN)} stroke="var(--muted-foreground)" strokeDasharray="3 3" ifOverflow="extendDomain" />
          <Line type="monotone" dataKey="price" stroke="var(--primary)" strokeWidth={2} dot={false} name="Price" />
          <Line type="monotone" dataKey="fair_value" stroke="var(--muted-foreground)" strokeDasharray="4 4" strokeWidth={1.25} dot={false} name="Fair value" />
          <Tooltip formatter={(value, name) => [formatChartNumber(value), name === "fair_value" ? "Fair value" : "Price"]} labelFormatter={(value) => String(value)} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
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
  return <Badge variant={postureBadge(posture)}>{Number.isFinite(value) ? Math.round(value) : "--"}</Badge>;
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

function formatScore(value: number): string {
  return Number.isFinite(value) ? `${Math.round(value)} / 100` : "-";
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

function valuationTone(value: number): "good" | "warn" | "info" | "muted" {
  if (!Number.isFinite(value)) return "muted";
  if (value >= 10) return "good";
  if (value <= -10) return "warn";
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
