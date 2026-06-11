import { ArrowUpRight, Loader2, NotebookPen, Target } from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { StatusBadge } from "@/components/market/workstation";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { cn } from "@/lib/utils";
import type { JsonValue, RowRecord } from "@/types";
import type { Tone } from "@/ui/tone";
import { displayField, formatMoney, listField, numberField, textField, titleLabel } from "./rowFormat";
import type { OpenTicker } from "./workspacePage";

// Rich detail drawer for a single radar opportunity (Phase 4). The payoff diagram, score
// breakdown, entry plan, reasons and alternatives all read from the opportunity read-model
// row; the IV term sparkline reads the matching vol_surface_features row and degrades to a
// placeholder when that table is empty (it is populated on a slower cadence than candidates).

type OpportunityDetailDrawerProps = {
  opportunity: RowRecord | null;
  volSurface?: RowRecord;
  onClose: () => void;
  onOpenTicker: OpenTicker;
  onLogTrade?: (opportunity: RowRecord, notes: string) => Promise<void>;
};

const SCORE_FIELDS: Array<{ label: string; key: string }> = [
  { label: "Conviction", key: "conviction_score" },
  { label: "Asymmetry", key: "asymmetry_score" },
  { label: "Entry quality", key: "entry_quality_score" },
  { label: "Catalyst", key: "catalyst_score" },
  { label: "Evidence", key: "evidence_score" },
  { label: "Regime", key: "regime_score" },
  { label: "Survivability", key: "survivability_score" },
  { label: "Learning", key: "learning_score" },
];

export function OpportunityDetailDrawer({ opportunity, volSurface, onClose, onOpenTicker, onLogTrade }: OpportunityDetailDrawerProps) {
  return (
    <Sheet open={Boolean(opportunity)} onOpenChange={(open) => (open ? undefined : onClose())}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 overflow-y-auto p-0 sm:max-w-xl">
        {opportunity ? <DrawerBody opportunity={opportunity} volSurface={volSurface} onOpenTicker={onOpenTicker} onLogTrade={onLogTrade} /> : null}
      </SheetContent>
    </Sheet>
  );
}

function DrawerBody({ opportunity, volSurface, onOpenTicker, onLogTrade }: { opportunity: RowRecord; volSurface?: RowRecord; onOpenTicker: OpenTicker; onLogTrade?: (opportunity: RowRecord, notes: string) => Promise<void> }) {
  const ticker = textField(opportunity, ["ticker"]);
  const tier = textField(opportunity, ["tier"], "Watch");
  const state = textField(opportunity, ["primary_state"]).toUpperCase();
  const detail = parseRecord(opportunity["raw"], ["primary_detail"]);

  const strike = numberField(detail, ["strike"], Number.NaN);
  const premium = numberField(opportunity, ["premium_mid"], Number.NaN);
  const required10x = numberField(opportunity, ["required_10x_price"], Number.NaN);
  const requiredMove = numberField(opportunity, ["required_move_pct"], Number.NaN);
  const spot = Number.isFinite(required10x) && Number.isFinite(requiredMove) && requiredMove > -1 ? required10x / (1 + requiredMove) : Number.NaN;
  const evRaw = numberField(detail, ["ev_multiple"], Number.NaN);
  const evMultiple = Number.isFinite(evRaw) ? evRaw : null;
  const p2x = numberField(detail, ["p_2x"], Number.NaN);
  const p5x = numberField(detail, ["p_5x"], Number.NaN);
  const daysToEarnings = numberField(detail, ["days_to_earnings"], Number.NaN);

  const alternatives = parseArray(opportunity["alternative_contracts"]);
  const topReasons = listField(opportunity, ["top_reasons"]);
  const blockers = listField(opportunity, ["blockers"]);

  return (
    <>
      <SheetHeader className="sticky top-0 z-10 gap-2 border-b border-border bg-card px-5 py-4">
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => onOpenTicker(ticker)}
            className="inline-flex items-center gap-1 text-base font-semibold text-foreground hover:text-primary"
          >
            {ticker}
            <ArrowUpRight className="h-3.5 w-3.5" />
          </button>
          <StatusBadge tone={tierTone(tier)}>{tier}</StatusBadge>
          <StatusBadge tone={stateTone(state)}>{titleLabel(state || "watch")}</StatusBadge>
        </div>
        <SheetTitle className="text-sm font-medium text-muted-foreground">
          {displayField(detail, ["contract_id"], textField(opportunity, ["primary_contract_id"], "Contract"))}
        </SheetTitle>
        <SheetDescription>{textField(opportunity, ["why_now"], "No why-now summary for this opportunity.")}</SheetDescription>
      </SheetHeader>

      <div className="flex flex-col gap-5 px-5 py-5">
        <Section title="Asymmetry & catalyst" hint="EV-engine read for this contract">
          <div className="grid grid-cols-3 gap-2">
            <Metric label="EV multiple" value={evMultiple != null ? `${evMultiple.toFixed(2)}x` : "—"} />
            <Metric label="P(2x)" value={prob(p2x)} />
            <Metric label="P(5x)" value={prob(p5x)} />
            <Metric label="Required move" value={ratio(requiredMove)} />
            <Metric label="To catalyst" value={Number.isFinite(daysToEarnings) ? `${Math.round(daysToEarnings)}d` : "—"} />
            <Metric label="DTE" value={displayField(detail, ["dte"], "—")} />
          </div>
          {evMultiple == null ? (
            <p className="mt-2 text-[11px] text-muted-foreground">EV not yet priced for this contract — refresh the radar to populate it.</p>
          ) : null}
        </Section>

        <Section title="Conviction breakdown" hint="0–100 sub-scores feeding the tier">
          <div className="grid gap-2">
            {SCORE_FIELDS.map(({ label, key }) => (
              <ScoreBar key={key} label={label} value={numberField(opportunity, [key], Number.NaN)} />
            ))}
          </div>
        </Section>

        <Section title="IV term structure" hint="ATM implied vol across tenor">
          <IvTermSparkline volSurface={volSurface} />
        </Section>

        <Section title="Entry plan">
          <div className="grid grid-cols-2 gap-2">
            <Metric label="Premium (mid)" value={financial(premium)} />
            <Metric label="Buy under" value={financial(numberField(opportunity, ["buy_under"], Number.NaN))} />
            <Metric label="Entry zone" value={textField(opportunity, ["entry_zone"], "—")} />
            <Metric label="Position size" value={titleLabel(textField(opportunity, ["position_sizing_band"], "—"))} />
            <Metric label="Max loss" value={financial(numberField(opportunity, ["max_loss_assumption"], Number.NaN))} />
            <Metric label="Required move" value={ratio(requiredMove)} />
            <Metric label="10x price" value={financial(required10x)} />
            <Metric label="Spot (implied)" value={financial(spot)} />
          </div>
          <Callout label="Kill switch" tone="bad" value={textField(opportunity, ["kill_switch"], "No kill-switch defined.")} />
        </Section>

        {detail ? (
          <Section title="Contract">
            <div className="grid grid-cols-3 gap-2">
              <Metric label="Strike" value={financial(strike)} />
              <Metric label="Expiration" value={displayField(detail, ["expiration"], "—")} />
              <Metric label="DTE" value={displayField(detail, ["dte"], "—")} />
              <Metric label="Spread" value={ratio(numberField(detail, ["spread_pct"], Number.NaN))} />
              <Metric label="Open interest" value={integerLabel(numberField(detail, ["open_interest"], Number.NaN))} />
              <Metric label="Volume" value={integerLabel(numberField(detail, ["volume"], Number.NaN))} />
            </div>
          </Section>
        ) : null}

        <Section title="Why now">
          {topReasons.length ? (
            <ReasonRow reasons={topReasons} tone="good" />
          ) : (
            <p className="text-sm text-muted-foreground">No supporting reasons recorded.</p>
          )}
          {blockers.length ? (
            <div className="mt-2">
              <div className="mb-1 text-[10px] font-semibold uppercase text-muted-foreground">Blockers</div>
              <ReasonRow reasons={blockers} tone="warn" />
            </div>
          ) : null}
        </Section>

        {alternatives.length ? (
          <Section title="Alternative contracts" hint={`${alternatives.length} on the same ticker`}>
            <div className="grid gap-1.5">
              {alternatives.slice(0, 6).map((alt, index) => (
                <AlternativeRow key={textField(alt, ["contract_id"], String(index))} alt={alt} />
              ))}
            </div>
          </Section>
        ) : null}
      </div>

      {onLogTrade ? <LogTradeFooter opportunity={opportunity} onLogTrade={onLogTrade} /> : null}
    </>
  );
}

function LogTradeFooter({ opportunity, onLogTrade }: { opportunity: RowRecord; onLogTrade: (opportunity: RowRecord, notes: string) => Promise<void> }) {
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [logged, setLogged] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset the form whenever the drawer switches to a different opportunity.
  const key = textField(opportunity, ["opportunity_id"], textField(opportunity, ["primary_contract_id"]));
  useEffect(() => {
    setNotes("");
    setLogged(false);
    setError(null);
  }, [key]);

  async function submit() {
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await onLogTrade(opportunity, notes.trim());
      setLogged(true);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Failed to log trade.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="sticky bottom-0 z-10 mt-auto border-t border-border bg-card px-5 py-3">
      <textarea
        value={notes}
        onChange={(event) => setNotes(event.target.value)}
        placeholder="Entry notes (thesis, sizing, trigger)…"
        rows={2}
        disabled={submitting || logged}
        className="mb-2 w-full resize-none rounded-md border border-border bg-background px-2.5 py-1.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring disabled:opacity-60"
      />
      <div className="flex items-center gap-2">
        <Button size="sm" onClick={submit} disabled={submitting || logged}>
          {submitting ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <NotebookPen className="mr-1.5 h-3.5 w-3.5" />}
          {logged ? "Logged" : "Log trade"}
        </Button>
        {logged ? <span className="text-xs text-emerald-600 dark:text-emerald-400">Snapshot saved to the journal.</span> : null}
        {error ? <span className="text-xs text-rose-600 dark:text-rose-400">{error}</span> : null}
      </div>
    </div>
  );
}

// --- IV term sparkline ---------------------------------------------------------------

function IvTermSparkline({ volSurface }: { volSurface?: RowRecord }) {
  const points = useMemo(() => {
    if (!volSurface) return [] as Array<{ label: string; value: number }>;
    return [
      { label: "30d", value: numberField(volSurface, ["atm_iv_30d"], Number.NaN) },
      { label: "90d", value: numberField(volSurface, ["atm_iv_90d"], Number.NaN) },
      { label: "LEAP", value: numberField(volSurface, ["atm_iv_leap"], Number.NaN) },
    ].filter((point) => Number.isFinite(point.value));
  }, [volSurface]);

  if (points.length < 2) {
    return <EmptyChart label="No IV term structure for this ticker yet." />;
  }
  const values = points.map((point) => point.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const width = 320;
  const height = 64;
  const pad = 18;
  const xFor = (i: number) => pad + (i / (points.length - 1)) * (width - pad * 2);
  const yFor = (value: number) => 10 + (1 - (value - min) / span) * (height - 24);
  const path = points.map((point, i) => `${i === 0 ? "M" : "L"} ${xFor(i).toFixed(1)} ${yFor(point.value).toFixed(1)}`).join(" ");
  const slope = numberField(volSurface, ["term_slope"], Number.NaN);

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} className="h-16 w-full" role="img" aria-label="IV term structure">
        <path d={path} className={cn("fill-none", slope < 0 ? "stroke-rose-500" : "stroke-sky-500")} strokeWidth={1.5} />
        {points.map((point, i) => (
          <g key={point.label}>
            <circle cx={xFor(i)} cy={yFor(point.value)} r={2.5} className={cn(slope < 0 ? "fill-rose-500" : "fill-sky-500")} />
            <text x={xFor(i)} y={height - 2} textAnchor="middle" className="fill-muted-foreground text-[9px]">{point.label}</text>
            <text x={xFor(i)} y={yFor(point.value) - 5} textAnchor="middle" className="fill-foreground text-[9px]">{(point.value * 100).toFixed(0)}%</text>
          </g>
        ))}
      </svg>
      <div className="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-[10px] text-muted-foreground">
        <span>Term slope {ratio(slope)}</span>
        <span>IV/RV {decimal(numberField(volSurface, ["iv_rv_ratio"], Number.NaN))}</span>
        <span>IV %ile {decimal(numberField(volSurface, ["iv_percentile_252d"], Number.NaN))}</span>
      </div>
    </div>
  );
}

// --- Small building blocks -----------------------------------------------------------

function Section({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <section>
      <div className="mb-2 flex items-baseline justify-between">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-foreground">{title}</h3>
        {hint ? <span className="text-[10px] text-muted-foreground">{hint}</span> : null}
      </div>
      {children}
    </section>
  );
}

function ScoreBar({ label, value }: { label: string; value: number }) {
  const has = Number.isFinite(value);
  const pct = has ? Math.max(0, Math.min(100, value)) : 0;
  return (
    <div className="flex items-center gap-2">
      <span className="w-24 shrink-0 text-[11px] text-muted-foreground">{label}</span>
      <div className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
        <div className={cn("h-full rounded-full", scoreColor(pct))} style={{ width: `${pct}%` }} />
      </div>
      <span className="w-8 shrink-0 text-right text-[11px] tabular-nums text-foreground">{has ? Math.round(value) : "—"}</span>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-border/70 bg-background px-2.5 py-1.5">
      <div className="text-[10px] uppercase text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-sm tabular-nums text-foreground">{value}</div>
    </div>
  );
}

function Callout({ label, value, tone }: { label: string; value: string; tone: Tone }) {
  return (
    <div className="mt-2 rounded-md border border-border/70 bg-background px-3 py-2">
      <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
      <div className={cn("mt-0.5 text-sm leading-5", toneText(tone))}>{value}</div>
    </div>
  );
}

function ReasonRow({ reasons, tone }: { reasons: string[]; tone: Tone }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {reasons.map((reason) => (
        <span key={reason} className={cn("rounded-md border px-2 py-1 text-[11px] font-medium", reasonChipClass(tone))}>
          {humanize(reason)}
        </span>
      ))}
    </div>
  );
}

function AlternativeRow({ alt }: { alt: RowRecord }) {
  return (
    <div className="flex items-center justify-between rounded-md border border-border/70 bg-background px-2.5 py-1.5 text-xs">
      <span className="font-medium text-foreground">{textField(alt, ["contract_id"], "—")}</span>
      <span className="flex items-center gap-2 text-muted-foreground">
        <StatusBadge tone={stateTone(textField(alt, ["state"]).toUpperCase())}>{titleLabel(textField(alt, ["state"], "watch"))}</StatusBadge>
        <span className="tabular-nums">conv {Math.round(numberField(alt, ["conviction_score"], 0))}</span>
        <span className="tabular-nums">move {ratio(numberField(alt, ["required_move_pct"], Number.NaN))}</span>
      </span>
    </div>
  );
}

function EmptyChart({ label }: { label: string }) {
  return (
    <div className="flex h-20 items-center justify-center gap-2 rounded-md border border-dashed border-border text-xs text-muted-foreground">
      <Target className="h-3.5 w-3.5" />
      {label}
    </div>
  );
}

// --- helpers -------------------------------------------------------------------------

function parseRecord(value: JsonValue | undefined, keys: string[]): RowRecord | undefined {
  let current = coerceRecord(value);
  for (const key of keys) {
    if (!current) return undefined;
    current = coerceRecord(current[key]);
  }
  return current;
}

function coerceRecord(value: JsonValue | undefined): RowRecord | undefined {
  if (value && typeof value === "object" && !Array.isArray(value)) return value as RowRecord;
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed as RowRecord;
    } catch {
      return undefined;
    }
  }
  return undefined;
}

function parseArray(value: JsonValue | undefined): RowRecord[] {
  const source = Array.isArray(value) ? value : typeof value === "string" ? safeJsonArray(value) : null;
  if (!source) return [];
  const records: RowRecord[] = [];
  for (const item of source) {
    if (item && typeof item === "object" && !Array.isArray(item)) records.push(item as RowRecord);
  }
  return records;
}

function safeJsonArray(value: string): JsonValue[] | null {
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? (parsed as JsonValue[]) : null;
  } catch {
    return null;
  }
}

function financial(value: number): string {
  return Number.isFinite(value) ? formatMoney(value) : "—";
}

function ratio(value: number): string {
  return Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "—";
}

function prob(value: number): string {
  return Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "—";
}

function decimal(value: number): string {
  return Number.isFinite(value) ? value.toFixed(2) : "—";
}

function integerLabel(value: number): string {
  return Number.isFinite(value) ? Math.round(value).toLocaleString() : "—";
}

function humanize(reason: string): string {
  return reason.replace(/_/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function scoreColor(pct: number): string {
  if (pct >= 70) return "bg-emerald-500";
  if (pct >= 45) return "bg-amber-500";
  return "bg-rose-500";
}

function tierTone(tier: string): Tone {
  const value = tier.toLowerCase();
  if (value === "exceptional") return "good";
  if (value === "research") return "info";
  if (value.includes("bug") || value.includes("repair")) return "bad";
  return "muted";
}

function stateTone(state: string): Tone {
  if (state === "FIRE") return "good";
  if (state === "SETUP") return "info";
  if (state === "REJECT") return "bad";
  return "muted";
}

function toneText(tone: Tone): string {
  switch (tone) {
    case "good":
      return "text-emerald-600 dark:text-emerald-400";
    case "warn":
      return "text-amber-600 dark:text-amber-400";
    case "bad":
      return "text-rose-600 dark:text-rose-400";
    case "info":
      return "text-sky-600 dark:text-sky-400";
    default:
      return "text-muted-foreground";
  }
}

function reasonChipClass(tone: Tone): string {
  switch (tone) {
    case "good":
      return "border-emerald-500/30 bg-emerald-500/5 text-emerald-700 dark:text-emerald-300";
    case "warn":
      return "border-amber-500/30 bg-amber-500/5 text-amber-700 dark:text-amber-300";
    case "bad":
      return "border-rose-500/30 bg-rose-500/5 text-rose-700 dark:text-rose-300";
    default:
      return "border-border bg-muted text-muted-foreground";
  }
}
