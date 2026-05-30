import { AlertTriangle, CheckCircle2 } from "lucide-react";

import { ClickableDecisionCard, DataTableFrame, EmptyState, EvidenceList, MetricTile, PageHeader, StatusBadge } from "@/components/market/workstation";
import type { PanelData, RowRecord } from "@/types";
import { buildThesisMonitorViewModel } from "@/viewModels/thesisMonitor";
import { booleanField, displayField, listField, numberField, symbolList, textField, titleLabel, toneFromText, type Tone } from "./rowFormat";
import { DataGridSection } from "./dataGridSection";

export function ThesisMonitorPage({ data, onOpenTicker }: { data: PanelData; onOpenTicker: (symbol: string) => void }) {
  const viewModel = buildThesisMonitorViewModel(data);

  return (
    <section>
      <PageHeader
        eyebrow="Thesis and invalidation"
        title="Thesis Monitor"
        subtitle="Owned and watched names that need a thesis refresh, contradiction check, or invalidation review."
      />

      <div className="mb-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <MetricTile label="Needs Review" value={viewModel.needsReview.length} caption="thesis review reasons" tone={viewModel.needsReview.length ? "warn" : "good"} />
        <MetricTile label="Aging Thesis" value={viewModel.stale.length} caption="theses to revisit" tone={viewModel.stale.length ? "warn" : "good"} />
        <MetricTile label="Contradictions" value={viewModel.contradictions.length} caption="evidence conflicts" tone={viewModel.contradictions.length ? "bad" : "good"} />
        <MetricTile label="Invalidation Watch" value={viewModel.invalidationWatch.length} caption="price or thesis trigger" tone={viewModel.invalidationWatch.length ? "info" : "muted"} />
      </div>

      {viewModel.monitorRows.length ? (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.1fr)_minmax(0,0.9fr)]">
          <div className="space-y-3">
            {viewModel.monitorRows.slice(0, 16).map((row, index) => <ThesisCard key={textField(row, ["symbol"], `row-${index}`)} row={row} onOpenTicker={onOpenTicker} />)}
          </div>
          <div className="space-y-4">
            <QueuePanel title="Review Queue" rows={viewModel.needsReview} empty="No thesis or contradiction reviews are active." onOpenTicker={onOpenTicker} />
            <QueuePanel title="Invalidation Watch" rows={viewModel.invalidationWatch} empty="No invalidation triggers are active." onOpenTicker={onOpenTicker} />
          </div>
        </div>
      ) : (
        <EmptyState icon={AlertTriangle} title="No thesis monitor loaded" detail="Refresh this page before using it for portfolio review." />
      )}

      <div className="mt-4">
        <DataGridSection title="Structured Thesis Fields" rows={(viewModel.monitorRows.length ? viewModel.monitorRows : viewModel.thesisRows).slice(0, 32)} onOpenTicker={onOpenTicker} />
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

  return (
    <ClickableDecisionCard
      enabled={Boolean(primarySymbol)}
      onOpen={() => primarySymbol && onOpenTicker(primarySymbol)}
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
