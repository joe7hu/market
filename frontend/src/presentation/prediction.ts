import { humanize, percent } from "./labels";
import { learningProgress } from "./lifecycle";

export const DEFAULT_PROMOTION_OUTCOMES = 30;

export function predictionProgress(quality: Record<string, any>, required = DEFAULT_PROMOTION_OUTCOMES) {
  return learningProgress(quality.valid_resolved_claims ?? quality.brier_sample_count ?? 0, quality.required_independent_outcomes ?? required, required);
}

export function calibrationBinLabel(bin: Record<string, any>): string {
  const lower = Math.round(Number(bin.lower_bound ?? Number(bin.bin ?? 0) / 10) * 100);
  const upper = Math.round(Number(bin.upper_bound ?? (Number(bin.bin ?? 0) + 1) / 10) * 100);
  return `${lower}–${upper}%`;
}

export function calibrationPoints(quality: Record<string, any>): Array<{ label: string; predicted: number; observed: number; count: number }> {
  const bins = Array.isArray(quality.calibration_bins) ? quality.calibration_bins : [];
  return bins.map((bin: Record<string, any>) => ({
    label: calibrationBinLabel(bin),
    predicted: Number(bin.mean_predicted ?? 0),
    observed: Number(bin.observed_rate ?? 0),
    count: Number(bin.sample_count ?? 0),
  })).filter((point) => Number.isFinite(point.predicted) && Number.isFinite(point.observed));
}

export function promptMetric(value: unknown, kind: "number" | "percent" | "brier" = "number"): string {
  if (value == null || value === "") return "—";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  if (kind === "percent") return percent(numeric);
  if (kind === "brier") return numeric.toFixed(3);
  return numeric.toLocaleString();
}

export function predictionState(value: unknown): string {
  return humanize(value, "Awaiting outcome");
}
