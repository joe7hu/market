import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { loadPaperObservations, type PaperObservationPage } from "@/api/paper";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { evidenceReason } from "@/presentation/evidence";
import { dateTime, humanize, money, percent } from "@/presentation/labels";


export function PaperObservations() {
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);
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
  }, [offset, status]);
  return <Card>
    <CardHeader><CardTitle>Paper experiments</CardTitle>
      <p className="text-sm text-muted-foreground">All strategy observations, separate from the portfolio filters above. Each experiment tracks one contract using later quotes and estimated costs. These results do not count as funded paper-account P&amp;L.</p>
      <label className="flex items-center gap-2 text-sm">Experiment status
        <select className="rounded-md border border-border bg-background p-2" value={status} onChange={(event) => { setStatus(event.target.value); setOffset(0); }}>
          <option value="">All</option>{["pending", "entered", "closed", "unfilled", "rejected", "unmeasurable"].map((value) => <option key={value} value={value}>{humanize(value)}</option>)}
        </select>
      </label>
      {page ? <p className="text-sm">{page.counts.pending ?? 0} awaiting entry · {page.counts.entered ?? 0} open · {page.counts.closed ?? 0} closed · {page.counts.unfilled ?? 0} unfilled</p> : null}
    </CardHeader>
    <CardContent>
      {error ? <p role="alert" className="text-destructive">{error}</p> : !page ? <p role="status">Loading experiments…</p> : <>
        {page.rows.length === 0 ? <p className="text-sm text-muted-foreground">No experiments match this status.</p> : <div className="divide-y divide-border">{page.rows.map((row) => <details key={row.id} className="py-3">
          <summary className="cursor-pointer text-sm"><strong>{row.symbol}</strong> · {humanize(row.structure)} · {humanize(row.status)} · {row.status === "closed" ? `Net return ${percent(row.net_return)}` : dateTime(row.created_at)}</summary>
          <div className="mt-3 space-y-3 text-sm">
            <p>{row.strategy} · Decision {dateTime(row.decision_at)} · <Link className="text-primary underline" to={`/ticker/${encodeURIComponent(row.symbol)}`}>Current company evidence</Link></p>
            <p>{humanize(row.exit_reason || row.reason, "Waiting for the next eligible quote")}{row.status === "pending" ? ` · Entry deadline ${dateTime(row.entry_deadline)}` : ""}</p>
            {row.thesis_summary ? <p>{row.thesis_summary}</p> : null}
            {row.blockers?.length ? <ul className="list-disc space-y-1 pl-5">{row.blockers.map((blocker) => <li key={blocker}>{evidenceReason(blocker)}</li>)}</ul> : null}
            {row.status !== "closed" && row.required_next_action ? <p><strong>Next action:</strong> {row.required_next_action}</p> : null}
            <dl className="grid gap-3 sm:grid-cols-2"><div><dt className="text-muted-foreground">Entry quote per share</dt><dd>{money(row.entry_price)} · {dateTime(row.entry_at)}</dd></div><div><dt className="text-muted-foreground">Exit quote per share</dt><dd>{money(row.exit_price)} · {dateTime(row.exit_at)}</dd></div></dl>
            <details><summary className="cursor-pointer">Frozen decision and quote evidence</summary><pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-all rounded bg-muted p-3 text-xs">{JSON.stringify({ decision_id: row.decision_id, ticket: row.ticket, entry_quotes: row.entry_quotes, exit_quotes: row.exit_quotes }, null, 2)}</pre></details>
          </div>
        </details>)}</div>}
        <div className="mt-4 flex items-center gap-3 text-sm"><button className="rounded border px-3 py-2 disabled:opacity-50" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>Previous</button><span>{page.total ? offset + 1 : 0}–{offset + page.rows.length} of {page.total}</span><button className="rounded border px-3 py-2 disabled:opacity-50" disabled={page.next_offset === null} onClick={() => setOffset(page.next_offset ?? offset)}>Next</button></div>
      </>}
    </CardContent>
  </Card>;
}
