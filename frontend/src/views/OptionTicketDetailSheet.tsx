import { useEffect, useState, type ReactNode } from "react";

import { loadOptionTicketDetail } from "@/api/options";
import { DataFieldStateNotice, decisionReason, missingFieldState } from "@/components/market/dataFieldState";
import { EvidenceFields, TechnicalDetails } from "@/components/market/StoredEvidence";
import { StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { dateTime, evidenceLabel, lifecycleLabel, statusLabel } from "@/presentation/labels";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import type { JsonValue, RowRecord } from "@/types";
import { formatMoney, listField, numberField, textField, titleLabel, toneFromText } from "@/shared/rowFormat";
import type { OpenTicker } from "./workspacePage";

type OptionTicketDetailSheetProps = {
  decisionId: string | null;
  onClose: () => void;
  onOpenTicker: OpenTicker;
};

export function OptionTicketLoadError({ onRetry }: { onRetry: () => void }) {
  return (
    <section role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
      <p className="font-medium">Ticket detail could not load.</p>
      <p className="mt-1 text-muted-foreground">Check the decision link or retry.</p>
      <Button type="button" size="sm" variant="outline" className="mt-3" onClick={onRetry}>Retry</Button>
    </section>
  );
}

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
  const state = textField(ticket, ["state"], "research").toLowerCase();
  const lane = textField(ticket, ["lane"], "radar").toLowerCase();
  const entry = recordOf(ticket.entry);
  const risk = recordOf(ticket.risk);
  const thesis = recordOf(ticket.thesis);
  const exits = recordOf(ticket.exits);
  const outcome = recordOf(detail?.outcome);
  const legs = records(ticket.legs);
  const evidence = records(detail?.evidence);
  const agentProvenance = recordOf(detail?.agent_provenance);
  const blockers = listField(ticket, ["blockers"]);
  const resolution = recordOf(ticket.resolution);
  const primaryBlocker = textField(resolution, ["primary_blocker"]) || blockers[0] || "";
  const requiredFieldStates = [
    textField(ticket, ["required_next_action"]) ? null : missingFieldState({
      field: "required_next_action", source: "option_ticket", reason: "required_next_action_missing",
      nextAction: "Refresh the immutable option ticket before acting.",
    }),
    textField(resolution, ["primary_blocker"]) || blockers.length ? null : missingFieldState({
      field: "primary_blocker", source: "option_ticket_resolution", reason: "primary_blocker_missing",
      nextAction: "Refresh the ticket resolution and confirm its policy state.",
    }),
  ].filter((state): state is NonNullable<typeof state> => state !== null);

  return (
    <Sheet open={Boolean(decisionId)} onOpenChange={(open) => (open ? undefined : onClose())}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-y-auto p-0 sm:max-w-xl">
        <SheetHeader className="sticky top-0 z-10 border-b border-border bg-background px-5 py-4 pr-12">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge tone={toneFromText(state)}>{statusLabel(state, "Research")}</StatusBadge>
            <StatusBadge tone="info">{statusLabel(lane, "Radar")}</StatusBadge>
            <StatusBadge tone="info">Paper ticket</StatusBadge>
          </div>
          <SheetTitle>{symbol || "Option decision"}</SheetTitle>
          <SheetDescription>Review the recorded terms, evidence, and outcome before considering a paper action.</SheetDescription>
        </SheetHeader>

        <div className="space-y-5 px-5 py-5">
          {loading ? <p className="text-sm text-muted-foreground">Loading ticket detail…</p> : null}
          {error ? <OptionTicketLoadError onRetry={() => setRetry((value) => value + 1)} /> : null}
          {!loading && !error ? (
            <>
              <DetailSection title="Recorded ticket terms">
                <MetricGrid values={[
                  ["Structure", textField(ticket, ["structure"], "—").replaceAll("_", " ")],
                  ["Limit", formatMoney(numberField(entry, ["limit_price"], Number.NaN))],
                  ["Maximum risk", formatMoney(numberField(risk, ["one_unit_max_loss", "one_unit_collateral"], Number.NaN))],
                  ["Quote expires", dateTime(textField(ticket, ["expires_at"], textField(entry, ["valid_until"])))],
                  ["Lower-confidence EV / risk", decimal(numberField(ticket, ["lower_confidence_expectancy_per_max_risk"], Number.NaN))],
                  ["Next action", decisionReason(textField(ticket, ["required_next_action"]))],
                  ["Resolution", `${statusLabel(textField(resolution, ["eligibility"]).toLowerCase(), "Unknown")} · ${lifecycleLabel(textField(resolution, ["lifecycle"]).toLowerCase())}`],
                  ["Authorization", statusLabel(textField(resolution, ["authorization_mode"]).toLowerCase(), "Not recorded")],
                  ["Policy", textField(ticket, ["policy_version", "risk_policy_version"]) ? "Current risk policy" : "Not recorded"],
                  ["Decision revision", textField(ticket, ["decision_revision"], textField(resolution, ["decision_revision"])) ? "Recorded" : "Not recorded"],
                  ["Primary blocker", decisionReason(primaryBlocker)],
                ]} />
                {requiredFieldStates.map((state) => <DataFieldStateNotice key={state.field} state={state} />)}
              </DetailSection>

              <DetailSection title="Ticket legs">
                {legs.length ? <div className="space-y-2">{legs.map((leg, index) => (
                  <div key={`${textField(leg, ["option_type"], String(index))}-${index}`} className="rounded-md border border-border p-3 text-sm">
                    <div className="font-medium">{statusLabel(textField(leg, ["side"]).toLowerCase(), "Leg")} {statusLabel(textField(leg, ["option_type"]).toLowerCase(), "Option")} {formatMoney(numberField(leg, ["strike"], Number.NaN))}</div>
                    <div className="mt-1 text-xs text-muted-foreground">Bid {formatMoney(numberField(leg, ["bid"], Number.NaN))} · Ask {formatMoney(numberField(leg, ["ask"], Number.NaN))} · Quote {dateTime(textField(leg, ["quote_time"]))}</div>
                  </div>
                ))}</div> : <p className="text-sm text-muted-foreground">No complete execution legs are available. The ticket remains fail-closed.</p>}
              </DetailSection>

              <DetailSection title="Primary blocker">
                {blockers.length ? <ul className="space-y-1 text-sm text-muted-foreground">{blockers.map((blocker) => <li key={blocker}>• {decisionReason(blocker)}</li>)}</ul> : <p className="text-sm text-muted-foreground">No blocker is recorded.</p>}
              </DetailSection>

              <DetailSection title="Thesis and exits">
                <p className="text-sm">{textField(thesis, ["summary"], "No thesis summary is stored.")}</p>
                <p className="mt-2 text-sm text-muted-foreground">Invalidation: {textField(thesis, ["invalidation"], textField(exits, ["thesis_invalidation"], "Not recorded"))}</p>
              </DetailSection>

              <DetailSection title="Publication and outcome">
                <MetricGrid values={[
                  ["Published", dateTime(textField(publication, ["published_at"]))],
                  ["Current", statusLabel(textField(publication, ["current"]).toLowerCase(), "Not current")],
                  ["Outcome", statusLabel(textField(outcome, ["maturity_state", "paper_status"]).toLowerCase(), "Not resolved")],
                  ["Return", decimal(numberField(outcome, ["current_return", "return_20d"], Number.NaN))],
                  ["Max drawdown", decimal(numberField(outcome, ["max_drawdown"], Number.NaN))],
                ]} />
              </DetailSection>

              <DetailSection title="Evidence">
                {evidence.length ? <ul className="space-y-2 text-sm text-muted-foreground">{evidence.slice(0, 12).map((item, index) => <li key={`${textField(item, ["evidence_kind"], String(index))}-${index}`}>• {evidenceLabel(textField(item, ["evidence_kind"], "Evidence"))}</li>)}</ul> : <p className="text-sm text-muted-foreground">No evidence reference is stored.</p>}
              </DetailSection>

              <DetailSection title="Agent provenance">
                {Object.keys(agentProvenance).length ? <MetricGrid values={[
                  ["Advisory", Object.keys(agentProvenance).length ? "Attached" : "Not attached"],
                  ["Validation", statusLabel(textField(agentProvenance, ["validation_status", "status"]).toLowerCase(), "Not recorded")],
                ]} /> : <p className="text-sm text-muted-foreground">No agent advisory is attached to this ticket.</p>}
              </DetailSection>

              {symbol ? <Button type="button" variant="outline" onClick={() => onOpenTicker(symbol)}>Open canonical ticker action</Button> : null}
              <TechnicalDetails>
                <EvidenceFields value={{ decision_id: decisionId, ticket, publication, evidence, agent_provenance: agentProvenance }} />
              </TechnicalDetails>
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
