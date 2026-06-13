// Shared cross-section mini-components for the options radar view.

import {ReactNode } from "react";
import {AlertTriangle } from "lucide-react";
import {StatusBadge } from "@/components/market/workstation";
import {Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import {cn } from "@/lib/utils";
import {RowRecord } from "@/types";
import {Tone } from "@/ui/tone";
import {formatMoney, numberField, textField, titleLabel } from "../rowFormat";
import {formatSignedRatio, formatMultiple } from "../optionsRadarFormat";
import {recordField, listFromRecord, numberFromRecord } from "../optionsRadarData";
import {attributionTone, verdictTone, toneText, reasonLabel } from "../optionsRadarTone";
import {outcomeMaturity } from "./helpers";

export function BriefCallout({ label, value, tone }: { label: string; value: string; tone: Tone }) {
  return (
    <div className="rounded-md border border-border/70 bg-background px-3 py-2">
      <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
      <div className={cn("mt-1 text-sm leading-5", toneText(tone))}>{value}</div>
    </div>
  );
}

export function ReadableReasonGroup({ label, reasons, tone }: { label: string; reasons: string[]; tone: Tone }) {
  return (
    <div>
      <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
      <div className="mt-1 flex flex-wrap gap-1.5">
        {reasons.map((reason) => <ReasonChip key={`${label}-${reason}`} reason={reason} tone={tone} />)}
      </div>
    </div>
  );
}

export function InlineMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md bg-muted px-2 py-1.5">
      <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-sm font-semibold tabular-nums">{value}</div>
    </div>
  );
}

export function MobileSection({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <div className="mb-1 text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
      <div className="text-sm leading-5">{children}</div>
    </div>
  );
}

export function QualityIndicator({ status, flags }: { status: string; flags: string[] }) {
  if (status === "ok" || !flags.length) return null;
  const title = flags.map(titleLabel).join(", ");
  return (
    <span
      className={cn(
        "inline-flex h-5 w-5 items-center justify-center rounded-full",
        status === "bad" ? "text-destructive" : "text-amber-600",
      )}
      title={title}
      aria-label={title}
    >
      <AlertTriangle className="h-4 w-4" aria-hidden="true" />
    </span>
  );
}

export function HelpLabel({ label, detail }: { label: string; detail: string }) {
  return (
    <TooltipProvider delayDuration={150}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="inline-flex cursor-help items-center justify-end gap-1 underline decoration-dotted underline-offset-4">{label}</span>
        </TooltipTrigger>
        <TooltipContent className="max-w-80 text-xs leading-5">{detail}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export function FillTarget({ row }: { row: RowRecord }) {
  const strike = numberFromRecord(recordField(row, "raw"), "strike");
  const fill = numberField(row, ["premium_fill_assumption"], Number.NaN);
  const midTarget = numberField(row, ["required_10x_price"], Number.NaN);
  if (!Number.isFinite(strike) || !Number.isFinite(fill)) return null;
  const fillTarget = strike + fill * 10;
  if (!Number.isFinite(fillTarget) || Math.abs(fillTarget - midTarget) < 0.01) return null;
  return <div className="text-xs text-muted-foreground">fill {formatMoney(fillTarget)}</div>;
}

export function PremiumCapHint({ row }: { row: RowRecord }) {
  const cap = numberField(row, ["buy_under"], Number.NaN);
  const fill = numberField(row, ["premium_fill_assumption"], Number.NaN);
  if (!Number.isFinite(cap) || !Number.isFinite(fill)) return null;
  const room = cap - fill;
  if (!Number.isFinite(room)) return null;
  return (
    <div className={cn("text-xs", room >= 0 ? "text-muted-foreground" : "text-destructive")}>
      {room >= 0 ? `cap room ${formatMoney(room)}` : `over cap ${formatMoney(Math.abs(room))}`}
    </div>
  );
}

export function OpportunityOutcome({ mark, attribution }: { mark: RowRecord | undefined; attribution: RowRecord | undefined }) {
  if (!mark) {
    return (
      <div className="min-w-0">
        <StatusBadge tone="muted">Waiting for next price</StatusBadge>
        <div className="mt-1 text-xs text-muted-foreground">No later option snapshot yet</div>
      </div>
    );
  }
  const maturity = outcomeMaturity(mark);
  const currentReturn = numberField(mark, ["current_return"], Number.NaN);
  const maxReturn = numberField(mark, ["max_return_since_alert"], Number.NaN);
  const label = textField(attribution, ["label"]);
  return (
    <div className="min-w-0">
      <div className="flex flex-wrap items-center gap-1.5">
        <StatusBadge tone={maturity.tone}>{maturity.label}</StatusBadge>
        {label ? <StatusBadge tone={attributionTone(label)}>{titleLabel(label)}</StatusBadge> : null}
      </div>
      <div className="mt-1 truncate text-xs text-muted-foreground">
        Now {formatSignedRatio(currentReturn)} / max {formatMultiple(maxReturn)}
      </div>
    </div>
  );
}

export function ReasonChips({ row }: { row: RowRecord }) {
  const raw = recordField(row, "raw");
  const hardRejects = listFromRecord(raw, "hard_rejects");
  const blockers = listFromRecord(raw, "blockers");
  const positives = listFromRecord(raw, "positives");
  const negativeReasons = [...hardRejects, ...blockers];
  const visibleReasons = negativeReasons.length ? negativeReasons.slice(0, 3) : positives.slice(0, 3);
  const extraCount = (negativeReasons.length ? negativeReasons.length : positives.length) - visibleReasons.length;
  return (
    <div className="flex flex-wrap gap-1.5">
      {visibleReasons.length ? visibleReasons.map((reason) => (
        <ReasonChip key={reason} reason={reason} tone={negativeReasons.includes(reason) ? "warn" : "good"} />
      )) : <StatusBadge tone="good">Core gates passed</StatusBadge>}
      {extraCount > 0 ? <span className="rounded-md border border-border px-2 py-0.5 text-xs text-muted-foreground">+{extraCount}</span> : null}
    </div>
  );
}

export function ReasonChip({ reason, tone }: { reason: string; tone: Tone }) {
  return (
    <span
      className={cn(
        "rounded-md border px-2 py-0.5 text-xs font-medium",
        tone === "good" && "border-green-500/30 bg-green-50/30 text-foreground dark:bg-green-950/20",
        tone === "bad" && "border-destructive/30 bg-destructive/5 text-destructive",
        tone !== "good" && tone !== "bad" && "border-amber-500/30 bg-amber-50/40 text-foreground dark:bg-amber-950/20",
      )}
      title={reason}
    >
      {reasonLabel(reason)}
    </span>
  );
}

export function GatePill({ label, row, keys, detail }: { label: string; row: RowRecord | undefined; keys: string[]; detail: string }) {
  const verdict = row ? textField(row, keys, "pending") : "pending";
  return (
    <div className="rounded-md bg-muted px-2 py-2">
      <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
      <div className={cn("mt-1 text-sm font-semibold", toneText(verdictTone(verdict)))}>{titleLabel(verdict)}</div>
      <div className="mt-1 text-xs text-muted-foreground">{detail}</div>
    </div>
  );
}

export function InsightLine({ label, value, className }: { label: string; value: string; className?: string }) {
  if (!value) return null;
  return (
    <div className={cn("text-sm leading-6", className)}>
      <span className="font-semibold text-foreground">{label}: </span>
      <span className="text-muted-foreground">{value}</span>
    </div>
  );
}

export function BrowserStat({ label, value, tone }: { label: string; value: number; tone: Tone }) {
  return (
    <div className="border-l border-border pl-3">
      <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
      <div className={cn("mt-1 text-sm font-semibold tabular-nums", toneText(tone))}>{value.toLocaleString()}</div>
    </div>
  );
}

export function MetricBox({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-border/70 bg-background px-3 py-2">
      <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
      <div className="mt-1 truncate text-sm font-semibold tabular-nums">{value}</div>
    </div>
  );
}

export function ReadableSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="min-w-0">
      <h3 className="text-xs font-semibold uppercase text-muted-foreground">{title}</h3>
      <div className="mt-2">{children}</div>
    </section>
  );
}

export function ReadableList({ items, empty }: { items: string[]; empty: string }) {
  if (!items.length) return <p className="text-sm text-muted-foreground">{empty}</p>;
  return (
    <ul className="space-y-2">
      {items.map((item, index) => (
        <li key={`${index}-${item.slice(0, 32)}`} className="grid grid-cols-[18px_minmax(0,1fr)] gap-2 text-sm leading-6">
          <span className="mt-2 size-1.5 rounded-full bg-muted-foreground" />
          <span className="whitespace-pre-wrap">{item}</span>
        </li>
      ))}
    </ul>
  );
}

export function VerdictBadge({ row, keys }: { row: RowRecord | undefined; keys: string[] }) {
  if (!row) return <StatusBadge tone="muted">Pending</StatusBadge>;
  const verdict = textField(row, keys, "pending");
  return <StatusBadge tone={verdictTone(verdict)}>{titleLabel(verdict)}</StatusBadge>;
}

