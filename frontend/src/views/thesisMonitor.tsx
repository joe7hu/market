import { AlertTriangle, CheckCircle2 } from "lucide-react";
import { type KeyboardEvent } from "react";

import { DataTableFrame, DecisionCard, EmptyState, EvidenceList, MetricTile, PageHeader, StatusBadge } from "@/components/market/workstation";
import type { AppModel } from "@/model";
import type { PanelData, RowRecord } from "@/types";
import { rows } from "@/utils";
import { booleanField, displayField, fullField, listField, numberField, symbolList, textField, titleLabel, toneFromText, type Tone } from "./rowFormat";

export function ThesisMonitorPage({ data, model, onOpenTicker }: { data: PanelData; model: AppModel; onOpenTicker: (symbol: string) => void }) {
  const monitorRows = rows(data.thesisMonitor);
  const thesisRows = rows(data.theses);
  const needsReview = monitorRows.filter((row) => booleanField(row, ["needs_review"]));
  const stale = monitorRows.filter((row) => booleanField(row, ["stale_thesis"]));
  const contradictions = monitorRows.filter((row) => listField(row, ["contradiction_flags"]).length);
  const invalidationWatch = monitorRows.filter((row) => Number.isFinite(numberField(row, ["invalidation_distance_pct"], Number.NaN)) || textField(row, ["invalidation_price"]));

  return (
    <section>
      <PageHeader
        eyebrow="Thesis and invalidation"
        title="Thesis Monitor"
        subtitle="Owned and watched names that need a thesis refresh, contradiction check, or invalidation review."
      />

      <div className="mb-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <MetricTile label="Needs Review" value={needsReview.length} caption="backend review_reason present" tone={needsReview.length ? "warn" : "good"} />
        <MetricTile label="Stale Thesis" value={stale.length} caption="stale_thesis flag" tone={stale.length ? "warn" : "good"} />
        <MetricTile label="Contradictions" value={contradictions.length} caption="contradiction_flags rows" tone={contradictions.length ? "bad" : "good"} />
        <MetricTile label="Invalidation Watch" value={invalidationWatch.length} caption="price or distance loaded" tone={invalidationWatch.length ? "info" : "muted"} />
      </div>

      {monitorRows.length ? (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.1fr)_minmax(0,0.9fr)]">
          <div className="space-y-3">
            {monitorRows.slice(0, 16).map((row, index) => <ThesisCard key={textField(row, ["symbol"], `row-${index}`)} row={row} onOpenTicker={onOpenTicker} />)}
          </div>
          <div className="space-y-4">
            <QueuePanel title="Review Queue" rows={needsReview} empty="No stale thesis or contradiction review reasons are active." onOpenTicker={onOpenTicker} />
            <QueuePanel title="Invalidation Watch" rows={invalidationWatch} empty="No invalidation distance rows are currently loaded." onOpenTicker={onOpenTicker} />
          </div>
        </div>
      ) : (
        <EmptyState icon={AlertTriangle} title="No thesis monitor rows" detail="Run the thesis monitor read model before using this page for portfolio review." />
      )}

      <div className="mt-4">
        <DataTableFrame title="Structured Thesis Fields">
          <table className="w-full min-w-[920px] text-sm">
            <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2">Symbol</th>
                <th className="px-3 py-2">Status</th>
                <th className="px-3 py-2">Thesis</th>
                <th className="px-3 py-2">Invalidation</th>
                <th className="px-3 py-2">Review Reason</th>
              </tr>
            </thead>
            <tbody>
              {(monitorRows.length ? monitorRows : thesisRows).slice(0, 32).map((row, index) => (
                <tr key={index} className="border-b border-border align-top">
                  <td className="px-3 py-2 font-semibold">{displayField(row, ["symbol", "ticker"])}</td>
                  <td className="px-3 py-2">{displayField(row, ["status"])}</td>
                  <td className="px-3 py-2">{fullField(row, ["thesis", "thesis_text"], "No thesis")}</td>
                  <td className="px-3 py-2">{fullField(row, ["invalidation", "invalidation_text", "invalidation_price"], "No invalidation")}</td>
                  <td className="px-3 py-2">{fullField(row, ["review_reason", "stale_reason"], "-")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </DataTableFrame>
      </div>
    </section>
  );
}

function ThesisCard({ row, onOpenTicker }: { row: RowRecord; onOpenTicker: (symbol: string) => void }) {
  const symbols = symbolList(row);
  const flags = listField(row, ["contradiction_flags"]);
  const needsReview = booleanField(row, ["needs_review"]);
  const stale = booleanField(row, ["stale_thesis"]);
  const status = textField(row, ["status"], needsReview ? "review" : "monitor");
  const tone: Tone = flags.some((flag) => flag.toLowerCase().includes("breach")) ? "bad" : needsReview || stale ? "warn" : toneFromText(status);
  const evidence = listField(row, ["evidence_links", "evidence", "sources"]);
  const age = numberField(row, ["last_reviewed_age_days"], Number.NaN);
  const primarySymbol = symbols[0];
  const openTicker = () => {
    if (primarySymbol) onOpenTicker(primarySymbol);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!primarySymbol) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onOpenTicker(primarySymbol);
    }
  };

  return (
    <div
      role={primarySymbol ? "button" : undefined}
      tabIndex={primarySymbol ? 0 : -1}
      aria-disabled={primarySymbol ? undefined : true}
      className={primarySymbol ? "block w-full cursor-pointer text-left transition-transform hover:-translate-y-px focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2" : "block w-full cursor-default text-left"}
      onClick={openTicker}
      onKeyDown={onKeyDown}
    >
      <DecisionCard
        title={`${symbols[0] || "Symbol"}: ${displayField(row, ["thesis", "thesis_text"], "No thesis")}`}
        status={<StatusBadge tone={tone}>{needsReview ? "Review" : titleLabel(status)}</StatusBadge>}
        reason={
          <div className="space-y-1">
            <div>{displayField(row, ["why_owned", "why_watched", "why", "reason"], "No why-owned/watched field")}</div>
            {Number.isFinite(age) ? <div className="text-muted-foreground">Last reviewed {Math.round(age)} days ago</div> : null}
          </div>
        }
        evidence={<EvidenceList items={evidence.slice(0, 4)} />}
        nextAction={
          <div className="space-y-1">
            <div>{displayField(row, ["review_reason", "stale_reason"], needsReview ? "Review thesis state" : "Monitor")}</div>
            <div className="text-muted-foreground">Invalidation: {displayField(row, ["invalidation", "invalidation_text", "invalidation_price"], "Not set")}</div>
            {flags.length ? <div className="text-red-700">Flags: {flags.map(titleLabel).join(", ")}</div> : null}
          </div>
        }
        symbols={symbols}
        tone={tone}
      />
    </div>
  );
}

function QueuePanel({ title, rows: queueRows, empty, onOpenTicker }: { title: string; rows: RowRecord[]; empty: string; onOpenTicker: (symbol: string) => void }) {
  return (
    <DataTableFrame title={title}>
      {queueRows.length ? (
        <div className="divide-y divide-border">
          {queueRows.slice(0, 10).map((row, index) => {
            const symbols = symbolList(row);
            return (
              <button key={index} type="button" className="flex min-h-14 w-full items-start gap-3 px-4 py-3 text-left hover:bg-muted/50" onClick={() => symbols[0] && onOpenTicker(symbols[0])} disabled={!symbols[0]}>
                <CheckCircle2 className="mt-1 size-4 text-muted-foreground" />
                <span className="min-w-0 flex-1">
                  <strong className="block truncate">{symbols[0] || "Symbol"}</strong>
                  <span className="block text-sm leading-6 text-muted-foreground">{displayField(row, ["review_reason", "stale_reason", "invalidation"], "Review")}</span>
                </span>
              </button>
            );
          })}
        </div>
      ) : (
        <div className="p-4 text-sm text-muted-foreground">{empty}</div>
      )}
    </DataTableFrame>
  );
}
