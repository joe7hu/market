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
  return bins.filter((bin: Record<string, any>) =>
    typeof bin.mean_predicted === "number" && Number.isFinite(bin.mean_predicted) && bin.mean_predicted >= 0 && bin.mean_predicted <= 1 &&
    typeof bin.observed_rate === "number" && Number.isFinite(bin.observed_rate) && bin.observed_rate >= 0 && bin.observed_rate <= 1 &&
    typeof bin.sample_count === "number" && Number.isInteger(bin.sample_count) && bin.sample_count > 0
  ).map((bin: Record<string, any>) => ({
    label: calibrationBinLabel(bin), predicted: bin.mean_predicted,
    observed: bin.observed_rate, count: bin.sample_count,
  })).sort((a: { predicted: number }, b: { predicted: number }) => a.predicted - b.predicted);
}

export function promptMetric(value: unknown, kind: "number" | "percent" | "brier" = "number"): string {
  if (value == null || value === "") return "—";
  const numeric = Number(value);
  if (typeof value === "boolean" || !Number.isFinite(numeric)) return "—";
  if (kind === "percent") return percent(numeric);
  if (kind === "brier") return numeric.toFixed(3);
  return numeric.toLocaleString();
}

export function predictionState(value: unknown): string {
  return humanize(value, "Awaiting outcome");
}
