import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { loadPaperObservations, type PaperObservation, type PaperObservationPage } from "@/api/paper";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { evidenceReason } from "@/presentation/evidence";
import { dateTime, humanize, money, percent } from "@/presentation/labels";
import { paperObservationProgress } from "@/presentation/paperProgress";

export function PaperObservations() {
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);
  const [reload, setReload] = useState(0);
  const [page, setPage] = useState<PaperObservationPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setPage(null);
    setError(null);
    void loadPaperObservations(status, offset, controller.signal)
      .then((value) => { if (!controller.signal.aborted) setPage(value); })
      .catch((reason) => { if (!controller.signal.aborted) setError(String(reason)); });
    return () => controller.abort();
  }, [offset, status, reload]);
  const progress = page ? paperObservationProgress(page.counts) : null;
  return <Card>
    <CardHeader><CardTitle>Paper experiments</CardTitle>
      <p className="text-sm text-muted-foreground">All strategy observations, separate from the portfolio filters above. Each experiment tracks one contract using later quotes and estimated costs. These results do not count as funded paper-account P&amp;L.</p>
      {progress ? <div role="status" className={`rounded-md border p-3 text-sm ${progress.attention ? "border-amber-300 bg-amber-50 text-amber-950 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-100" : "border-border bg-muted/30"}`}>
        <p className="font-semibold">{progress.title}</p><p className="mt-1">{progress.detail}</p>
        {progress.attention ? <Link className="mt-2 inline-block underline" to="/health">Inspect execution and data health</Link> : null}
      </div> : null}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <label className="flex items-center gap-2 text-sm">Experiment status
          <select className="rounded-md border border-border bg-background p-2" value={status} onChange={(event) => { setStatus(event.target.value); setOffset(0); }}>
            <option value="">All</option>{["pending", "entered", "closed", "unfilled", "rejected", "unmeasurable"].map((value) => <option key={value} value={value}>{humanize(value)}</option>)}
          </select>
        </label>
        <button type="button" className="rounded border px-3 py-2 text-sm disabled:opacity-50" disabled={!page && !error} onClick={() => setReload((value) => value + 1)}>Reload observations</button>
      </div>
      {page ? <p className="text-sm">All observations: {page.counts.pending ?? 0} awaiting entry · {page.counts.entered ?? 0} open · {page.counts.closed ?? 0} closed · {page.counts.unfilled ?? 0} unfilled · {page.counts.rejected ?? 0} rejected · {page.counts.unmeasurable ?? 0} unmeasurable</p> : null}
    </CardHeader>
    <CardContent>
      {error ? <div role="alert" className="text-sm text-destructive"><p>{error}</p><button type="button" className="mt-2 rounded border px-3 py-2" onClick={() => setReload((value) => value + 1)}>Retry observations</button></div> : !page ? <p role="status">Loading experiments…</p> : <>
        {page.rows.length === 0 ? <p className="text-sm text-muted-foreground">No experiments match this status.</p> : <div className="divide-y divide-border">{page.rows.map((row) => <PaperObservationRow key={row.id} row={row} />)}</div>}
        <div className="mt-4 flex items-center gap-3 text-sm"><button type="button" className="rounded border px-3 py-2 disabled:opacity-50" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>Previous</button><span>{page.total ? offset + 1 : 0}–{offset + page.rows.length} of {page.total}</span><button type="button" className="rounded border px-3 py-2 disabled:opacity-50" disabled={page.next_offset === null} onClick={() => setOffset(page.next_offset ?? offset)}>Next</button></div>
      </>}
    </CardContent>
  </Card>;
}

export function PaperObservationRow({ row }: { row: PaperObservation }) {
  const reason = row.exit_reason || row.reason;
  const hasEntry = row.entry_at !== null || row.entry_price !== null || ["entered", "closed"].includes(row.status);
  const hasExit = row.exit_at !== null || row.exit_price !== null || row.status === "closed";
  return <details className="py-3">
    <summary className="cursor-pointer text-sm">
      <span><strong>{row.symbol}</strong> · {humanize(row.structure)} · {humanize(row.status)} · {row.status === "closed" ? `Net return ${percent(row.net_return)}` : dateTime(row.created_at)}</span>
      <span className="mt-1 block text-muted-foreground">{reason ? evidenceReason(reason) : "No lifecycle reason recorded"}{row.status === "pending" && row.entry_deadline ? ` · Entry deadline ${dateTime(row.entry_deadline)}` : ""}</span>
      {row.status !== "closed" && row.required_next_action ? <span className="mt-1 block"><strong>Next action:</strong> {row.required_next_action}</span> : null}
    </summary>
    <div className="mt-3 space-y-3 text-sm">
      <p>{row.strategy} · Decision {dateTime(row.decision_at)} · <Link className="text-primary underline" to={`/tickers/${encodeURIComponent(row.symbol)}`}>Current company evidence</Link></p>
      {row.thesis_summary ? <p>{row.thesis_summary}</p> : null}
      {row.blockers?.length ? <ul className="list-disc space-y-1 pl-5">{row.blockers.map((blocker) => <li key={blocker}>{evidenceReason(blocker)}</li>)}</ul> : null}
      {hasEntry || hasExit ? <dl className="grid gap-3 sm:grid-cols-2">
        {hasEntry ? <div><dt className="text-muted-foreground">Entry quote per share</dt><dd>{row.entry_price === null ? "Entry price evidence missing" : money(row.entry_price)} · {row.entry_at === null ? "Entry time evidence missing" : dateTime(row.entry_at)}</dd></div> : null}
        {hasExit ? <div><dt className="text-muted-foreground">Exit quote per share</dt><dd>{row.exit_price === null ? "Exit price evidence missing" : money(row.exit_price)} · {row.exit_at === null ? "Exit time evidence missing" : dateTime(row.exit_at)}</dd></div> : null}
      </dl> : null}
      <details><summary className="cursor-pointer">Frozen decision and quote evidence</summary><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-all rounded bg-muted p-3 text-xs">{JSON.stringify({ decision_id: row.decision_id, ticket: row.ticket, entry_quotes: row.entry_quotes, exit_quotes: row.exit_quotes }, null, 2)}</pre></details>
    </div>
  </details>;
}
