import {StatusBadge} from "@/components/market/workstation";
import {RowRecord} from "@/types";
import {formatNumber, formatShortDate} from "../optionsRadarFormat";
import {textField, numberField} from "../rowFormat";
import {TickerButton} from "../optionsRadarPrimitives";
import {OpenTicker} from "../workspacePage";

export function DiscoveryQueue({rows, onOpenTicker}: {rows: RowRecord[]; onOpenTicker: OpenTicker}) {
  const queued = [...rows]
    .filter((row) => textField(row, ["stage"]) !== "PUBLISHED")
    .sort((a, b) => numberField(b, ["discovery_score"], 0) - numberField(a, ["discovery_score"], 0));
  const candidates = queued.slice(0, 8);
  return (
    <section className="rounded-md border border-border bg-card p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="text-base font-semibold">Discovery Queue</h2>
          <p className="mt-1 text-sm text-muted-foreground">Broader candidates awaiting evidence or execution-quality data. No contract recommendation is made at this stage.</p>
        </div>
        <StatusBadge tone={queued.length ? "info" : "muted"}>{queued.length ? `${queued.length} in discovery pipeline` : "Queue clear"}</StatusBadge>
      </div>
      {candidates.length ? (
        <div className="mt-3 grid gap-2 lg:grid-cols-2">
          {candidates.map((row) => {
            const ticker = textField(row, ["ticker"]);
            const start = textField(row, ["catalyst_start"]);
            const end = textField(row, ["catalyst_end"]);
            return (
              <article key={ticker} className="rounded-md border border-border/70 bg-background p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} />
                  <StatusBadge tone="info">{textField(row, ["stage"], "DISCOVERED")}</StatusBadge>
                  <StatusBadge tone="muted">Data {textField(row, ["data_readiness"], "D")}</StatusBadge>
                  <StatusBadge tone="muted">Evidence {formatNumber(numberField(row, ["evidence_completeness"], 0), 0)}/5</StatusBadge>
                </div>
                <p className="mt-2 text-sm leading-5">{textField(row, ["surface_reason"], "Current option-chain candidate")}</p>
                <p className="mt-2 text-xs text-muted-foreground"><span className="font-semibold text-foreground">Primary edge:</span> {textField(row, ["primary_edge"], "unknown")} · <span className="font-semibold text-foreground">Timing:</span> {textField(row, ["timeliness"], "unknown")}</p>
                {start ? <p className="mt-1 text-xs text-muted-foreground">Catalyst window {formatShortDate(start)}{end ? ` – ${formatShortDate(end)}` : ""}</p> : null}
                <p className="mt-2 text-xs leading-5 text-muted-foreground"><span className="font-semibold text-foreground">Next evidence:</span> {textField(row, ["next_evidence"], "Complete underwriting.")}</p>
              </article>
            );
          })}
        </div>
      ) : <p className="mt-3 text-sm text-muted-foreground">No candidates are waiting outside the published shortlist.</p>}
    </section>
  );
}
