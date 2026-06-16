import { useState } from "react";
import { AlertTriangle, CheckCircle2 } from "lucide-react";

import { markThesisReviewed, saveThesis } from "@/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { DataTableFrame, DecisionCard, EmptyState, EvidenceList, MetricTile, PageHeader, StatusBadge } from "@/components/market/workstation";
import type { PanelData, RowRecord } from "@/types";
import { buildThesisMonitorViewModel } from "@/viewModels/thesisMonitor";
import { booleanField, displayField, listField, numberField, symbolList, textField, titleLabel, toneFromText, type Tone } from "./rowFormat";
import { DataGridSection } from "./dataGridSection";

const CARD_LIMIT = 16;
const QUEUE_LIMIT = 10;
const GRID_LIMIT = 32;

export function ThesisMonitorPage({ data, onOpenTicker, onReload }: { data: PanelData; onOpenTicker: (symbol: string) => void; onReload: () => Promise<void> }) {
  const viewModel = buildThesisMonitorViewModel(data);

  return (
    <section>
      <PageHeader
        eyebrow="Thesis and invalidation"
        title="Thesis Monitor"
        subtitle="Owned and watched names that need a thesis refresh, contradiction check, or invalidation review."
      />

      <div className="mb-4 grid gap-3 md:grid-cols-2 xl:grid-cols-5">
        <MetricTile label="Needs Review" value={viewModel.needsReview.length} caption="thesis review reasons" tone={viewModel.needsReview.length ? "warn" : "good"} />
        <MetricTile label="Incomplete" value={viewModel.incomplete.length} caption="missing thesis fields" tone={viewModel.incomplete.length ? "warn" : "good"} />
        <MetricTile label="Aging" value={viewModel.aging.length} caption="reviewed too long ago" tone={viewModel.aging.length ? "warn" : "good"} />
        <MetricTile label="Contradictions" value={viewModel.contradictions.length} caption="evidence conflicts" tone={viewModel.contradictions.length ? "bad" : "good"} />
        <MetricTile label="Invalidation Watch" value={viewModel.invalidationWatch.length} caption="price near or through stop" tone={viewModel.invalidationWatch.length ? "info" : "muted"} />
      </div>

      {viewModel.monitorRows.length ? (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1.1fr)_minmax(0,0.9fr)]">
          <div className="space-y-3">
            {viewModel.monitorRows.slice(0, CARD_LIMIT).map((row, index) => (
              <ThesisCard key={textField(row, ["symbol"], `row-${index}`)} row={row} onOpenTicker={onOpenTicker} onReload={onReload} />
            ))}
            <Overflow shown={Math.min(CARD_LIMIT, viewModel.monitorRows.length)} total={viewModel.monitorRows.length} noun="monitored names" />
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
        <DataGridSection title="Structured Thesis Fields" rows={(viewModel.monitorRows.length ? viewModel.monitorRows : viewModel.thesisRows).slice(0, GRID_LIMIT)} onOpenTicker={onOpenTicker} />
      </div>
    </section>
  );
}

function Overflow({ shown, total, noun }: { shown: number; total: number; noun: string }) {
  if (total <= shown) return null;
  return <p className="px-1 text-xs text-muted-foreground">Showing {shown} of {total} {noun}.</p>;
}

function ThesisCard({ row, onOpenTicker, onReload }: { row: RowRecord; onOpenTicker: (symbol: string) => void; onReload: () => Promise<void> }) {
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const symbols = symbolList(row);
  const primarySymbol = symbols[0];
  const flags = listField(row, ["contradiction_flags"]);
  const needsReview = booleanField(row, ["needs_review"]);
  const stale = booleanField(row, ["stale_thesis"]);
  const status = textField(row, ["status"], needsReview ? "review" : "monitor");
  const tone: Tone = flags.some((flag) => flag.toLowerCase().includes("breach")) ? "bad" : needsReview || stale ? "warn" : toneFromText(status);
  const evidence = listField(row, ["evidence_links", "evidence", "sources"]);
  const age = numberField(row, ["last_reviewed_age_days"], Number.NaN);
  const confidence = numberField(row, ["agent_confidence"], Number.NaN);
  const isAgent = textField(row, ["source"]) === "agent_thesis";

  async function runAction(action: () => Promise<void>) {
    if (!primarySymbol) return;
    setBusy(true);
    setError(null);
    try {
      await action();
      await onReload();
      setEditing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Action failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <DecisionCard
      title={`${primarySymbol || "Symbol"}: ${displayField(row, ["thesis", "thesis_text"], "No thesis")}`}
      status={
        <div className="flex items-center gap-1.5">
          {isAgent ? <StatusBadge tone="info">Agent</StatusBadge> : null}
          <StatusBadge tone={tone}>{needsReview ? "Review" : titleLabel(status)}</StatusBadge>
        </div>
      }
      reason={
        <div className="space-y-1">
          <div>{displayField(row, ["why_owned", "why_watched", "why", "reason"], "No why-owned/watched field")}</div>
          {Number.isFinite(age) ? <div className="text-muted-foreground">Last reviewed {Math.round(age)} days ago</div> : null}
          {Number.isFinite(confidence) ? <div className="text-muted-foreground">Agent confidence {(confidence * 100).toFixed(0)}%</div> : null}
        </div>
      }
      evidence={<EvidenceList items={evidence.slice(0, 4)} />}
      nextAction={
        <div className="space-y-2">
          <div>{displayField(row, ["review_reason", "stale_reason"], needsReview ? "Review thesis state" : "Monitor")}</div>
          <div className="text-muted-foreground">Invalidation: {displayField(row, ["invalidation", "invalidation_text", "invalidation_price"], "Not set")}</div>
          {flags.length ? <div className="text-red-700">Flags: {flags.map(titleLabel).join(", ")}</div> : null}
          {error ? <div className="text-red-700">{error}</div> : null}
          <div className="flex flex-wrap gap-2 pt-1">
            <Button type="button" size="sm" variant="outline" disabled={!primarySymbol} onClick={() => primarySymbol && onOpenTicker(primarySymbol)}>Open</Button>
            <Button type="button" size="sm" variant="secondary" disabled={!primarySymbol || busy} onClick={() => runAction(() => markThesisReviewed(primarySymbol))}>Mark reviewed</Button>
            <Button type="button" size="sm" variant="ghost" disabled={!primarySymbol} onClick={() => setEditing((value) => !value)}>{editing ? "Cancel" : "Edit thesis"}</Button>
          </div>
          {editing && primarySymbol ? <ThesisEditor row={row} symbol={primarySymbol} busy={busy} onSave={(input) => runAction(() => saveThesis(primarySymbol, input))} /> : null}
        </div>
      }
      symbols={symbols}
      tone={tone}
    />
  );
}

function ThesisEditor({ row, busy, onSave }: { row: RowRecord; symbol: string; busy: boolean; onSave: (input: { thesis: string; why: string; invalidation: string; invalidation_price: number | null }) => void }) {
  const [thesis, setThesis] = useState(textField(row, ["thesis", "thesis_text"]));
  const [why, setWhy] = useState(textField(row, ["why_owned_watched", "why_owned", "why_watched", "why"]));
  const [invalidation, setInvalidation] = useState(textField(row, ["invalidation", "invalidation_text"]));
  const [price, setPrice] = useState(() => {
    const value = numberField(row, ["invalidation_price"], Number.NaN);
    return Number.isFinite(value) ? String(value) : "";
  });

  const canSave = thesis.trim().length > 0 && !busy;

  return (
    <div className="mt-2 space-y-2 rounded-md border border-border bg-background/60 p-3">
      <Field label="Thesis">
        <textarea className="min-h-16 w-full rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring" value={thesis} onChange={(event) => setThesis(event.target.value)} placeholder="Core thesis (required)" />
      </Field>
      <Field label="Why owned/watched">
        <textarea className="min-h-12 w-full rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring" value={why} onChange={(event) => setWhy(event.target.value)} placeholder="Why this is owned or watched" />
      </Field>
      <Field label="Invalidation">
        <textarea className="min-h-12 w-full rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring" value={invalidation} onChange={(event) => setInvalidation(event.target.value)} placeholder="What would prove the thesis wrong" />
      </Field>
      <Field label="Invalidation price">
        <Input type="number" inputMode="decimal" value={price} onChange={(event) => setPrice(event.target.value)} placeholder="Optional price stop" />
      </Field>
      <div className="flex justify-end">
        <Button
          type="button"
          size="sm"
          disabled={!canSave}
          onClick={() => onSave({ thesis: thesis.trim(), why: why.trim(), invalidation: invalidation.trim(), invalidation_price: price.trim() === "" ? null : Number(price) })}
        >
          Save thesis
        </Button>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-xs font-semibold uppercase text-muted-foreground">{label}</span>
      {children}
    </label>
  );
}

function QueuePanel({ title, rows: queueRows, empty, onOpenTicker }: { title: string; rows: RowRecord[]; empty: string; onOpenTicker: (symbol: string) => void }) {
  return (
    <DataTableFrame title={title}>
      {queueRows.length ? (
        <div className="divide-y divide-border">
          {queueRows.slice(0, QUEUE_LIMIT).map((row, index) => {
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
          <Overflow shown={Math.min(QUEUE_LIMIT, queueRows.length)} total={queueRows.length} noun="rows" />
        </div>
      ) : (
        <div className="p-4 text-sm text-muted-foreground">{empty}</div>
      )}
    </DataTableFrame>
  );
}
