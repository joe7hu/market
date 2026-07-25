import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { AlertTriangle, CheckCircle2, History, Newspaper } from "lucide-react";

import { getThesisHistory, markThesisReviewed, saveThesis } from "@/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { DataTableFrame, DecisionCard, EmptyState, EvidenceList, MetricTile, PageHeader, StatusBadge } from "@/components/market/workstation";
import type { PanelData, RowRecord } from "@/types";
import { buildThesisMonitorViewModel } from "@/viewModels/thesisMonitor";
import { booleanField, displayField, formatMoney, formatPct, listField, numberField, symbolList, textField, titleLabel, toneFromText, type Tone } from "./rowFormat";
import { DataGridSection } from "./dataGridSection";

const CARD_LIMIT = 16;
const QUEUE_LIMIT = 10;
const GRID_LIMIT = 32;

export function ThesisMonitorPage({ data, onOpenTicker, onReload }: { data: PanelData; onOpenTicker: (symbol: string) => void; onReload: () => Promise<void> }) {
  const viewModel = buildThesisMonitorViewModel(data);
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const focusSymbol = (searchParams.get("symbol") ?? "").toUpperCase();
  const viewFeed = (symbol: string) => navigate(`/feed?ticker=${encodeURIComponent(symbol)}`);

  const matchesFocus = (row: RowRecord) => !focusSymbol || symbolList(row).includes(focusSymbol);
  const cards = viewModel.monitorRows.filter(matchesFocus);
  const ownedRisk = viewModel.ownedRisk.filter(matchesFocus);
  const watchlistGaps = viewModel.watchlistGaps.filter(matchesFocus);
  const current = viewModel.current.filter(matchesFocus);

  return (
    <section>
      <PageHeader
        eyebrow="Thesis and invalidation"
        title="Thesis Monitor"
        subtitle="Owned and watched names that need a thesis refresh, contradiction check, or invalidation review."
      />

      {focusSymbol ? (
        <div className="mb-4 flex items-center justify-between gap-3 rounded-md border border-border bg-muted/40 px-4 py-2 text-sm">
          <span>Focused on <strong>{focusSymbol}</strong> from the source feed.{cards.length ? "" : ` ${focusSymbol} is not a monitored (owned/watched) position.`}</span>
          <Button type="button" size="sm" variant="outline" onClick={() => setSearchParams({}, { replace: true })}>Show all</Button>
        </div>
      ) : null}

      <div className="mb-4 grid gap-3 md:grid-cols-2 xl:grid-cols-5">
        <MetricTile label="Owned Risk" value={ownedRisk.length} caption="owned exceptions" tone={ownedRisk.length ? "bad" : "good"} />
        <MetricTile label="Watch Gaps" value={watchlistGaps.length} caption="underwriting gaps" tone={watchlistGaps.length ? "warn" : "good"} />
        <MetricTile label="Active V3" value={viewModel.monitorRows.filter((row) => booleanField(row, ["has_active_revision"])).length} caption={`${viewModel.monitorRows.length} monitored`} tone="info" />
        <MetricTile label="Contradictions" value={viewModel.contradictions.length} caption="evidence conflicts" tone={viewModel.contradictions.length ? "bad" : "good"} />
        <MetricTile label="Invalidation Rules" value={viewModel.monitorRows.filter((row) => numberField(row, ["invalidation_rule_count"], 0) > 0).length} caption="covered names" tone="info" />
      </div>

      {cards.length ? (
        <div className="space-y-5">
          <LaneSection title="Owned Risk Exceptions" rows={ownedRisk} empty="No owned thesis exceptions." onOpenTicker={onOpenTicker} onViewFeed={viewFeed} onReload={onReload} />
          <LaneSection title="Watchlist Underwriting Gaps" rows={watchlistGaps} empty="No watchlist underwriting gaps." onOpenTicker={onOpenTicker} onViewFeed={viewFeed} onReload={onReload} />
          <CurrentSection rows={current} onOpenTicker={onOpenTicker} />
        </div>
      ) : (
        <EmptyState icon={AlertTriangle} title={focusSymbol ? `No monitored thesis for ${focusSymbol}` : "No thesis monitor loaded"} detail={focusSymbol ? "This name is not in your owned or watched set. Clear the focus to see all monitored theses." : "Refresh this page before using it for portfolio review."} />
      )}

      <div className="mt-5">
        <DataGridSection title="Structured Thesis Fields" rows={(cards.length ? cards : viewModel.thesisRows).slice(0, GRID_LIMIT)} onOpenTicker={onOpenTicker} />
      </div>
    </section>
  );
}

function LaneSection({ title, rows: laneRows, empty, onOpenTicker, onViewFeed, onReload }: { title: string; rows: RowRecord[]; empty: string; onOpenTicker: (symbol: string) => void; onViewFeed: (symbol: string) => void; onReload: () => Promise<void> }) {
  return (
    <section>
      <div className="mb-2 flex items-center justify-between gap-3 border-b border-border pb-2">
        <h2 className="text-lg font-semibold">{title}</h2>
        <StatusBadge tone={laneRows.length ? "warn" : "good"}>{laneRows.length}</StatusBadge>
      </div>
      {laneRows.length ? (
        <div className="space-y-3">
          {laneRows.slice(0, CARD_LIMIT).map((row, index) => (
            <ThesisCard key={textField(row, ["symbol"], `row-${index}`)} row={row} onOpenTicker={onOpenTicker} onViewFeed={onViewFeed} onReload={onReload} />
          ))}
          <Overflow shown={Math.min(CARD_LIMIT, laneRows.length)} total={laneRows.length} noun="exceptions" />
        </div>
      ) : (
        <div className="rounded-md border border-border p-4 text-sm text-muted-foreground">{empty}</div>
      )}
    </section>
  );
}

function CurrentSection({ rows: currentRows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: (symbol: string) => void }) {
  const [open, setOpen] = useState(false);
  if (!currentRows.length) return null;
  return (
    <section>
      <button type="button" className="flex w-full items-center justify-between gap-3 border-b border-border pb-2 text-left" onClick={() => setOpen((value) => !value)}>
        <h2 className="text-lg font-semibold">Current</h2>
        <StatusBadge tone="good">{open ? "Hide" : `${currentRows.length} collapsed`}</StatusBadge>
      </button>
      {open ? <QueuePanel title="Current theses" rows={currentRows} empty="No current theses." onOpenTicker={onOpenTicker} /> : null}
    </section>
  );
}

function Overflow({ shown, total, noun }: { shown: number; total: number; noun: string }) {
  if (total <= shown) return null;
  return <p className="px-1 text-xs text-muted-foreground">Showing {shown} of {total} {noun}.</p>;
}

function ThesisCard({ row, onOpenTicker, onViewFeed, onReload }: { row: RowRecord; onOpenTicker: (symbol: string) => void; onViewFeed: (symbol: string) => void; onReload: () => Promise<void> }) {
  const [editing, setEditing] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyRows, setHistoryRows] = useState<RowRecord[]>([]);
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
  const confidence = textField(row, ["confidence_tier", "confidence"], "unknown");
  const isAgent = textField(row, ["author_kind"]) === "ai";
  const exposure = numberField(row, ["portfolio_weight"], Number.NaN);
  const pnl = numberField(row, ["unrealized_pnl"], Number.NaN);
  const pnlPct = numberField(row, ["unrealized_pnl_pct"], Number.NaN);
  const priority = numberField(row, ["priority_score"], Number.NaN);
  const catalyst = textField(row, ["next_catalyst"]);
  const catalystDate = textField(row, ["next_catalyst_at"]);
  const quoteFreshness = textField(row, ["quote_freshness"], "unknown");
  const evidenceCards = recordArray(row, "evidence_cards");

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

  async function showHistory() {
    if (!primarySymbol) return;
    setBusy(true);
    setError(null);
    try {
      const payload = await getThesisHistory(primarySymbol);
      setHistoryRows(recordArray(payload, "revisions"));
      setHistoryOpen(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "History failed");
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
          <div className="text-muted-foreground">Confidence {titleLabel(confidence)} · Quote {titleLabel(quoteFreshness)}</div>
        </div>
      }
      evidence={<EvidenceCards rows={evidenceCards} fallback={evidence.slice(0, 4)} />}
      nextAction={
        <div className="space-y-2">
          <div className="font-medium">{displayField(row, ["review_reason", "stale_reason"], needsReview ? "Review thesis state" : "Monitor")}</div>
          <div className="grid gap-1 text-muted-foreground sm:grid-cols-2">
            <span>Priority: {Number.isFinite(priority) ? priority : "-"}</span>
            <span>Exposure: {Number.isFinite(exposure) ? formatPct(exposure) : "-"}</span>
            <span>P&L: {Number.isFinite(pnl) ? `${formatMoney(pnl)} ${Number.isFinite(pnlPct) ? `(${formatPct(pnlPct)})` : ""}` : "-"}</span>
            <span>Catalyst: {catalyst ? `${catalyst}${catalystDate ? ` · ${catalystDate.slice(0, 10)}` : ""}` : "-"}</span>
          </div>
          <div className="text-muted-foreground">Invalidation: {displayField(row, ["invalidation", "invalidation_text", "invalidation_price"], "Not set")}</div>
          {flags.length ? <div className="text-red-700">Flags: {flags.map(titleLabel).join(", ")}</div> : null}
          {error ? <div className="text-red-700">{error}</div> : null}
          <div className="flex flex-wrap gap-2 pt-1">
            <Button type="button" size="sm" variant="outline" disabled={!primarySymbol} onClick={() => primarySymbol && onOpenTicker(primarySymbol)}>Open</Button>
            <Button type="button" size="sm" variant="outline" className="gap-1" disabled={!primarySymbol} onClick={() => primarySymbol && onViewFeed(primarySymbol)}><Newspaper className="size-3.5" /> Source feed</Button>
            <Button type="button" size="sm" variant="secondary" disabled={!primarySymbol || busy} onClick={() => runAction(() => markThesisReviewed(primarySymbol, "unchanged"))}>Unchanged</Button>
            <Button type="button" size="sm" variant="secondary" disabled={!primarySymbol || busy} onClick={() => runAction(() => markThesisReviewed(primarySymbol, "invalidated"))}>Invalidated</Button>
            <Button type="button" size="sm" variant="outline" className="gap-1" disabled={!primarySymbol || busy} onClick={showHistory}><History className="size-3.5" /> History</Button>
            <Button type="button" size="sm" variant="ghost" disabled={!primarySymbol} onClick={() => setEditing((value) => !value)}>{editing ? "Cancel" : "Edit thesis"}</Button>
          </div>
          {editing && primarySymbol ? <ThesisEditor row={row} symbol={primarySymbol} busy={busy} onSave={(input) => runAction(() => saveThesis(primarySymbol, input))} /> : null}
          {historyOpen ? <HistoryPanel rows={historyRows} /> : null}
        </div>
      }
      symbols={symbols}
      tone={tone}
    />
  );
}

function EvidenceCards({ rows: evidenceRows, fallback }: { rows: RowRecord[]; fallback: string[] }) {
  if (!evidenceRows.length) return <EvidenceList items={fallback} />;
  return (
    <div className="grid gap-2">
      {evidenceRows.slice(0, 3).map((item, index) => (
        <div key={textField(item, ["reference"], `evidence-${index}`)} className="rounded-md border border-border p-2 text-xs">
          <div className="flex items-start justify-between gap-2">
            <strong className="line-clamp-2 text-foreground">{displayField(item, ["title", "reference"], "Evidence")}</strong>
            <StatusBadge tone={toneFromText(textField(item, ["stance"], "unassessed"))}>{titleLabel(textField(item, ["stance"], "unassessed"))}</StatusBadge>
          </div>
          <div className="mt-1 text-muted-foreground">{textField(item, ["source_name"], "Source")} · {textField(item, ["materiality"], "unknown")} · {textField(item, ["date"]).slice(0, 10)}</div>
          <p className="mt-1 line-clamp-2 text-muted-foreground">{displayField(item, ["rationale"], "")}</p>
        </div>
      ))}
    </div>
  );
}

function HistoryPanel({ rows }: { rows: RowRecord[] }) {
  return (
    <div className="rounded-md border border-border bg-background/60 p-3">
      <p className="text-xs font-semibold uppercase text-muted-foreground">Revision history</p>
      {rows.length ? (
        <div className="mt-2 space-y-2">
          {rows.slice(0, 4).map((row, index) => (
            <div key={textField(row, ["revision_id"], `history-${index}`)} className="text-sm">
              <span className="font-medium">Rev {displayField(row, ["revision"], "?")}</span>
              <span className="text-muted-foreground"> · {titleLabel(textField(row, ["author_kind"], "unknown"))} · {textField(row, ["created_at"]).slice(0, 10)}</span>
              <div className="text-xs text-muted-foreground">Changed: {displayField(recordValue(row, "diff"), ["changed_keys"], "unknown")}</div>
            </div>
          ))}
        </div>
      ) : (
        <p className="mt-2 text-sm text-muted-foreground">No revision history loaded.</p>
      )}
    </div>
  );
}

function ThesisEditor({ row, busy, onSave }: { row: RowRecord; symbol: string; busy: boolean; onSave: (input: Parameters<typeof saveThesis>[1]) => void }) {
  const raw = recordValue(row, "raw_thesis");
  const [thesis, setThesis] = useState(textField(row, ["thesis", "thesis_text"]));
  const [why, setWhy] = useState(textField(row, ["why_owned_watched", "why_owned", "why_watched", "why"]));
  const [direction, setDirection] = useState(textField(row, ["direction"], textField(raw, ["direction"], "long")));
  const [horizon, setHorizon] = useState(textField(row, ["horizon_date"], textField(raw, ["horizon_date"])));
  const [conviction, setConviction] = useState(textField(row, ["conviction"], textField(raw, ["conviction"], "unknown")));
  const [confidence, setConfidence] = useState(textField(row, ["confidence"], textField(raw, ["confidence"], "low")));
  const [automationPolicy, setAutomationPolicy] = useState(textField(row, ["automation_policy"], textField(raw, ["automation_policy"], "auto")));
  const [changeRationale, setChangeRationale] = useState("");
  const [invalidation, setInvalidation] = useState(textField(row, ["invalidation", "invalidation_text"]));
  const [rules, setRules] = useState(JSON.stringify(recordArray(row, "invalidation_rules").length ? recordArray(row, "invalidation_rules") : recordArray(raw, "invalidation_rules"), null, 2));
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
      <div className="grid gap-2 md:grid-cols-2">
        <Field label="Direction">
          <Input value={direction} onChange={(event) => setDirection(event.target.value)} placeholder="long, bullish, bearish, short" />
        </Field>
        <Field label="Horizon date">
          <Input type="date" value={horizon} onChange={(event) => setHorizon(event.target.value)} />
        </Field>
        <Field label="Conviction">
          <Input value={conviction} onChange={(event) => setConviction(event.target.value)} placeholder="low / medium / high" />
        </Field>
        <Field label="Confidence">
          <Input value={confidence} onChange={(event) => setConfidence(event.target.value)} placeholder="low / medium / high" />
        </Field>
      </div>
      <Field label="Invalidation">
        <textarea className="min-h-12 w-full rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring" value={invalidation} onChange={(event) => setInvalidation(event.target.value)} placeholder="What would prove the thesis wrong" />
      </Field>
      <Field label="Invalidation price">
        <Input type="number" inputMode="decimal" value={price} onChange={(event) => setPrice(event.target.value)} placeholder="Optional price stop" />
      </Field>
      <Field label="Typed invalidation rules">
        <textarea className="min-h-20 w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring" value={rules} onChange={(event) => setRules(event.target.value)} placeholder='[{"type":"price","operator":"<=","price":95,"text":"Breaks below support"}]' />
      </Field>
      <div className="grid gap-2 md:grid-cols-2">
        <Field label="Automation policy">
          <select className="h-9 rounded-md border border-input bg-background px-3 text-sm" value={automationPolicy} onChange={(event) => setAutomationPolicy(event.target.value)}>
            <option value="auto">Auto</option>
            <option value="manual_lock">Manual lock</option>
          </select>
        </Field>
        <Field label="Change rationale">
          <Input value={changeRationale} onChange={(event) => setChangeRationale(event.target.value)} placeholder="Why this revision changed" />
        </Field>
      </div>
      <div className="flex justify-end">
        <Button
          type="button"
          size="sm"
          disabled={!canSave}
          onClick={() => {
            let invalidationRules: RowRecord[] = [];
            try {
              const parsed = JSON.parse(rules || "[]") as unknown;
              invalidationRules = Array.isArray(parsed) ? parsed.filter((item): item is RowRecord => Boolean(item) && typeof item === "object" && !Array.isArray(item)) : [];
            } catch {
              invalidationRules = [];
            }
            onSave({
              thesis: thesis.trim(),
              why: why.trim(),
              direction: direction.trim(),
              horizon_date: horizon || null,
              conviction: conviction.trim(),
              confidence: confidence.trim(),
              invalidation: invalidation.trim(),
              invalidation_price: price.trim() === "" ? null : Number(price),
              invalidation_rules: invalidationRules,
              automation_policy: automationPolicy === "manual_lock" ? "manual_lock" : "auto",
              change_rationale: changeRationale.trim() || null,
            });
          }}
        >
          Save thesis
        </Button>
      </div>
    </div>
  );
}

function recordArray(source: RowRecord | undefined, key: string): RowRecord[] {
  const value = source?.[key];
  if (!Array.isArray(value)) return [];
  return value
    .filter((item) => Boolean(item) && typeof item === "object" && !Array.isArray(item))
    .map((item) => item as RowRecord);
}

function recordValue(source: RowRecord | undefined, key: string): RowRecord {
  const value = source?.[key];
  return value && typeof value === "object" && !Array.isArray(value) ? value as RowRecord : {};
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
