import type { OptionHistoryCurves } from "@/api";

type CurveRecord = Record<string, unknown>;

export type CurveChoice = { key: string; label: string };
export type CurveTrace = { name: string; x: Array<number | string>; y: number[]; text: string[] };

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
