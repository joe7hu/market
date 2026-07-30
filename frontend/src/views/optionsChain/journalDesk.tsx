import { BookOpenCheck, FlaskConical, Microscope } from "lucide-react";
import type { ReactNode } from "react";

import type { OptionsLearningProgress, OptionsPaperJournalRow } from "@/api";
import { StatusBadge } from "@/components/market/workstation";
import { sentence } from "./decisionDesk";

type Props = {
  journal: OptionsPaperJournalRow[];
  journalCount: number;
  shadow: OptionsPaperJournalRow[];
  shadowCount: number;
  learning: OptionsLearningProgress[];
};

export function JournalDesk({ journal, journalCount, shadow, shadowCount, learning }: Props) {
  const mature = journal.filter((row) => ["mature", "expired"].includes(row.lifecycle)).length;
  const missingMarks = journal.filter((row) => row.missing_mark_gap).length;
  return <div className="space-y-4">
    <section className="rounded-xl border border-border bg-card p-4 sm:p-5">
      <Heading icon={<BookOpenCheck className="size-4" />} title="Paper journal" detail="Only explicitly staged, thesis-backed PAPER_READY trades appear here." />
      <dl className="mt-4 grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-border bg-border md:grid-cols-4">
        <Summary label="Staged trades" value={journalCount} />
        <Summary label="Mature outcomes" value={mature} />
        <Summary label="Open / pending" value={Math.max(0, journalCount - mature)} />
        <Summary label="Missing marks" value={missingMarks} warning={missingMarks > 0} />
      </dl>
      {journal.length
        ? <div className="mt-3 divide-y divide-border">{journal.map((row) => <JournalRow key={row.paper_order_id ?? row.decision_id} row={row} />)}</div>
        : <Empty text="No paper trade has been explicitly staged. A setup must have a current thesis, pass every PAPER_READY gate, and be staged before it becomes part of this journal or its learning record." />}
    </section>

    <div className="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(20rem,0.65fr)]">
      <section className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 sm:p-5">
        <Heading icon={<Microscope className="size-4" />} title="Shadow observations" detail="Automated research experiments — not trades, fills, P&L, or a track record." />
        <div className="mt-3 flex flex-wrap items-baseline justify-between gap-2 border-b border-amber-500/20 pb-3 text-sm">
          <strong>{shadowCount.toLocaleString()} observations</strong>
          <span className="text-xs text-muted-foreground">Showing {shadow.length.toLocaleString()} newest · excluded from paper calibration</span>
        </div>
        {shadow.length
          ? <div className="divide-y divide-amber-500/20">{shadow.map((row) => <ShadowRow key={row.shadow_id ?? row.decision_id} row={row} />)}</div>
          : <Empty text="No unpromoted shadow observations exist." />}
      </section>

      <section className="rounded-xl border border-border bg-card p-4 sm:p-5">
        <Heading icon={<FlaskConical className="size-4" />} title="Paper learning" detail="Exact structure × regime × model cohorts, using staged paper trades only." />
        {learning.length
          ? <div className="mt-4 divide-y divide-border">{learning.map((row) => <LearningRow key={`${row.structure}-${row.market_regime}-${row.model_revision}`} row={row} />)}</div>
          : <Empty text="There is no paper cohort to learn from yet. Shadow returns are intentionally excluded. Promotion requires 30 mature staged outcomes, positive lower-95% expectancy, and Brier score at or below 0.25." />}
      </section>
    </div>
  </div>;
}

function JournalRow({ row }: { row: OptionsPaperJournalRow }) {
  return <article className="py-4 text-sm">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <strong>{sentence(row.structure ?? "paper trade")}</strong>
        <p className="mt-0.5 text-xs text-muted-foreground">{contract(row)} · staged {dateTime(row.execution.staged_at)}</p>
      </div>
      <StatusBadge tone={row.lifecycle === "mature" ? "good" : row.missing_mark_gap ? "warn" : "info"}>{row.lifecycle}</StatusBadge>
    </div>
    <p className="mt-3 leading-6">{row.thesis.core_thesis ?? "Thesis text unavailable for this historical revision."}</p>
    <p className="mt-1 text-xs text-amber-700 dark:text-amber-300"><strong>Invalidation:</strong> {row.thesis.invalidation ?? "Not recorded"}</p>
    <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-5">
      <Value label="Entry" value={money(row.execution.entry_price)} />
      <Value label="Latest mark" value={money(row.execution.latest_mark)} />
      <Value label="Return" value={percent(row.outcome.current_return)} />
      <Value label="Expected value" value={money(row.forecast.expected_value)} />
      <Value label="Max loss" value={money(row.forecast.max_loss)} />
    </dl>
    <AuditDetails row={row} />
  </article>;
}

function ShadowRow({ row }: { row: OptionsPaperJournalRow }) {
  return <article className="py-3 text-sm">
    <div className="grid gap-1 sm:grid-cols-[minmax(0,1fr)_auto]">
      <strong>{sentence(row.structure ?? "observation")} · {contract(row)}</strong>
      <StatusBadge tone="muted">{row.admission.paper_state ?? row.lifecycle}</StatusBadge>
      <span className="text-xs text-muted-foreground">{sentence(row.admission.discovery_lane ?? "research")} · {row.admission.market_regime ?? "regime unavailable"}</span>
      <span className="text-xs tabular-nums">{row.latest_mark === null ? "No mark" : `${money(row.latest_mark)} · observed ${percent(row.current_return)}`}</span>
    </div>
    <details className="mt-2 text-xs">
      <summary className="cursor-pointer text-muted-foreground hover:text-foreground">Why it was not a paper trade</summary>
      <div className="mt-2 grid gap-2 rounded border border-amber-500/20 bg-background/60 p-3 sm:grid-cols-2">
        <Audit label="Admission" value={`${row.admission.decision_state ?? "—"} / ${row.admission.paper_state ?? "—"}`} />
        <Audit label="Modeled case" value={`P(profit) ${percent(row.forecast.probability_profit)} · EV ${money(row.forecast.expected_value)} · max loss ${money(row.forecast.max_loss)}`} />
        <Audit label="Blockers" value={row.admission.blockers.length ? row.admission.blockers.map(sentence).join(" · ") : "No explicit blockers recorded"} />
        <Audit label="Research basis" value={`${row.forecast.scenario_count} scenarios · data confidence ${percent(row.forecast.data_confidence)}`} />
      </div>
    </details>
  </article>;
}

function AuditDetails({ row }: { row: OptionsPaperJournalRow }) {
  const attribution = row.outcome.attribution;
  return <details className="mt-3 text-xs">
    <summary className="cursor-pointer font-medium text-muted-foreground hover:text-foreground">Full decision and outcome audit</summary>
    <div className="mt-2 grid gap-3 rounded-lg border border-border bg-muted/30 p-3 md:grid-cols-3">
      <Audit label="Admission" value={`${row.admission.discovery_lane ?? "—"} · ${row.admission.paper_state ?? "—"} · ${row.admission.model_revision ?? "—"}`} />
      <Audit label="Forecast quality" value={`${row.forecast.scenario_count} scenarios · lower-95 EV ${money(row.forecast.lower_95_expected_value)} · execution confidence ${percent(row.forecast.execution_confidence)}`} />
      <Audit label="Execution" value={`${row.execution.fill_basis ?? "Fill basis unavailable"} · held ${hours(row.execution.holding_period_hours)}`} />
      <Audit label="Path" value={`1d ${percent(row.outcome.return_1d)} · 5d ${percent(row.outcome.return_5d)} · 20d ${percent(row.outcome.return_20d)} · 60d ${percent(row.outcome.return_60d)}`} />
      <Audit label="Excursion" value={`Peak ${percent(row.outcome.peak_return)} · drawdown ${percent(row.outcome.max_drawdown)}`} />
      <Audit label="Attribution" value={`spot ${percent(attribution.underlying)} · IV ${percent(attribution.iv)} · theta ${percent(attribution.theta)} · spread ${percent(attribution.spread)}`} />
    </div>
  </details>;
}

function LearningRow({ row }: { row: OptionsLearningProgress }) {
  return <article className="py-3 text-sm">
    <div className="flex justify-between gap-3"><strong>{sentence(row.structure)}</strong><span className="tabular-nums">{row.mature_outcomes}/{row.required_mature_outcomes}</span></div>
    <p className="mt-1 text-xs text-muted-foreground">{row.market_regime ?? "Regime unavailable"} · lower 95% {percent(row.lower_95_expectancy)} · Brier {decimal(row.brier_score)}</p>
    {row.missing_prerequisites.length ? <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">{row.missing_prerequisites.map(sentence).join(" · ")}</p> : <p className="mt-1 text-xs text-emerald-700 dark:text-emerald-300">Calibration gate passed.</p>}
  </article>;
}

function Heading({ icon, title, detail }: { icon: ReactNode; title: string; detail: string }) { return <div className="flex items-start gap-3"><span className="mt-0.5 text-primary">{icon}</span><span><h2 className="font-semibold">{title}</h2><p className="mt-0.5 text-xs leading-5 text-muted-foreground">{detail}</p></span></div>; }
function Summary({ label, value, warning = false }: { label: string; value: number; warning?: boolean }) { return <div className="bg-card p-3"><dt className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</dt><dd className={`mt-1 text-xl font-semibold tabular-nums ${warning ? "text-amber-700 dark:text-amber-300" : ""}`}>{value.toLocaleString()}</dd></div>; }
function Value({ label, value }: { label: string; value: string }) { return <div><dt className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</dt><dd className="mt-0.5 font-medium tabular-nums">{value}</dd></div>; }
function Audit({ label, value }: { label: string; value: string }) { return <div><strong className="block text-[10px] uppercase tracking-wide text-muted-foreground">{label}</strong><span className="mt-1 block leading-5">{value}</span></div>; }
function Empty({ text }: { text: string }) { return <p className="mt-4 rounded-lg border border-dashed border-border p-5 text-sm leading-6 text-muted-foreground">{text}</p>; }
function contract(row: OptionsPaperJournalRow) { return `${row.contract.expiration ?? "No expiry"} · ${(row.contract.option_type ?? "option").toUpperCase()} ${money(row.contract.strike)}`; }
function money(value: number | null | undefined) { return value === null || value === undefined ? "—" : value.toLocaleString(undefined, { style: "currency", currency: "USD" }); }
function percent(value: number | null | undefined) { return value === null || value === undefined ? "—" : value.toLocaleString(undefined, { style: "percent", maximumFractionDigits: 1 }); }
function decimal(value: number | null | undefined) { return value === null || value === undefined ? "—" : value.toFixed(3); }
function hours(value: number | null) { return value === null ? "pending" : `${value.toLocaleString(undefined, { maximumFractionDigits: 1 })}h`; }
function dateTime(value: string | null) { return value ? new Date(value).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "time unavailable"; }
