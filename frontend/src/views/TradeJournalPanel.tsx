import { NotebookPen } from "lucide-react";
import { useMemo } from "react";

import { DataTableFrame, EmptyState, StatusBadge } from "@/components/market/workstation";
import { cn } from "@/lib/utils";
import type { RowRecord } from "@/types";
import type { Tone } from "@/ui/tone";
import { formatMoney, numberField, textField } from "./rowFormat";
import type { OpenTicker } from "./workspacePage";

// Trade journal (Phase 4): the real-money log captured from the opportunity drawer. Each
// entry snapshots the predicted EV / P(2x) / conviction at click; realized_return + status
// fill in as the position resolves, which is exactly what the calibration dashboard grades.

type TradeJournalPanelProps = {
  rows: RowRecord[];
  onOpenTicker: OpenTicker;
};

export function TradeJournalPanel({ rows, onOpenTicker }: TradeJournalPanelProps) {
  const entries = useMemo(() => [...rows].sort((left, right) => textField(right, ["created_at"]).localeCompare(textField(left, ["created_at"]))), [rows]);

  const summary = useMemo(() => {
    const open = entries.filter((row) => textField(row, ["realized_status"], "open") === "open").length;
    const closed = entries.length - open;
    const settled = entries.filter((row) => Number.isFinite(numberField(row, ["realized_return"], Number.NaN)));
    const wins = settled.filter((row) => numberField(row, ["realized_return"], 0) > 0).length;
    return { open, closed, hitRate: settled.length ? wins / settled.length : Number.NaN };
  }, [entries]);

  if (!entries.length) {
    return (
      <DataTableFrame title="Trade Journal">
        <EmptyState
          title="No trades logged yet"
          detail="Open an opportunity's detail drawer and use “Log trade” to snapshot its predicted EV, P(2x) and conviction. Logged trades are what the calibration dashboard grades against."
          icon={NotebookPen}
        />
      </DataTableFrame>
    );
  }

  return (
    <DataTableFrame
      title="Trade Journal"
      action={
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>{entries.length.toLocaleString()} logged</span>
          <span>{summary.open.toLocaleString()} open</span>
          <span>{summary.closed.toLocaleString()} closed</span>
          {Number.isFinite(summary.hitRate) ? <StatusBadge tone={summary.hitRate >= 0.5 ? "good" : "warn"}>{`${Math.round(summary.hitRate * 100)}% in profit`}</StatusBadge> : null}
        </div>
      }
    >
      <div className="overflow-x-auto p-3">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border text-left text-[10px] uppercase text-muted-foreground">
              <th className="py-1 pr-3 font-medium">Logged</th>
              <th className="py-1 pr-3 font-medium">Ticker</th>
              <th className="py-1 pr-3 font-medium">Contract</th>
              <th className="py-1 pr-3 text-right font-medium">Entry</th>
              <th className="py-1 pr-3 text-right font-medium">Conv</th>
              <th className="py-1 pr-3 text-right font-medium">Pred EV / P(2x)</th>
              <th className="py-1 pr-3 font-medium">Status</th>
              <th className="py-1 pr-3 text-right font-medium">Realized</th>
              <th className="py-1 font-medium">Notes</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((row) => {
              const status = textField(row, ["realized_status"], "open");
              const realized = numberField(row, ["realized_return"], Number.NaN);
              return (
                <tr key={textField(row, ["journal_id"])} className="border-b border-border/50 align-top last:border-0">
                  <td className="py-1.5 pr-3 tabular-nums text-muted-foreground">{formatDate(textField(row, ["created_at"]))}</td>
                  <td className="py-1.5 pr-3">
                    <button type="button" onClick={() => onOpenTicker(textField(row, ["ticker"]))} className="font-medium text-foreground hover:text-primary">
                      {textField(row, ["ticker"])}
                    </button>
                  </td>
                  <td className="py-1.5 pr-3 text-muted-foreground">{textField(row, ["contract_id"], "—")}</td>
                  <td className="py-1.5 pr-3 text-right tabular-nums text-foreground">{money(numberField(row, ["entry_premium"], Number.NaN))}</td>
                  <td className="py-1.5 pr-3 text-right tabular-nums text-muted-foreground">{integer(numberField(row, ["conviction_score"], Number.NaN))}</td>
                  <td className="py-1.5 pr-3 text-right tabular-nums text-muted-foreground">
                    {decimalX(numberField(row, ["predicted_ev_multiple"], Number.NaN))} / {prob(numberField(row, ["predicted_p2x"], Number.NaN))}
                  </td>
                  <td className="py-1.5 pr-3">
                    <StatusBadge tone={statusTone(status)}>{titleCase(status)}</StatusBadge>
                  </td>
                  <td className={cn("py-1.5 pr-3 text-right tabular-nums", returnTone(realized))}>{Number.isFinite(realized) ? `${realized >= 0 ? "+" : ""}${Math.round(realized * 100)}%` : "—"}</td>
                  <td className="py-1.5 max-w-[16rem] truncate text-muted-foreground" title={textField(row, ["notes"])}>{textField(row, ["notes"], "—") || "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </DataTableFrame>
  );
}

function money(value: number): string {
  return Number.isFinite(value) ? formatMoney(value) : "—";
}

function integer(value: number): string {
  return Number.isFinite(value) ? String(Math.round(value)) : "—";
}

function decimalX(value: number): string {
  return Number.isFinite(value) ? `${value.toFixed(1)}x` : "—";
}

function prob(value: number): string {
  return Number.isFinite(value) ? `${(value * 100).toFixed(0)}%` : "—";
}

function titleCase(value: string): string {
  return value ? value.charAt(0).toUpperCase() + value.slice(1) : "—";
}

function statusTone(status: string): Tone {
  if (status === "open") return "info";
  if (status === "win" || status === "closed_win") return "good";
  if (status === "loss" || status === "closed_loss") return "bad";
  return "muted";
}

function returnTone(value: number): string {
  if (!Number.isFinite(value)) return "text-muted-foreground";
  if (value > 0) return "text-emerald-600 dark:text-emerald-400";
  if (value < 0) return "text-rose-600 dark:text-rose-400";
  return "text-foreground";
}

function formatDate(value: string): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
