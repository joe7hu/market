import { AlertTriangle, Clock3, ShieldAlert } from "lucide-react";

import { Card, CardContent } from "@/components/ui/card";
import { StatusBadge } from "@/components/market/workstation";
import type { RowRecord } from "@/types";
import { textField } from "./rowFormat";

type Props = {
  truths: RowRecord[];
  packets?: RowRecord[];
  onOpenTicker?: (symbol: string) => void;
};

export function EventScoutPanel({ truths, packets = [], onOpenTicker }: Props) {
  const rows = truths.filter((row) => textField(row, ["lane"]) === "event_scout").slice(0, 5);
  if (!rows.length) return null;
  return (
    <section className="mb-6">
      <div className="mb-3 flex items-end justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Intraday Event Scout</h2>
          <p className="text-xs text-muted-foreground">One timestamped packet feeds both tactical and fundamental conclusions.</p>
        </div>
        <StatusBadge tone="info">{rows.length} current</StatusBadge>
      </div>
      <div className="grid gap-3 lg:grid-cols-2">
        {rows.map((truth) => {
          const symbol = textField(truth, ["symbol"], "—");
          const packet = packets.find((row) => textField(row, ["event_id"]) === textField(truth, ["event_id"]));
          const positioning = objectField(packet, "positioning");
          const shortRecord = fieldValue(positioning, "short_interest_record_date");
          const tactical = objectField(packet, "tactical_decision");
          const doNotShort = Boolean(tactical.do_not_short);
          const verdict = textField(truth, ["candidate_state"]) === "SETUP" && textField(truth, ["route_verdict"]) === "NO_TRADE"
            ? "Research candidate · No trade"
            : `${textField(truth, ["candidate_state"], "Research")} · ${textField(truth, ["route_verdict"], "No trade")}`;
          return (
            <Card key={`${symbol}-${textField(truth, ["event_id", "as_of"])}`}>
              <CardContent className="space-y-3 p-4">
                <div className="flex items-start justify-between gap-3">
                  {onOpenTicker ? (
                    <button type="button" className="font-semibold hover:underline" onClick={() => onOpenTicker(symbol)}>{symbol}</button>
                  ) : <strong>{symbol}</strong>}
                  <StatusBadge tone={doNotShort ? "warn" : "info"}>{verdict}</StatusBadge>
                </div>
                <div className="grid gap-2 text-xs text-muted-foreground sm:grid-cols-2">
                  <span className="inline-flex items-center gap-1"><Clock3 className="size-3" /> as of {textField(truth, ["as_of"], "unknown")}</span>
                  <span>Blocker: {textField(truth, ["primary_blocker"], "none")}</span>
                  {shortRecord ? <span>Short record date: {shortRecord}</span> : null}
                  <span>Execution: {textField(truth, ["execution_state"], "disabled")}</span>
                </div>
                {doNotShort ? (
                  <p className="flex items-start gap-2 text-sm font-medium text-amber-700 dark:text-amber-300"><ShieldAlert className="mt-0.5 size-4 shrink-0" />Do not short: squeeze risk is elevated on the available evidence.</p>
                ) : null}
                <p className="flex items-start gap-2 text-sm text-muted-foreground"><AlertTriangle className="mt-0.5 size-4 shrink-0" />Next: {textField(truth, ["next_action"], "Refresh evidence.")}</p>
              </CardContent>
            </Card>
          );
        })}
      </div>
    </section>
  );
}

function objectField(row: RowRecord | undefined, key: string): Record<string, unknown> {
  const value = row?.[key];
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function fieldValue(row: Record<string, unknown>, key: string): string | null {
  const value = row[key];
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const nested = (value as Record<string, unknown>).value;
  return nested === null || nested === undefined ? null : String(nested);
}
