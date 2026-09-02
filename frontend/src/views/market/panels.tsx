import { useMemo, useState } from "react";
import { Activity, BarChart3, Gauge } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/market/workstation";
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
  latestAssetMatrixDate,
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
  snapshotRows,
  coverageRows,
  posteriorRows = [],
  coverageVectorRows = [],
  scenarioRows = [],
  optionSlaRows = [],
  observationRows = [],
  freshness,
}: {
  rows: RowRecord[];
  referenceRows: RowRecord[];
  assetRows: RowRecord[];
  snapshotRows: RowRecord[];
  coverageRows: RowRecord[];
  posteriorRows?: RowRecord[];
  coverageVectorRows?: RowRecord[];
  scenarioRows?: RowRecord[];
  optionSlaRows?: RowRecord[];
  observationRows?: RowRecord[];
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
        <MarketStateProjection snapshotRows={snapshotRows} coverageRows={coverageRows} posteriorRows={posteriorRows} coverageVectorRows={coverageVectorRows} scenarioRows={scenarioRows} optionSlaRows={optionSlaRows} observationRows={observationRows} />
      </CardContent>
    </Card>
  );
}

export function MarketStateProjection({ snapshotRows, coverageRows, posteriorRows = [], coverageVectorRows = [], scenarioRows = [], optionSlaRows = [], observationRows = [] }: { snapshotRows: RowRecord[]; coverageRows: RowRecord[]; posteriorRows?: RowRecord[]; coverageVectorRows?: RowRecord[]; scenarioRows?: RowRecord[]; optionSlaRows?: RowRecord[]; observationRows?: RowRecord[] }) {
  const snapshot = snapshotRows[0];
  const horizons = snapshot && isRecord(snapshot.horizons) ? snapshot.horizons : {};
  const horizonEntries = Object.entries(horizons);
  const regimes = snapshot && isRecord(snapshot.regime_distributions) ? snapshot.regime_distributions : {};
  const comparisons = snapshot && isRecord(snapshot.baseline_challenger) ? snapshot.baseline_challenger : {};
  return (
    <div className="space-y-3 rounded-md border border-border/80 bg-background/60 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-sm font-semibold">Point-in-time market state</p>
          <p className="text-xs text-muted-foreground">Four horizons · decision requirements and unavailable dimensions stay visible.</p>
        </div>
        <StatusBadge tone="muted">Decision-bound evidence</StatusBadge>
      </div>
      {horizonEntries.length ? (
        <div className="grid gap-2 sm:grid-cols-2">
          {horizonEntries.map(([horizon, value]) => (
            <div key={horizon} className="rounded border border-border/70 p-2">
              <p className="text-xs font-semibold">{horizon}</p>
              <div className="mt-2 space-y-1">
                {(Array.isArray(value) ? value : []).map((item, index) => {
                  const row = isRecord(item) ? item : {};
                  const state = typeof row.state === "string" ? row.state : "unavailable";
                  const status = typeof row.evidence_status === "string" ? row.evidence_status : "unavailable";
                  const source = typeof row.selected_source === "string" ? row.selected_source : "source unavailable";
                  const priority = Array.isArray(row.source_priority) ? row.source_priority.slice(0, 2).join(" → ") : "";
                  const distribution = isRecord(row.regime_distribution) ? Object.entries(row.regime_distribution).map(([key, value]) => `${key} ${String(value)}`).join(" · ") : "";
                  return <p key={`${horizon}:${String(row.dimension ?? index)}`} className="text-[11px] text-muted-foreground"><span className="text-foreground">{String(row.dimension ?? "dimension")}</span>: {state} · {status} · {source}{priority ? ` · priority ${priority}` : ""}{distribution ? ` · ${distribution}` : ""}</p>;
                })}
              </div>
            </div>
          ))}
        </div>
      ) : <EmptyChart label="Market state is unavailable at this cutoff" />}
      <div className="grid gap-2 sm:grid-cols-2">
        {Object.entries(regimes).map(([horizon, value]) => {
          const regime = isRecord(value) ? value : {};
          const distribution = isRecord(regime.distribution) ? Object.entries(regime.distribution).map(([key, item]) => `${key} ${String(item)}`).join(" · ") : "unavailable";
          return <div key={`regime:${horizon}`} className="rounded border border-border/70 p-2 text-[11px] text-muted-foreground">
            <p className="font-semibold text-foreground">{horizon} regime evidence</p>
            <p>{typeof regime.status === "string" ? regime.status : "unavailable"} · distribution: {distribution}</p>
            <p>method/version: {typeof regime.method === "string" ? regime.method : "unavailable"} / {typeof regime.version === "string" ? regime.version : "unavailable"} · sample: {regime.sample_count == null ? "unavailable" : String(regime.sample_count)}</p>
            <p>uncertainty: {typeof regime.uncertainty === "string" ? regime.uncertainty : "unavailable"}</p>
          </div>;
        })}
        {Object.entries(comparisons).map(([horizon, value]) => {
          const comparison = isRecord(value) ? value : {};
          const baseline = isRecord(comparison.baseline) ? comparison.baseline : {};
          const challenger = isRecord(comparison.challenger) ? comparison.challenger : {};
          return <div key={`comparison:${horizon}`} className="rounded border border-border/70 p-2 text-[11px] text-muted-foreground">
            <p className="font-semibold text-foreground">{horizon} baseline / challenger</p>
            <p>baseline: {String(baseline.status ?? "unavailable")} · {String(baseline.method ?? "method unavailable")} / {String(baseline.version ?? "version unavailable")} · sample {baseline.sample_count == null ? "unavailable" : String(baseline.sample_count)}</p>
            <p>challenger: {String(challenger.status ?? "unavailable")} · {String(challenger.method ?? "method unavailable")} / {String(challenger.version ?? "version unavailable")} · sample {challenger.sample_count == null ? "unavailable" : String(challenger.sample_count)}</p>
          </div>;
        })}
      </div>
      <div>
        <p className="mb-2 text-xs font-semibold uppercase tracking-[0.12em] text-muted-foreground">Coverage matrix · {coverageRows.length} rows</p>
        <div className="grid gap-1 sm:grid-cols-2">
          {coverageRows.map((row, index) => (
            <p key={`${textField(row, ["horizon"])}:${textField(row, ["dimension"])}:${index}`} className="text-[11px] text-muted-foreground">
              <span className="text-foreground">{textField(row, ["horizon"])} / {textField(row, ["dimension"])}</span> · {textField(row, ["current_status"], "unavailable")} · {textField(row, ["decision_impact"], "context")} · {textField(row, ["selected_source"], "source unavailable")} · priority {textField(row, ["source_priority"], "unavailable")} · PIT {String(row.point_in_time_safe ?? "unavailable")} · {textField(row, ["blockers"], "no blockers")}
            </p>
          ))}
        </div>
      </div>
      <Phase2Evidence posteriorRows={posteriorRows} coverageVectorRows={coverageVectorRows} scenarioRows={scenarioRows} optionSlaRows={optionSlaRows} observationRows={observationRows} />
    </div>
  );
}

function Phase2Evidence({ posteriorRows, coverageVectorRows, scenarioRows, optionSlaRows, observationRows }: { posteriorRows: RowRecord[]; coverageVectorRows: RowRecord[]; scenarioRows: RowRecord[]; optionSlaRows: RowRecord[]; observationRows: RowRecord[] }) {
  const posterior = isRecord(posteriorRows[0]?.payload) ? posteriorRows[0].payload : posteriorRows[0];
  const status = String(posteriorRows[0]?.status ?? (isRecord(posterior) ? posterior.status : undefined) ?? "MISSING_HISTORY").toUpperCase();
  const confidence = isRecord(posterior) ? String(posterior.overall_confidence ?? "unavailable") : "unavailable";
  const missingness = isRecord(posterior) ? String(posterior.missingness ?? "unavailable") : "unavailable";
  const sla = isRecord(optionSlaRows[0]?.payload) ? optionSlaRows[0].payload : optionSlaRows[0];
  const tone = phase2StatusTone(status);
  return <div className="space-y-2 rounded border border-border/70 bg-muted/20 p-3 text-[11px]">
    <div className="flex flex-wrap items-center justify-between gap-2"><p className="text-sm font-semibold">Phase 2 evidence</p><StatusBadge tone={tone}>{status}</StatusBadge></div>
    <p className="text-muted-foreground">Posterior status is {status}. Observable baseline and bounded latent challenger are read-only. Advisory-only: no rank or execution authorization.</p>
    <p>Posterior confidence: {confidence} · missingness: {missingness} · retained source facts: {observationRows.length} · reproducible scenarios: {scenarioRows.length}</p>
    <p>Per-expression coverage rows: {coverageVectorRows.length} · option OI/volume SLA: {isRecord(sla) ? String(sla.status ?? "MISSING_HISTORY").toUpperCase() : "MISSING_HISTORY"} · positioning allowed: {isRecord(sla) ? String(sla.positioning_allowed ?? false) : "false"}</p>
  </div>;
}

function phase2StatusTone(status: string): "good" | "warn" | "bad" | "muted" {
  if (status === "AVAILABLE") return "good";
  if (status === "FALLBACK" || status === "STALE") return "warn";
  if (["MISSING_SOURCE", "MISSING_HISTORY", "UNSUPPORTED", "CONFLICTED"].includes(status)) return "bad";
  return "muted";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
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
  const latestAsOf = latestAssetMatrixDate(rows);
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
        <div className="flex flex-wrap justify-end gap-2">
          {latestAsOf ? <Badge variant="secondary">As of {latestAsOf}</Badge> : null}
          <Badge variant="outline">{rows.length} rows</Badge>
        </div>
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
