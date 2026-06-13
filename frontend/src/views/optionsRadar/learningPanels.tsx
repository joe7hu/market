// Learning, cohort, missed-winner, and postmortem panels.

import {Activity, BrainCircuit, TrendingUp } from "lucide-react";
import {DataTableFrame, EmptyState, StatusBadge } from "@/components/market/workstation";
import {cn } from "@/lib/utils";
import {RowRecord } from "@/types";
import {Tone } from "@/ui/tone";
import {displayField, numberField, textField, titleLabel, toneFromText } from "../rowFormat";
import {moneyField, formatRatio, formatMultiple, formatScore, formatNumber, formatDate } from "../optionsRadarFormat";
import {jsonArrayField } from "../optionsRadarData";
import {toneText } from "../optionsRadarTone";
import {Cell, Head, MetricPill, SectionTitle, TickerButton, Truncated } from "../optionsRadarPrimitives";
import {OpenTicker } from "../workspacePage";
import {postmortemImpact, cohortKey, cohortObservationStats, formatObservedWindow, countWhere, cohortHasMatureEvidence, cohortDefinition } from "./helpers";
import {InsightLine } from "./shared";

export function MissedWinnersTable({ rows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: OpenTicker }) {
  if (!rows.length) {
    return <EmptyState title="No missed winners" detail="No unalerted 5x or 10x contracts are stored." icon={TrendingUp} />;
  }

  return (
    <DataTableFrame title={<SectionTitle title="Missed Winners" count={rows.length} />}>
      <table className="w-full min-w-[1160px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head>Threshold</Head>
            <Head className="text-right">Max Return</Head>
            <Head>Filter Reason</Head>
            <Head>Strategy Family</Head>
            <Head className="text-right">Entry</Head>
            <Head className="text-right">Winner</Head>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 80).map((row) => {
            const ticker = textField(row, ["ticker"]);
            return (
              <tr key={textField(row, ["missed_id"], `${ticker}-${textField(row, ["contract_id"])}`)} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                <Cell>{ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : "-"}</Cell>
                <Cell><StatusBadge tone={textField(row, ["winner_threshold"]).toLowerCase() === "10x" ? "bad" : "warn"}>{displayField(row, ["winner_threshold"])}</StatusBadge></Cell>
                <Cell className="text-right tabular-nums">{formatMultiple(numberField(row, ["max_return_seen"], Number.NaN))}</Cell>
                <Cell className="max-w-[360px]"><Truncated>{displayField(row, ["filter_reason"])}</Truncated></Cell>
                <Cell className="max-w-[240px]"><Truncated>{displayField(row, ["proposed_strategy_family"])}</Truncated></Cell>
                <Cell className="text-right tabular-nums">{moneyField(row, ["entry_price_assumption"])}</Cell>
                <Cell className="text-right tabular-nums">{moneyField(row, ["winner_price"])}</Cell>
              </tr>
            );
          })}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

export function LearningProgressPanel({
  opportunities,
  latestMarkByEvent,
  latestAttributionByEvent,
  cohorts,
  proposals,
  missedWinners,
  postmortemRequests,
  postmortems,
}: {
  opportunities: RowRecord[];
  latestMarkByEvent: Map<string, RowRecord>;
  latestAttributionByEvent: Map<string, RowRecord>;
  cohorts: RowRecord[];
  proposals: RowRecord[];
  missedWinners: RowRecord[];
  postmortemRequests: RowRecord[];
  postmortems: RowRecord[];
}) {
  const currentMarks = opportunities.map((row) => latestMarkByEvent.get(textField(row, ["event_id"]))).filter(Boolean) as RowRecord[];
  const oneDay = countWhere(currentMarks, (row) => Number.isFinite(numberField(row, ["return_1d"], Number.NaN)));
  const fiveDay = countWhere(currentMarks, (row) => Number.isFinite(numberField(row, ["return_5d"], Number.NaN)));
  const attributed = countWhere(opportunities, (row) => latestAttributionByEvent.has(textField(row, ["event_id"])));
  const matureCohorts = cohorts.filter(cohortHasMatureEvidence).length;
  const openPostmortems = countWhere(postmortemRequests, (row) => textField(row, ["status"], "open").toLowerCase() === "open");
  const readyProposals = countWhere(proposals, (row) => textField(row, ["status"]).toLowerCase() === "ready_for_human_review");
  const items: Array<[string, string, string, Tone]> = [
    ["Current outcomes", `${oneDay}/${opportunities.length}`, "current opportunities with at least 1D observed", oneDay ? "good" : "warn"],
    ["5D outcomes", `${fiveDay}/${opportunities.length}`, "current opportunities with a 5D read", fiveDay ? "good" : "muted"],
    ["Attribution", `${attributed}/${opportunities.length}`, "current opportunities with an explained move", attributed ? "good" : "muted"],
    ["Cohorts ready", `${matureCohorts}/${cohorts.length}`, "cohorts with enough post-entry evidence to interpret", matureCohorts ? "good" : "warn"],
    ["Missed winners", missedWinners.length.toLocaleString(), "unalerted 5x/10x contracts found", missedWinners.length ? "warn" : "muted"],
    ["Strategy changes", readyProposals.toLocaleString(), "proposals through deterministic gates", readyProposals ? "good" : proposals.length ? "warn" : "muted"],
    ["Postmortems", postmortems.length.toLocaleString(), "completed agent reviews of important outcomes", postmortems.length ? "good" : openPostmortems ? "warn" : "muted"],
  ];
  return (
    <section className="rounded-md border border-border bg-card p-4">
      <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
        <div>
          <h2 className="text-base font-semibold">Learning Progress</h2>
          <p className="mt-1 max-w-4xl text-sm leading-6 text-muted-foreground">
            Learning becomes actionable only after candidates have post-entry outcome windows. Current cohort hit rates are withheld until the cohort has mature evidence, so pending contracts are not shown as failures.
          </p>
        </div>
        <StatusBadge tone={matureCohorts ? "good" : "warn"}>{matureCohorts ? "Evidence available" : "Collecting outcomes"}</StatusBadge>
      </div>
      <div className="mt-4 grid gap-2 md:grid-cols-2 xl:grid-cols-4">
        {items.map(([label, value, detail, tone]) => (
          <div key={label} className="rounded-md border border-border/70 bg-background px-3 py-2">
            <div className="text-[10px] font-semibold uppercase text-muted-foreground">{label}</div>
            <div className={cn("mt-1 text-sm font-semibold tabular-nums", toneText(tone))}>{value}</div>
            <div className="mt-1 text-xs text-muted-foreground">{detail}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

export function CohortResultsTable({ rows }: { rows: RowRecord[] }) {
  if (!rows.length) {
    return <EmptyState title="No cohort results" detail="No deterministic setup cohorts have enough shadow outcomes yet." icon={Activity} />;
  }

  const matureRows = rows.filter(cohortHasMatureEvidence);
  const collectingRows = rows.filter((row) => !cohortHasMatureEvidence(row));
  const topCollectingRows = collectingRows
    .slice()
    .sort((left, right) => numberField(right, ["candidate_count"], 0) - numberField(left, ["candidate_count"], 0))
    .slice(0, 6);

  return (
    <section className="rounded-md border border-border bg-card p-4">
      <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
        <div>
          <h2 className="text-base font-semibold">Cohort Learning</h2>
          <p className="mt-1 max-w-4xl text-sm leading-6 text-muted-foreground">
            Cohorts are grouped by setup, IV, liquidity, move burden, and market regime. Hit-rate and drawdown numbers are shown only after the outcome window is mature.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <StatusBadge tone={matureRows.length ? "good" : "warn"}>{matureRows.length ? `${matureRows.length} readable` : "Collecting outcomes"}</StatusBadge>
          <StatusBadge tone="muted">{rows.length.toLocaleString()} cohorts</StatusBadge>
        </div>
      </div>

      {!matureRows.length ? (
        <div className="mt-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-3 text-sm leading-6 text-amber-950 dark:border-amber-900/60 dark:bg-amber-950/20 dark:text-amber-100">
          No cohort has a mature post-entry window yet. Current rows are useful for coverage only; zero hit rates here mean "not observed long enough," not "strategy failed."
        </div>
      ) : (
        <div className="mt-4 grid gap-3 lg:grid-cols-2">
          {matureRows.slice(0, 6).map((row) => (
            <CohortInsightCard key={cohortKey(row)} row={row} mature />
          ))}
        </div>
      )}

      {topCollectingRows.length ? (
        <div className="mt-4">
          <div className="mb-2 text-xs font-semibold uppercase text-muted-foreground">Largest Collecting Cohorts</div>
          <div className="grid gap-2 lg:grid-cols-3">
            {topCollectingRows.map((row) => (
              <CohortInsightCard key={cohortKey(row)} row={row} mature={false} />
            ))}
          </div>
        </div>
      ) : null}
    </section>
  );
}

export function CohortInsightCard({ row, mature }: { row: RowRecord; mature: boolean }) {
  const stats = cohortObservationStats(row);
  return (
    <article className="rounded-md border border-border/70 bg-background p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold">{titleLabel(displayField(row, ["cohort_value"]))}</div>
          <div className="mt-0.5 text-xs text-muted-foreground">{titleLabel(displayField(row, ["cohort_type"]))}</div>
        </div>
        <StatusBadge tone={mature ? "good" : "warn"}>{mature ? "Readable" : "Collecting"}</StatusBadge>
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2">
        <MetricPill label="Signals" value={formatNumber(numberField(row, ["candidate_count"], Number.NaN), 0)} />
        <MetricPill label="Observed" value={formatObservedWindow(stats.maxObservationDays)} />
        <MetricPill label="Sample" value={stats.sampleCount ? stats.sampleCount.toLocaleString() : "-"} />
      </div>
      {mature ? (
        <div className="mt-3 grid grid-cols-3 gap-2">
          <MetricPill label="2x" value={formatRatio(numberField(row, ["hit_rate_2x"], Number.NaN))} />
          <MetricPill label="5x" value={formatRatio(numberField(row, ["hit_rate_5x"], Number.NaN))} />
          <MetricPill label="Max" value={formatMultiple(numberField(row, ["median_max_return"], Number.NaN))} />
        </div>
      ) : null}
      <p className="mt-3 text-sm leading-6 text-muted-foreground">{mature ? cohortDefinition(row) : "Waiting for at least one trading-day outcome before judging hit rate or drawdown."}</p>
    </article>
  );
}

export function PostmortemRequestsTable({ rows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: OpenTicker }) {
  if (!rows.length) {
    return <EmptyState title="No postmortem requests" detail="No important outcomes are queued for agent postmortem." icon={BrainCircuit} />;
  }

  return (
    <DataTableFrame title={<SectionTitle title="Premarket Review Queue" count={rows.length} />}>
      <table className="w-full min-w-[880px] text-sm">
        <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            <Head>Ticker</Head>
            <Head>Status</Head>
            <Head>Outcome</Head>
            <Head className="text-right">Priority</Head>
            <Head>Created</Head>
            <Head>Decision Impact</Head>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 80).map((row) => {
            const ticker = textField(row, ["ticker"]);
            const status = textField(row, ["status"], "open");
            return (
              <tr key={textField(row, ["request_id"], `${ticker}-${textField(row, ["source_id"])}`)} className="border-b border-border align-top transition-colors hover:bg-accent/40">
                <Cell>{ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : "-"}</Cell>
                <Cell><StatusBadge tone={toneFromText(status)}>{titleLabel(status)}</StatusBadge></Cell>
                <Cell className="max-w-[220px]"><Truncated>{displayField(row, ["source_type"])}</Truncated></Cell>
                <Cell className="text-right tabular-nums">{formatScore(numberField(row, ["priority_score"], Number.NaN))}</Cell>
                <Cell className="whitespace-nowrap text-muted-foreground">{formatDate(textField(row, ["created_at"]))}</Cell>
                <Cell className="max-w-[360px]"><Truncated>{postmortemImpact(row)}</Truncated></Cell>
              </tr>
            );
          })}
        </tbody>
      </table>
    </DataTableFrame>
  );
}

export function PostmortemsTable({ rows, onOpenTicker }: { rows: RowRecord[]; onOpenTicker: OpenTicker }) {
  if (!rows.length) {
    return <EmptyState title="No postmortems" detail="No structured agent postmortems are stored." icon={BrainCircuit} />;
  }

  return (
    <section className="rounded-md border border-border bg-card p-4">
      <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
        <div>
          <h2 className="text-base font-semibold">Structured Postmortems</h2>
          <p className="mt-1 max-w-4xl text-sm leading-6 text-muted-foreground">
            Completed reviews are summarized as decision takeaways: why the setup failed, what rule would change, and what tradeoff that introduces.
          </p>
        </div>
        <StatusBadge tone="good">{rows.length.toLocaleString()} completed</StatusBadge>
      </div>
      <div className="mt-4 grid gap-3 xl:grid-cols-2">
        {rows.slice(0, 8).map((row) => (
          <PostmortemInsightCard key={textField(row, ["postmortem_id"], `${textField(row, ["ticker"])}-${textField(row, ["source_id"])}`)} row={row} onOpenTicker={onOpenTicker} />
        ))}
      </div>
    </section>
  );
}

export function PostmortemInsightCard({ row, onOpenTicker }: { row: RowRecord; onOpenTicker: OpenTicker }) {
  const ticker = textField(row, ["ticker"]);
  const evidence = jsonArrayField(row, "evidence").filter((item): item is string => typeof item === "string" && item.trim().length > 0);
  const failure = displayField(row, ["failure_type"], "Unclassified failure");
  const confidence = numberField(row, ["confidence"], Number.NaN);
  const ruleChange = displayField(row, ["proposed_rule_change"]);
  const expectedEffect = displayField(row, ["expected_effect"]);
  const risk = displayField(row, ["risk"]);
  return (
    <article className="rounded-md border border-border/70 bg-background p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            {ticker ? <TickerButton ticker={ticker} onOpenTicker={onOpenTicker} /> : <span className="font-semibold">Unknown</span>}
            <StatusBadge tone="bad">{titleLabel(displayField(row, ["outcome_type"], "Outcome"))}</StatusBadge>
            <StatusBadge tone={Number.isFinite(confidence) && confidence >= 0.8 ? "good" : "warn"}>{Number.isFinite(confidence) ? `${Math.round(confidence * 100)}% confidence` : "Confidence pending"}</StatusBadge>
          </div>
          <h3 className="mt-2 text-sm font-semibold leading-6">{titleLabel(failure)}</h3>
        </div>
        <div className="text-xs text-muted-foreground">{formatDate(textField(row, ["created_at"]))}</div>
      </div>

      {evidence.length ? (
        <ul className="mt-3 space-y-1 text-sm leading-6 text-muted-foreground">
          {evidence.slice(0, 2).map((item) => (
            <li key={item}>- {item}</li>
          ))}
        </ul>
      ) : null}

      <div className="mt-3 space-y-2 text-sm leading-6">
        {ruleChange ? <InsightLine label="Rule change" value={ruleChange} /> : null}
        {expectedEffect ? <InsightLine label="Decision impact" value={expectedEffect} /> : null}
        {risk ? <InsightLine label="Tradeoff" value={risk} /> : null}
      </div>
    </article>
  );
}

