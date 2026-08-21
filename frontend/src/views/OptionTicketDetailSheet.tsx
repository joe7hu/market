import { useEffect, useState, type ReactNode } from "react";

import { loadOptionTicketDetail } from "@/api/options";
import { StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import type { JsonValue, RowRecord } from "@/types";
import { formatMoney, listField, numberField, textField, titleLabel, toneFromText } from "./rowFormat";
import type { OpenTicker } from "./workspacePage";

type OptionTicketDetailSheetProps = {
  decisionId: string | null;
  onClose: () => void;
  onOpenTicker: OpenTicker;
};

/**
 * A decision-ID owner for immutable option detail.  It does not depend on the
 * ticker dossier, so a published signal never becomes a blank page when its
 * broader research cache is incomplete.
 */
export function OptionTicketDetailSheet({ decisionId, onClose, onOpenTicker }: OptionTicketDetailSheetProps) {
  const [detail, setDetail] = useState<RowRecord | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    if (!decisionId) {
      setDetail(null);
      setError(null);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    loadOptionTicketDetail(decisionId, controller.signal)
      .then((payload) => setDetail(payload as unknown as RowRecord))
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setDetail(null);
        setError(cause instanceof Error ? cause.message : "The ticket detail API failed.");
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [decisionId, retry]);

  const ticket = recordOf(detail?.ticket) || detail || {};
  const signal = recordOf(detail?.signal);
  const publication = recordOf(detail?.publication);
  const symbol = textField(ticket, ["symbol"], textField(signal, ["ticker", "symbol"]));
  const state = textField(ticket, ["state"], "RESEARCH").toUpperCase();
  const ready = state === "READY";
  const entry = recordOf(ticket.entry);
  const risk = recordOf(ticket.risk);
  const thesis = recordOf(ticket.thesis);
  const exits = recordOf(ticket.exits);
  const outcome = recordOf(detail?.outcome);
  const legs = records(ticket.legs);
  const evidence = records(detail?.evidence);
  const agentProvenance = recordOf(detail?.agent_provenance);
  const blockers = listField(ticket, ["blockers"]);

  return (
    <Sheet open={Boolean(decisionId)} onOpenChange={(open) => (open ? undefined : onClose())}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-y-auto p-0 sm:max-w-xl">
        <SheetHeader className="sticky top-0 z-10 border-b border-border bg-background px-5 py-4 pr-12">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge tone={toneFromText(state)}>{titleLabel(state)}</StatusBadge>
            <StatusBadge tone="info">{textField(ticket, ["lane"], "radar")}</StatusBadge>
            <StatusBadge tone={ready ? "good" : "warn"}>{ready ? "Paper entry eligible" : "Research / resolve blocker"}</StatusBadge>
          </div>
          <SheetTitle>{symbol || "Option decision"}</SheetTitle>
          <SheetDescription>
            Immutable decision {decisionId ?? ""}. {ready ? "Use paper-entry controls only after this ticket is revalidated." : "This is not a trade instruction."}
          </SheetDescription>
        </SheetHeader>

        <div className="space-y-5 px-5 py-5">
          {loading ? <p className="text-sm text-muted-foreground">Loading ticket detail…</p> : null}
          {error ? (
            <section className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
              <p className="font-medium">Ticket detail could not load.</p>
              <p className="mt-1 break-words text-muted-foreground">GET /api/options/tickets/{decisionId}: {error}</p>
              <Button type="button" size="sm" variant="outline" className="mt-3" onClick={() => setRetry((value) => value + 1)}>Retry</Button>
            </section>
          ) : null}
          {!loading && !error ? (
            <>
              <DetailSection title={ready ? "Paper entry" : "Research action"}>
                <MetricGrid values={[
                  ["Structure", textField(ticket, ["structure"], "—").replaceAll("_", " ")],
                  ["Limit", formatMoney(numberField(entry, ["limit_price"], Number.NaN))],
                  ["Maximum risk", formatMoney(numberField(risk, ["one_unit_max_loss", "one_unit_collateral"], Number.NaN))],
                  ["Quote expires", textField(ticket, ["expires_at"], textField(entry, ["valid_until"], "—"))],
                  ["Lower-confidence EV / risk", decimal(numberField(ticket, ["lower_confidence_expectancy_per_max_risk"], Number.NaN))],
                  ["Next action", textField(ticket, ["required_next_action"], ready ? "Revalidate before paper entry" : "Resolve blockers")],
                ]} />
              </DetailSection>

              <DetailSection title="Ticket legs">
                {legs.length ? <div className="space-y-2">{legs.map((leg, index) => (
                  <div key={`${textField(leg, ["contract_id"], String(index))}-${index}`} className="rounded-md border border-border p-3 text-sm">
                    <div className="flex flex-wrap items-center justify-between gap-2 font-medium"><span>{textField(leg, ["side"], "—")} {textField(leg, ["option_type"], "option")} {formatMoney(numberField(leg, ["strike"], Number.NaN))}</span><span className="text-muted-foreground">{textField(leg, ["contract_id", "occ_symbol"], "—")}</span></div>
                    <div className="mt-1 text-xs text-muted-foreground">Bid {formatMoney(numberField(leg, ["bid"], Number.NaN))} · Ask {formatMoney(numberField(leg, ["ask"], Number.NaN))} · Quote {textField(leg, ["quote_time"], "—")}</div>
                  </div>
                ))}</div> : <p className="text-sm text-muted-foreground">No complete execution legs are available. The ticket remains fail-closed.</p>}
              </DetailSection>

              <DetailSection title="Primary blocker">
                {blockers.length ? <ul className="space-y-1 text-sm text-muted-foreground">{blockers.map((blocker) => <li key={blocker}>• {blocker}</li>)}</ul> : <p className="text-sm text-muted-foreground">No blocker is recorded.</p>}
              </DetailSection>

              <DetailSection title="Thesis and exits">
                <p className="text-sm">{textField(thesis, ["summary"], "No thesis summary is stored.")}</p>
                <p className="mt-2 text-sm text-muted-foreground">Invalidation: {textField(thesis, ["invalidation"], textField(exits, ["thesis_invalidation"], "Not recorded"))}</p>
              </DetailSection>

              <DetailSection title="Publication and outcome">
                <MetricGrid values={[
                  ["Publication", textField(publication, ["id"], textField(ticket, ["publication_id"], "—"))],
                  ["Published", textField(publication, ["published_at"], "—")],
                  ["Current", textField(publication, ["current"], "false")],
                  ["Outcome", textField(outcome, ["maturity_state", "paper_status"], "Not resolved")],
                  ["Return", decimal(numberField(outcome, ["current_return", "return_20d"], Number.NaN))],
                  ["Max drawdown", decimal(numberField(outcome, ["max_drawdown"], Number.NaN))],
                ]} />
              </DetailSection>

              <DetailSection title="Evidence">
                {evidence.length ? <ul className="space-y-2 text-sm text-muted-foreground">{evidence.slice(0, 12).map((item, index) => <li key={`${textField(item, ["evidence_kind", "reference_key"], String(index))}-${index}`}>• {textField(item, ["evidence_kind", "reference_key"], "Evidence")}</li>)}</ul> : <p className="text-sm text-muted-foreground">No evidence reference is stored.</p>}
              </DetailSection>

              <DetailSection title="Agent provenance">
                {Object.keys(agentProvenance).length ? <MetricGrid values={[
                  ["Task", textField(agentProvenance, ["option_agent_task_id", "task_id"], "Advisory")],
                  ["Validation", textField(agentProvenance, ["validation_status", "status"], "Not recorded")],
                ]} /> : <p className="text-sm text-muted-foreground">No agent advisory is attached to this ticket.</p>}
              </DetailSection>

              {symbol ? <Button type="button" variant="outline" onClick={() => onOpenTicker(symbol)}>Open ticker context</Button> : null}
            </>
          ) : null}
        </div>
      </SheetContent>
    </Sheet>
  );
}

function DetailSection({ title, children }: { title: string; children: ReactNode }) {
  return <section><h3 className="text-sm font-semibold">{title}</h3><div className="mt-2">{children}</div></section>;
}

function MetricGrid({ values }: { values: Array<[string, string]> }) {
  return <div className="grid gap-2 sm:grid-cols-2">{values.map(([label, value]) => <div key={label} className="rounded-md border border-border p-3"><div className="text-xs text-muted-foreground">{label}</div><div className="mt-1 break-words text-sm font-medium">{value || "—"}</div></div>)}</div>;
}

function recordOf(value: JsonValue | undefined): RowRecord {
  return value && typeof value === "object" && !Array.isArray(value) ? value as RowRecord : {};
}

function records(value: JsonValue | undefined): RowRecord[] {
  return Array.isArray(value) ? value.filter((item): item is Record<string, JsonValue> => Boolean(item) && typeof item === "object" && !Array.isArray(item)) : [];
}

function decimal(value: number): string {
  return Number.isFinite(value) ? value.toFixed(3) : "—";
}
