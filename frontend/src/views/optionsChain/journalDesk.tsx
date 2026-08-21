import { ArrowRight, BookOpenCheck, FlaskConical, Microscope } from "lucide-react";
import type { ReactNode } from "react";

import type { OptionsDecisionBrief, OptionsLearningProgress, OptionsPaperJournalRow } from "@/api/options";
import { StatusBadge } from "@/components/market/workstation";
import { sentence } from "./decisionDesk";
import { buildJournalDeskModel, observationLabel, researchBlockerLabel } from "./journalDeskModel";

type Props = {
  brief: OptionsDecisionBrief | null;
  journal: OptionsPaperJournalRow[];
  journalCount: number;
  shadow: OptionsPaperJournalRow[];
  shadowCount: number;
  legacyShadowCount: number;
  learning: OptionsLearningProgress[];
};

export function JournalDesk({ brief, journal, journalCount, shadow, shadowCount, legacyShadowCount, learning }: Props) {
  const model = buildJournalDeskModel({ journal, journalCount, shadow, shadowCount, legacyShadowCount });
  return <div className="space-y-4">
    <section className="grid overflow-hidden rounded-xl border border-sky-500/25 bg-[linear-gradient(135deg,hsl(var(--card)),hsl(var(--muted)/0.35))] lg:grid-cols-[minmax(0,1fr)_minmax(18rem,0.55fr)]">
      <div className="p-5 sm:p-6">
        <p className="text-xs font-semibold uppercase tracking-[0.18em] text-sky-700 dark:text-sky-300">Learning state</p>
        <h2 className="mt-2 text-xl font-semibold tracking-tight sm:text-2xl">{model.paperStatus}</h2>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">{currentBenefit(brief, model.currentExperiments)}</p>
        <p className="mt-4 text-xs leading-5 text-muted-foreground">
          Forecast → staged paper entry → marked outcome → exact-cohort proof. Research observations test mechanics; only staged paper trades count as performance.
        </p>
      </div>
      <div className="border-t border-sky-500/20 bg-sky-500/[0.06] p-5 lg:border-l lg:border-t-0">
        <p className="text-xs font-semibold uppercase tracking-[0.16em] text-muted-foreground">Next decision change</p>
        <div className="mt-3 flex items-start gap-2 text-sm leading-6">
          <ArrowRight className="mt-1 size-4 shrink-0 text-sky-600" />
          <strong>{nextMilestone(brief, journalCount)}</strong>
        </div>
        <p className="mt-3 text-xs leading-5 text-muted-foreground">
          Promotion remains locked until one exact cohort reaches 30 mature outcomes with positive lower-95% expectancy and Brier ≤ 0.25.
        </p>
      </div>
    </section>

    {journal.length ? <section className="rounded-xl border border-border bg-card p-4 sm:p-5">
      <Heading icon={<BookOpenCheck className="size-4" />} title="Paper track record" detail="Only trades you explicitly stage after every PAPER_READY gate count as performance evidence." />
      <dl className="mt-4 grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-border bg-border md:grid-cols-4">
        <Summary label="Staged trades" value={journalCount} />
        <Summary label="Mature outcomes" value={model.maturePaperOutcomes} />
        <Summary label="Open / pending" value={model.openPaperTrades} />
        <Summary label="Missing marks" value={model.missingPaperMarks} warning={model.missingPaperMarks > 0} />
      </dl>
      <div className="mt-3 divide-y divide-border">{journal.map((row) => <JournalRow key={row.paper_order_id ?? row.decision_id} row={row} />)}</div>
    </section> : null}

    <div className={learning.length ? "grid gap-4 xl:grid-cols-[minmax(0,1.1fr)_minmax(20rem,0.9fr)]" : ""}>
      <section className="rounded-xl border border-sky-500/25 bg-sky-500/[0.04] p-4 sm:p-5">
        <Heading icon={<Microscope className="size-4" />} title="Research mechanics" detail="A compact health check for forecast marking—not a trade list or performance record." />
        <dl className="mt-4 grid grid-cols-3 gap-px overflow-hidden rounded-lg border border-sky-500/20 bg-sky-500/20">
          <Summary label="Observed" value={model.currentExperiments} />
          <Summary label="Marked" value={model.marked} warning={model.currentExperiments > 0 && model.marked < model.currentExperiments} />
          <Summary label="Awaiting quote" value={model.awaitingEntry} />
        </dl>
        {model.visibleExperiments.length
          ? <details className="mt-4 rounded-lg border border-sky-500/20 bg-background/60 p-3">
            <summary className="cursor-pointer text-sm font-medium">Inspect 5 latest research cases</summary>
            <div className="mt-2 divide-y divide-sky-500/15">{model.visibleExperiments.map((row) => <ShadowRow key={row.shadow_id ?? row.decision_id} row={row} />)}</div>
          </details>
          : <Empty text="No current-model research experiment is active. The system will add one only when a candidate is worth tracking." />}
      </section>

      {learning.length ? <section className="rounded-xl border border-border bg-card p-4 sm:p-5">
        <Heading icon={<FlaskConical className="size-4" />} title="Validated cohorts" detail="Exact structure × regime × model groups built from staged paper trades only." />
        <div className="mt-4 divide-y divide-border">{learning.map((row) => <LearningRow key={`${row.structure}-${row.market_regime}-${row.model_revision}`} row={row} />)}</div>
      </section> : null}
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
      <StatusBadge tone={["entered", "observing"].includes(row.lifecycle) ? "info" : "muted"}>{observationLabel(row)}</StatusBadge>
      <span className="text-xs text-muted-foreground">{sentence(row.admission.discovery_lane ?? "research")} · {row.admission.market_regime ?? "regime unavailable"} · {dateTime(row.admission.decision_at)}</span>
      <span className="text-xs tabular-nums">{row.latest_mark === null ? "No mark" : `${money(row.latest_mark)} · observed ${percent(row.current_return)}`}</span>
    </div>
    <details className="mt-2 text-xs">
      <summary className="cursor-pointer text-muted-foreground hover:text-foreground">Research case and gate</summary>
      <div className="mt-2 grid gap-2 rounded border border-amber-500/20 bg-background/60 p-3 sm:grid-cols-2">
        <Audit label="Research status" value={`${observationLabel(row)} · ${sentence(row.admission.discovery_lane ?? "research")}`} />
        <Audit label="Modeled case" value={`P(profit) ${percent(row.forecast.probability_profit)} · EV ${money(row.forecast.expected_value)} · max loss ${money(row.forecast.max_loss)}`} />
        <Audit label="Why no paper trade" value={row.admission.blockers.length ? row.admission.blockers.map(researchBlockerLabel).join(" · ") : "Research-only observation"} />
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
function hours(value: number | null | undefined) { return value === null || value === undefined ? "pending" : `${value.toLocaleString(undefined, { maximumFractionDigits: 1 })}h`; }
function dateTime(value: string | null | undefined) { return value ? new Date(value).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "time unavailable"; }
function currentBenefit(brief: OptionsDecisionBrief | null, experiments: number) {
  if (!brief) return "Loading the current underwriting state.";
  if (!brief.readiness.thesis.eligible) {
    return `${experiments.toLocaleString()} current experiment${experiments === 1 ? "" : "s"} may test mechanics, but the neutral thesis correctly prevents a directional paper trade.`;
  }
  return experiments
    ? `${experiments.toLocaleString()} current experiment${experiments === 1 ? "" : "s"} are testing the path from forecast to observable outcome.`
    : "The log is waiting for a candidate worth tracking.";
}
function nextMilestone(brief: OptionsDecisionBrief | null, journalCount: number) {
  if (!brief) return "Load the current decision gates.";
  if (brief.readiness.thesis.blocker === "thesis_direction_required") return "Independent evidence turns the automatic QQQ thesis directional.";
  if (brief.readiness.canary.qualified_regular_sessions < brief.readiness.canary.required_regular_sessions) {
    const remaining = brief.readiness.canary.required_regular_sessions - brief.readiness.canary.qualified_regular_sessions;
    return `${remaining} more qualified reliability session${remaining === 1 ? "" : "s"}.`;
  }
  if (!journalCount) return "A PAPER_READY setup is explicitly staged, creating the first honest paper record.";
  return "Build 30 mature outcomes in an exact cohort before trusting the modeled edge.";
}
