import { useEffect, useState } from "react";
import { loadPaperAccountHistory, type PaperAccountHistory } from "@/api/workstation";
import { money, dateTime } from "@/presentation/labels";

type NavPoints = NonNullable<PaperAccountHistory["points"]>;
export function navSegments(points: NavPoints): NavPoints[] {
  const segments: NavPoints[] = [];
  for (const point of points) {
    if (point.status !== "complete" || point.nav == null || !Number.isFinite(point.nav) || !Number.isFinite(Date.parse(point.at))) { segments.push([]); continue; }
    const last = segments.at(-1)?.at(-1);
    // Missing hourly captures are gaps too, not continuous observations.
    const hour = Math.floor(Date.parse(point.at) / 3600000);
    const lastHour = last ? Math.floor(Date.parse(last.at) / 3600000) : null;
    if (!segments.length || last && (hour - lastHour! > 1 || Date.parse(point.at) <= Date.parse(last.at))) segments.push([]);
    segments[segments.length - 1].push(point);
  }
  return segments.filter(segment => segment.length);
}
export function PaperAccountCurve() {
  const [history, setHistory] = useState<PaperAccountHistory | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void loadPaperAccountHistory(90, controller.signal).then(result => { if (!controller.signal.aborted) { setHistory(result); setError(null); } }).catch(reason => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Account history unavailable"); });
    const timer = setInterval(() => { if (document.visibilityState === "visible") setReload(value => value + 1); }, 60000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [reload]);
  const segments = navSegments(history?.points ?? []), values = segments.flat();
  const times = values.map(p => Date.parse(p.at)), amounts = values.map(p => p.nav as number);
  const minT = Math.min(...times), maxT = Math.max(...times), minY = Math.min(...amounts), maxY = Math.max(...amounts);
  const pad = Math.max((maxY - minY) * .1, Math.abs(maxY) * .001, 1);
  const x = (at: string) => 80 + (Date.parse(at) - minT) / Math.max(1, maxT - minT) * 800;
  const y = (value: number) => 215 - (value - minY + pad) / (maxY - minY + 2 * pad) * 190;
  return <section className="rounded-xl border border-border bg-card p-5" aria-label="Whole paper account NAV history"><div className="flex flex-wrap items-center justify-between gap-3"><h2 className="font-semibold">Whole-account equity curve</h2><button className="rounded border px-3 py-1 text-xs" onClick={() => setReload(value => value + 1)}>Reload history</button></div><p className="mt-2 text-xs text-muted-foreground">{history?.basis ?? "Prospectively recorded NAV, including verified open-position marks. Trade filters do not apply."}</p>
    {error ? <p role="alert" className="mt-3 text-sm text-destructive">{history ? "Retained history may be stale. " : ""}{error}</p> : null}
    {values.length ? <><svg viewBox="0 0 920 255" className="mt-4 w-full" role="img" aria-label="Recorded whole-book net asset value; discontinuities indicate missing evidence"><text x="4" y="30" fontSize="12" fill="currentColor">{money(maxY + pad)}</text><text x="4" y="217" fontSize="12" fill="currentColor">{money(minY - pad)}</text>{segments.map((segment, i) => <g key={i}><polyline points={segment.map(p => `${x(p.at)},${y(p.nav as number)}`).join(" ")} fill="none" stroke="currentColor" strokeWidth="2" className="text-primary" />{segment.length === 1 ? <circle cx={x(segment[0].at)} cy={y(segment[0].nav as number)} r="3" fill="currentColor" /> : null}</g>)}<text x="80" y="248" fontSize="11" fill="currentColor">{dateTime(values[0].at)}</text><text x="880" y="248" textAnchor="end" fontSize="11" fill="currentColor">{dateTime(values[values.length - 1].at)}</text></svg><p className="text-xs text-muted-foreground">{values.length} verified hourly samples. A cash-only curve is not evidence of strategy success. Gaps are not interpolated.{history?.truncated ? " Older samples exceed this response limit." : ""}</p></> : <p className="mt-4 rounded-lg border border-dashed p-5 text-sm">{history ? "No verified NAV observations yet. The paper manager records the first funded-book observation, then at most one every five minutes. History is not backfilled from today's portfolio." : "Loading recorded account history…"}</p>}
  </section>;
}
