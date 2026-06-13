import { Activity } from "lucide-react";
import { useMemo } from "react";

import { DataTableFrame, EmptyState, StatusBadge } from "@/components/market/workstation";
import { cn } from "@/lib/utils";
import type { RowRecord } from "@/types";
import { booleanField, numberField, textField } from "./rowFormat";

// Probability-calibration dashboard (Phase 4). Reads conviction_calibration bins (Phase 2a:
// predicted vs realized P(2x) with Wilson intervals per predicted-probability bin) and renders
// a reliability diagram + per-bin table. The table comes online only once shadow trades reach
// their 2x horizon, so it degrades to an explanatory empty state until mature observations exist.

type CalibrationPanelProps = {
  rows: RowRecord[];
  strategyVersion?: string;
};

type Bin = {
  index: number;
  lo: number;
  hi: number;
  n: number;
  matureN: number;
  predicted: number;
  realized: number;
  realized5x: number;
  wilsonLo: number;
  wilsonHi: number;
  brier: number;
};

export function CalibrationPanel({ rows, strategyVersion }: CalibrationPanelProps) {
  const scoped = useMemo(() => {
    const filtered = strategyVersion ? rows.filter((row) => textField(row, ["strategy_version"]) === strategyVersion) : rows;
    return filtered.length ? filtered : rows;
  }, [rows, strategyVersion]);

  const bins = useMemo<Bin[]>(
    () =>
      scoped
        .map((row) => ({
          index: numberField(row, ["bin_index"], 0),
          lo: numberField(row, ["bin_lo"], Number.NaN),
          hi: numberField(row, ["bin_hi"], Number.NaN),
          n: numberField(row, ["n"], 0),
          matureN: numberField(row, ["mature_n"], 0),
          predicted: numberField(row, ["predicted_p2x"], Number.NaN),
          realized: numberField(row, ["realized_p2x"], Number.NaN),
          realized5x: numberField(row, ["realized_p5x"], Number.NaN),
          wilsonLo: numberField(row, ["wilson_lo"], Number.NaN),
          wilsonHi: numberField(row, ["wilson_hi"], Number.NaN),
          brier: numberField(row, ["brier"], Number.NaN),
        }))
        .filter((bin) => bin.n > 0 && Number.isFinite(bin.predicted) && Number.isFinite(bin.realized))
        .sort((left, right) => left.index - right.index),
    [scoped],
  );

  const summary = useMemo(() => {
    const totalN = bins.reduce((sum, bin) => sum + bin.n, 0);
    const matureN = bins.reduce((sum, bin) => sum + bin.matureN, 0);
    const weighted = bins.reduce((sum, bin) => (Number.isFinite(bin.brier) ? sum + bin.brier * bin.n : sum), 0);
    const brierDenom = bins.reduce((sum, bin) => (Number.isFinite(bin.brier) ? sum + bin.n : sum), 0);
    const calibrated = scoped.some((row) => booleanField(row, ["calibrated"]));
    return { totalN, matureN, brier: brierDenom ? weighted / brierDenom : Number.NaN, calibrated };
  }, [bins, scoped]);

  const version = strategyVersion ?? (scoped.length ? textField(scoped[0], ["strategy_version"]) : "");

  if (!bins.length) {
    return (
      <DataTableFrame title="Probability Calibration">
        <EmptyState
          title="Calibration not online yet"
          detail="Predicted P(2x) is checked against realized 2x outcomes once shadow trades reach their horizon. No matured bins exist for this strategy yet."
          icon={Activity}
        />
      </DataTableFrame>
    );
  }

  return (
    <DataTableFrame
      title="Probability Calibration"
      action={
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          {version ? <StatusBadge tone="muted">{version}</StatusBadge> : null}
          <StatusBadge tone={summary.calibrated ? "good" : "warn"}>{summary.calibrated ? "Calibrated" : "Provisional"}</StatusBadge>
          <span>{summary.totalN.toLocaleString()} obs</span>
          <span>{summary.matureN.toLocaleString()} mature</span>
          {Number.isFinite(summary.brier) ? <span>Brier {summary.brier.toFixed(3)}</span> : null}
        </div>
      }
    >
      <div className="grid gap-4 p-3 lg:grid-cols-[minmax(0,280px)_minmax(0,1fr)]">
        <ReliabilityDiagram bins={bins} />
        <BinTable bins={bins} />
      </div>
    </DataTableFrame>
  );
}

function ReliabilityDiagram({ bins }: { bins: Bin[] }) {
  const max = useMemo(() => {
    const candidates = bins.flatMap((bin) => [bin.predicted, bin.realized, bin.wilsonHi].filter(Number.isFinite));
    const peak = candidates.length ? Math.max(...candidates) : 1;
    return Math.min(1, Math.max(0.1, Math.ceil(peak * 11) / 10)); // round up to a tenth, clamp 0.1..1
  }, [bins]);

  const size = 240;
  const pad = { top: 10, right: 10, bottom: 26, left: 30 };
  const innerW = size - pad.left - pad.right;
  const innerH = size - pad.top - pad.bottom;
  const xFor = (value: number) => pad.left + (value / max) * innerW;
  const yFor = (value: number) => pad.top + (1 - value / max) * innerH;
  const radiusFor = (n: number) => {
    const maxN = Math.max(...bins.map((bin) => bin.n), 1);
    return 3 + (Math.sqrt(n) / Math.sqrt(maxN)) * 5;
  };
  const ticks = [0, max / 2, max];

  return (
    <div>
      <svg viewBox={`0 0 ${size} ${size}`} className="w-full" role="img" aria-label="Reliability diagram: predicted vs realized P(2x)">
        {ticks.map((tick) => (
          <g key={`t${tick}`}>
            <line x1={xFor(tick)} y1={pad.top} x2={xFor(tick)} y2={size - pad.bottom} className="stroke-border" strokeWidth={0.5} />
            <line x1={pad.left} y1={yFor(tick)} x2={size - pad.right} y2={yFor(tick)} className="stroke-border" strokeWidth={0.5} />
            <text x={xFor(tick)} y={size - pad.bottom + 12} textAnchor="middle" className="fill-muted-foreground text-[9px]">{formatProb(tick)}</text>
            <text x={pad.left - 4} y={yFor(tick) + 3} textAnchor="end" className="fill-muted-foreground text-[9px]">{formatProb(tick)}</text>
          </g>
        ))}
        {/* perfect-calibration diagonal */}
        <line x1={xFor(0)} y1={yFor(0)} x2={xFor(max)} y2={yFor(max)} className="stroke-muted-foreground" strokeWidth={1} strokeDasharray="4 3" />
        {bins.map((bin) => {
          const overconfident = bin.realized < bin.predicted;
          return (
            <g key={bin.index} className={overconfident ? "text-rose-500" : "text-emerald-500"}>
              {Number.isFinite(bin.wilsonLo) && Number.isFinite(bin.wilsonHi) ? (
                <line x1={xFor(bin.predicted)} y1={yFor(bin.wilsonLo)} x2={xFor(bin.predicted)} y2={yFor(bin.wilsonHi)} className="stroke-current opacity-50" strokeWidth={1} />
              ) : null}
              <circle cx={xFor(bin.predicted)} cy={yFor(bin.realized)} r={radiusFor(bin.n)} className="fill-current opacity-80" />
            </g>
          );
        })}
        <text x={pad.left + innerW / 2} y={size - 2} textAnchor="middle" className="fill-muted-foreground text-[9px]">Predicted P(2x)</text>
        <text x={9} y={pad.top + innerH / 2} textAnchor="middle" transform={`rotate(-90 9 ${pad.top + innerH / 2})`} className="fill-muted-foreground text-[9px]">Realized</text>
      </svg>
      <div className="mt-1 flex items-center justify-center gap-4 text-[10px] text-muted-foreground">
        <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-emerald-500" /> under-promised</span>
        <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-rose-500" /> over-confident</span>
      </div>
    </div>
  );
}

function BinTable({ bins }: { bins: Bin[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-border text-left text-[10px] uppercase text-muted-foreground">
            <th className="py-1 pr-2 font-medium">Predicted bin</th>
            <th className="py-1 pr-2 text-right font-medium">n</th>
            <th className="py-1 pr-2 text-right font-medium">Predicted</th>
            <th className="py-1 pr-2 text-right font-medium">Realized (95% CI)</th>
            <th className="py-1 pr-2 text-right font-medium">5x</th>
            <th className="py-1 text-right font-medium">Brier</th>
          </tr>
        </thead>
        <tbody>
          {bins.map((bin) => {
            const gap = bin.realized - bin.predicted;
            return (
              <tr key={bin.index} className="border-b border-border/50 last:border-0">
                <td className="py-1 pr-2 tabular-nums text-foreground">{formatProb(bin.lo)}–{formatProb(bin.hi)}</td>
                <td className="py-1 pr-2 text-right tabular-nums text-muted-foreground">{bin.n.toLocaleString()}{bin.matureN < bin.n ? <span className="text-amber-500"> ·{bin.matureN}m</span> : null}</td>
                <td className="py-1 pr-2 text-right tabular-nums text-muted-foreground">{formatProb(bin.predicted)}</td>
                <td className={cn("py-1 pr-2 text-right tabular-nums", gapTone(gap))}>
                  {formatProb(bin.realized)}
                  {Number.isFinite(bin.wilsonLo) && Number.isFinite(bin.wilsonHi) ? (
                    <span className="text-muted-foreground"> [{formatProb(bin.wilsonLo)}, {formatProb(bin.wilsonHi)}]</span>
                  ) : null}
                </td>
                <td className="py-1 pr-2 text-right tabular-nums text-muted-foreground">{Number.isFinite(bin.realized5x) ? formatProb(bin.realized5x) : "—"}</td>
                <td className="py-1 text-right tabular-nums text-muted-foreground">{Number.isFinite(bin.brier) ? bin.brier.toFixed(3) : "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function formatProb(value: number): string {
  return Number.isFinite(value) ? `${(value * 100).toFixed(0)}%` : "—";
}

function gapTone(gap: number): string {
  if (!Number.isFinite(gap)) return "text-muted-foreground";
  if (gap > 0.03) return "text-emerald-600 dark:text-emerald-400";
  if (gap < -0.03) return "text-rose-600 dark:text-rose-400";
  return "text-foreground";
}
