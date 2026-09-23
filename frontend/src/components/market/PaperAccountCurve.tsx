import { useEffect, useMemo, useState } from "react";
import { loadPaperAccountHistory, type PaperAccountHistory } from "@/api/workstation";
import { money, dateTime } from "@/presentation/labels";
import { PaperPerformanceChart } from "./PaperPerformanceChart";
import { finite, type FinancialPoint } from "./financialChartModel";

type NavPoints = NonNullable<PaperAccountHistory["points"]>;
const noTradeSelection = () => undefined;

export function navSegments(points: NavPoints): NavPoints[] {
  const segments: NavPoints[] = [];
  for (const point of points) {
    if (point.status !== "complete" || point.nav == null || !Number.isFinite(point.nav) || !Number.isFinite(Date.parse(point.at))) { segments.push([]); continue; }
    const last = segments.at(-1)?.at(-1);
    const hour = Math.floor(Date.parse(point.at) / 3600000);
    const lastHour = last ? Math.floor(Date.parse(last.at) / 3600000) : null;
    if (!segments.length || last && (hour - lastHour! > 1 || Date.parse(point.at) <= Date.parse(last.at))) segments.push([]);
    segments[segments.length - 1].push(point);
  }
  return segments.filter(segment => segment.length);
}

export function navChartPoints(points: NavPoints): FinancialPoint[] {
  const membership = new Map(navSegments(points).flatMap((segment, index) => segment.map(point => [point, index] as const)));
  return points.map(point => ({ at: point.at, value: point.status === "complete" && point.nav != null && Number.isFinite(point.nav) ? point.nav : null, trade_id: "", segment: membership.get(point) }));
}

export function PaperAccountCurve() {
  const [history, setHistory] = useState<PaperAccountHistory | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    void loadPaperAccountHistory(90, controller.signal)
      .then(result => { if (!controller.signal.aborted) { setHistory(result); setError(null); } })
      .catch(reason => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Account history unavailable"); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    const timer = setInterval(() => { if (document.visibilityState === "visible") setReload(value => value + 1); }, 60000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [reload]);
  const points = useMemo(() => navChartPoints(history?.points ?? []), [history]);
  const verified = points.filter(point => point.value !== null && Number.isFinite(Date.parse(point.at))).sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
  const latest = verified.at(-1);
  const lastRecord = history?.points?.at(-1);
  const latestIncomplete = Boolean(lastRecord && (lastRecord.status !== "complete" || !finite(lastRecord.nav) || !Number.isFinite(Date.parse(lastRecord.at))));
  return <section className="rounded-xl border border-border bg-card p-4 sm:p-6" aria-label="Whole paper account NAV history">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-sm font-medium text-muted-foreground">Whole-account equity curve</h2><p className="mt-2 text-3xl font-semibold tracking-tight tabular-nums">{latest ? money(latest.value) : "—"}<span className="ml-2 text-xs font-normal text-muted-foreground">verified NAV · USD</span></p><p className="mt-1 text-xs text-muted-foreground">{latest ? `As of ${dateTime(latest.at)}` : loading ? "Loading recorded account history…" : "No verified NAV observations yet."}</p></div><button type="button" className="rounded border px-3 py-1.5 text-xs disabled:opacity-50" disabled={loading} onClick={() => setReload(value => value + 1)}>{loading ? "Refreshing…" : "Reload history"}</button></div>
    {error ? <p role="alert" className="mt-3 rounded border border-destructive/40 p-3 text-sm text-destructive">{history ? "Retained history may be stale. " : ""}{error}</p> : null}
    {latestIncomplete ? <p role="status" className="mt-3 rounded border border-amber-500/40 p-3 text-sm">Latest account valuation is incomplete. The amount above is the last verified NAV, not a current balance.</p> : null}
    <div className="mt-5"><PaperPerformanceChart points={points} label="Net asset value" metric="nav" onSelect={noTradeSelection} /></div>
    <div className="mt-3 flex flex-wrap justify-between gap-2 text-xs text-muted-foreground"><span>{verified.length} verified observations · last 90 days</span><span>Whole account · trade filters do not apply</span></div>
    <details className="mt-3 text-xs text-muted-foreground"><summary className="cursor-pointer">Valuation basis and coverage</summary><p className="mt-2">{history?.basis ?? "Prospectively recorded NAV, including verified open-position marks."} NAV includes funding and is not the same as P&L. Gaps are not interpolated; history is not reconstructed from today's portfolio.{history?.truncated ? " Older samples exceed this response limit." : ""}</p><p className="mt-2">A cash-only curve is not evidence of strategy success.</p></details>
  </section>;
}
