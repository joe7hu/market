import { useEffect, useState } from "react";
import { loadPaperObservationHistory, type PaperObservationHistory } from "@/api/paper";
import { dateTime, money, humanize } from "@/presentation/labels";

type Event = NonNullable<PaperObservationHistory["events"]>[number];
export function experimentSegments(events: Event[]): Event[][] {
  const segments: Event[][] = [];
  let current: Event[] = [];
  for (const event of events) {
    if (event.kind === "mark_gap" || event.net_pnl == null || !Number.isFinite(event.net_pnl) || !Number.isFinite(Date.parse(event.at))) {
      if (current.length) segments.push(current);
      current = [];
    } else current.push(event);
  }
  if (current.length) segments.push(current);
  return segments;
}

export function ExperimentCurve({ observationId }: { observationId: string }) {
  const [history, setHistory] = useState<PaperObservationHistory | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    let inFlight = false;
    setHistory(null); setError(null);
    const refresh = async () => {
      if (inFlight || controller.signal.aborted) return;
      inFlight = true;
      try {
        const value = await loadPaperObservationHistory(observationId, controller.signal);
        if (!controller.signal.aborted) { setHistory(value); setError(null); }
      } catch (reason) {
        if (!controller.signal.aborted) setError(String(reason));
      } finally { inFlight = false; }
    };
    void refresh();
    const timer = setInterval(() => { void refresh(); }, 30000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [observationId]);
  return <section aria-label="Research experiment P&L" className="rounded-lg border border-border p-4">
    <h3 className="font-semibold">Research experiment P&amp;L and events</h3>
    {error ? <p role="alert" className="mt-2 text-sm text-destructive">History refresh failed. Retained points are historical, not a current mark. {error}</p> : null}
    {!history ? !error ? <p role="status">Loading recorded events…</p> : null : <ExperimentHistory history={history} />}
  </section>;
}

export function ExperimentHistory({ history }: { history: PaperObservationHistory }) {
  const events = history.events ?? [];
  const priced = events.filter(event => event.kind !== "mark_gap" && event.net_pnl != null && Number.isFinite(event.net_pnl) && Number.isFinite(Date.parse(event.at)));
  const times = events.map(event => Date.parse(event.at)).filter(Number.isFinite);
  const first = Math.min(...times), last = Math.max(...times);
  const min = Math.min(0, ...priced.map(event => event.net_pnl!));
  const max = Math.max(0, ...priced.map(event => event.net_pnl!));
  const x = (event: Event) => 64 + (last === first ? .5 : (Date.parse(event.at) - first) / (last - first)) * 700;
  const y = (event: Event) => 170 - ((event.net_pnl! - min) / (max - min || 1)) * 130;
  const shown = [...events].reverse().slice(0, 100);
  return <>
    <p className="mt-1 text-xs text-muted-foreground">{history.basis}</p>
    {history.status === "unmeasurable" ? <p role="alert" className="mt-2 text-sm">This observation became unmeasurable. Earlier marks are retained for audit, not a verified completed return or learning outcome.</p> : null}
    <div className="mt-3 flex flex-wrap gap-5 text-sm"><span><strong>Entry:</strong> {history.entry_at ? `${dateTime(history.entry_at)} · ${money(history.entry_price)} per share · 1 contract × 100` : "No fill recorded"}</span>{history.exit_at ? <span><strong>Exit:</strong> {dateTime(history.exit_at)} · {money(history.exit_price)} per share</span> : null}</div>
    {priced.length ? <svg className="my-3 w-full" viewBox="0 0 820 210" role="img" aria-label={`${history.symbol} net experiment P&L in dollars; entry and exit markers`}>
      <text x="8" y="25" fontSize="11" fill="currentColor">{money(max)}</text><text x="8" y="174" fontSize="11" fill="currentColor">{money(min)}</text>
      <line x1="64" x2="764" y1={170 - ((0 - min) / (max - min || 1)) * 130} y2={170 - ((0 - min) / (max - min || 1)) * 130} stroke="currentColor" opacity=".2" />
      {experimentSegments(events).map((segment, index) => <polyline key={index} points={segment.map(event => `${x(event)},${y(event)}`).join(" ")} fill="none" stroke="currentColor" strokeWidth="2" />)}
      {priced.map(event => <circle key={event.event_key} cx={x(event)} cy={y(event)} r={event.kind === "mark" ? 2 : 6} fill="currentColor"><title>{`${humanize(event.kind)} · ${dateTime(event.at)} · Price ${money(event.price)} · Net P&L ${money(event.net_pnl)}`}</title></circle>)}
      {priced.filter(event => event.kind !== "mark").map(event => <text key={`label-${event.event_key}`} x={x(event)} y={Math.max(20, y(event) - 12)} textAnchor="middle" fontSize="12" fill="currentColor">{humanize(event.kind)}</text>)}
      <text x="64" y="201" fontSize="10" fill="currentColor">{dateTime(events[0]?.at)}</text><text x="764" y="201" textAnchor="end" fontSize="10" fill="currentColor">{dateTime(events.at(-1)?.at)}</text>
    </svg> : <p className="my-3 text-sm">{history.coverage === "snapshot_only" ? "This position predates the event journal. Its recorded entry is shown above; the historical price path is not reconstructed. New observed marks will appear here." : history.coverage === "not_entered" ? "No position was entered. Rejections and unfilled orders have no trading P&L." : "The worker has recorded a quote gap; there is no verified P&L point to plot."}</p>}
    {history.history_started_at ? <p className="text-xs text-muted-foreground">Recorded history starts {dateTime(history.history_started_at)}.{history.truncated ? " Older events are outside this 2,000-event response window." : ""}</p> : null}
    {shown.length ? <details className="mt-3" open><summary className="cursor-pointer text-sm font-medium">Event ledger · latest {shown.length} of {events.length} loaded events</summary><div className="overflow-x-auto"><table className="mt-2 w-full text-left text-xs"><thead><tr><th className="p-2">Event / observed time</th><th className="p-2">Source quote time</th><th className="p-2">Price / share</th><th className="p-2">Net P&amp;L</th><th className="p-2">Fees</th></tr></thead><tbody>{shown.map(event => <tr className="border-t border-border" key={event.event_key}><td className="p-2">{humanize(event.kind)} · {dateTime(event.at)}{event.reason ? <span className="block">{humanize(event.reason)}</span> : null}</td><td className="p-2">{event.quote_observed_at ? dateTime(event.quote_observed_at) : "No executable quote"}</td><td className="p-2">{event.price == null ? "Not priced" : money(event.price)}</td><td className="p-2">{event.net_pnl == null ? "Gap — not zero" : money(event.net_pnl)}</td><td className="p-2">{event.fees == null ? "Not assessed" : money(event.fees)}</td></tr>)}</tbody></table></div></details> : null}
  </>;
}
