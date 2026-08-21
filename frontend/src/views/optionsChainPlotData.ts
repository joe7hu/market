import type { OptionHistoryCurves, OptionHistorySurfaceGrid } from "@/api/options";

type CurveRecord = Record<string, unknown>;

export type CurveChoice = { key: string; label: string };
export type CurveTrace = { name: string; x: Array<number | string>; y: number[]; text: string[] };
export type ProviderIVSurfaceData = {
  x: number[];
  y: number[];
  z: Array<Array<number | null>>;
  observed: Array<{ logMoneyness: number; dte: number; providerIV: number; strike: number | null }>;
  heatmap: Array<[number, number, number]>;
  surfacePoints: Array<[number, number, number | null]>;
  dataShape: [number, number];
  ivDomain: [number, number];
  selectedDte: number | null;
  selectedSlice: Array<[number, number]>;
};

export type SurfaceViewportPreset = "focus" | "standard";

const SURFACE_VIEWPORTS: Record<SurfaceViewportPreset, { maxAbsMoneyness: number; maxDte: number }> = {
  focus: { maxAbsMoneyness: 0.12, maxDte: 120 },
  standard: { maxAbsMoneyness: 0.30, maxDte: 365 },
};

/**
 * The grid is deliberately a display interpolation of provider IV only. The
 * observed trace remains the source-faithful evidence, and null cells preserve
 * the no-extrapolation boundary from the API.
 */
export function buildProviderIVSurfaceData(
  surface: OptionHistorySurfaceGrid,
  optionType: "call" | "put",
  preset: SurfaceViewportPreset = "focus",
  requestedDte?: number,
): ProviderIVSurfaceData {
  const viewport = SURFACE_VIEWPORTS[preset];
  const source = surface.surfaces[optionType] ?? [];
  const xIndices = surface.x
    .map((value, index) => ({ value, index }))
    .filter(({ value }) => finiteNumber(value) && Math.abs(value) <= viewport.maxAbsMoneyness);
  const yIndices = surface.y
    .map((value, index) => ({ value, index }))
    .filter(({ value }) => finiteNumber(value) && value <= viewport.maxDte);
  const x = xIndices.map(({ value }) => value);
  const y = yIndices.map(({ value }) => value);
  const z = yIndices.map(({ index: yIndex }) => xIndices.map(({ index: xIndex }) => {
    const value = source[yIndex]?.[xIndex];
    return finiteNumber(value) ? value : null;
  }));
  const observed = downsample(surface.observed
    .filter((row) => row.option_type === optionType && finiteNumber(row.log_moneyness) && finiteNumber(row.dte) && finiteNumber(row.provider_iv))
    .map((row) => ({
      logMoneyness: numberValue(row.log_moneyness),
      dte: numberValue(row.dte),
      providerIV: numberValue(row.provider_iv),
      strike: finiteNumber(row.strike) ? row.strike : null,
    }))
    .filter((row) => Math.abs(row.logMoneyness) <= viewport.maxAbsMoneyness && row.dte <= viewport.maxDte), 1_500);
  const heatmap = z.flatMap((row, yIndex) => row.flatMap((value, xIndex) => value === null ? [] : [[xIndex, yIndex, value] as [number, number, number]]));
  const surfacePoints = z.flatMap((row, yIndex) => row.map((value, xIndex) => [x[xIndex] ?? 0, y[yIndex] ?? 0, value] as [number, number, number | null]));
  const values = heatmap.map((point) => point[2]).sort((left, right) => left - right);
  const ivDomain = robustDomain(values);
  const selectedDte = nearest(y, requestedDte ?? 30);
  const selectedIndex = selectedDte === null ? -1 : y.indexOf(selectedDte);
  const selectedSlice = selectedIndex < 0 ? [] : z[selectedIndex]!.flatMap((value, index) => value === null ? [] : [[x[index]!, value] as [number, number]]);
  return { x, y, z, observed, heatmap, surfacePoints, dataShape: [y.length, x.length], ivDomain, selectedDte, selectedSlice };
}

export function curveChoices(curves: OptionHistoryCurves): CurveChoice[] {
  return curves.smiles
    .map((smile) => curveIdentity(smile))
    .filter((choice): choice is CurveChoice => choice !== null)
    .sort((left, right) => left.label.localeCompare(right.label));
}

export function buildOptionCurvePlotData(curves: OptionHistoryCurves, requestedKey: string): {
  selectedKey: string;
  smile: CurveTrace | null;
  term: CurveTrace[];
  history: CurveTrace | null;
} {
  const choices = curveChoices(curves);
  const selectedKey = choices.some((choice) => choice.key === requestedKey) ? requestedKey : (choices[0]?.key ?? "");
  const selectedSmile = curves.smiles.find((smile) => curveKey(smile) === selectedKey);
  const smilePoints = points(selectedSmile).sort((left, right) => numberValue(left.moneyness) - numberValue(right.moneyness));
  const smile = selectedSmile && selectedKey
    ? {
        name: curveLabel(selectedSmile),
        x: smilePoints.map((point) => numberValue(point.moneyness)),
        y: smilePoints.map((point) => numberValue(point.iv)),
        text: smilePoints.map((point) => `Strike ${String(point.strike ?? "—")}`),
      }
    : null;
  const term = ["call", "put"].flatMap((optionType) => {
    const rows = curves.term_structure
      .filter((row) => String(row.option_type) === optionType && finiteNumber(row.dte) && finiteNumber(row.atm_iv))
      .sort((left, right) => numberValue(left.dte) - numberValue(right.dte));
    return rows.length
      ? [{
          name: `${optionType === "call" ? "Call" : "Put"} ATM IV`,
          x: rows.map((row) => numberValue(row.dte)),
          y: rows.map((row) => numberValue(row.atm_iv)),
          text: rows.map((row) => `${String(row.expiration)} ${optionType}`),
        }]
      : [];
  });
  const historyRows = curves.history
    .filter((row) => curveKey(row) === selectedKey && finiteNumber(row.atm_iv) && typeof row.slot_at === "string")
    .sort((left, right) => String(left.slot_at).localeCompare(String(right.slot_at)));
  const history = selectedKey && historyRows.length
    ? {
        name: curveLabel(selectedSmile),
        x: historyRows.map((row) => String(row.slot_at)),
        y: historyRows.map((row) => numberValue(row.atm_iv)),
        text: historyRows.map((row) => `${String(row.expiration)} ${String(row.option_type)}`),
      }
    : null;
  return { selectedKey, smile, term, history };
}

function curveIdentity(row: CurveRecord): CurveChoice | null {
  const key = curveKey(row);
  return key ? { key, label: curveLabel(row) } : null;
}

function curveKey(row: CurveRecord | undefined): string | null {
  const expiration = row?.expiration;
  const optionType = row?.option_type;
  return typeof expiration === "string" && (optionType === "call" || optionType === "put") ? `${expiration}:${optionType}` : null;
}

function curveLabel(row: CurveRecord | undefined): string {
  const key = curveKey(row);
  return key ? key.replace(":", " ") : "Selected curve";
}

function points(row: CurveRecord | undefined): CurveRecord[] {
  return Array.isArray(row?.points) ? row.points.filter((point): point is CurveRecord => Boolean(point && typeof point === "object")) : [];
}

function finiteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function numberValue(value: unknown): number {
  return finiteNumber(value) ? value : Number.NaN;
}

function downsample<T>(rows: T[], limit: number): T[] {
  if (rows.length <= limit) return rows;
  const stride = Math.ceil(rows.length / limit);
  return rows.filter((_, index) => index % stride === 0);
}

function robustDomain(values: number[]): [number, number] {
  if (values.length === 0) return [0, 1];
  const low = values[Math.floor((values.length - 1) * 0.02)] ?? values[0]!;
  const high = values[Math.ceil((values.length - 1) * 0.98)] ?? values.at(-1)!;
  return low === high ? [low * 0.9, high * 1.1 || 1] : [low, high];
}

function nearest(values: number[], target: number): number | null {
  if (values.length === 0) return null;
  return values.reduce((best, value) => Math.abs(value - target) < Math.abs(best - target) ? value : best);
}
