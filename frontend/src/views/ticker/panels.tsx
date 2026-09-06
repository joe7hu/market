import { ExternalLink } from "lucide-react";

import { resolveTradingViewSymbol, tradingViewEmbedUrl } from "@/adapters/tradingView";
import { DataTableFrame, StatusBadge } from "@/components/market/workstation";
import { DataFieldStateNotice, missingFieldState, decisionReason } from "@/components/market/dataFieldState";
import { Button } from "@/components/ui/button";
import type { components } from "@/generated/apiSchema";
import type { JsonValue, RowRecord, TickerDossier, TickerLearning, TickerPayload } from "@/types";
import { displayField, listField, symbolList, textField, titleLabel, toneFromText } from "@/views/rowFormat";
import type { OpenTicker } from "@/views/workspacePage";
import { PortfolioImpactCard, TradePlanCard } from "@/views/TradePlanCard";

import { CoverageBadge, DecisionStat, MetricGrid, ReasonList, SimpleTable } from "./cells";
import {
  arrayField,
  estimateForPeriod,
  moneyMetric,
  moneyOrNumber,
  multipleMetric,
  numberFrom,
  numberMetric,
  objectField,
  optionMove,
  percentMetric,
  presentMetricCells,
  ratioMetric,
  rowList,
  scoreMetric,
  skewDetail,
  targetRange,
} from "./data";

type TickerDecisionContract = components["schemas"]["TickerDecisionDetailResponse"];
type TickerDecisionSnapshotContract = components["schemas"]["TickerDecisionSnapshotResponse"];
type HorizonDecisionContract = components["schemas"]["HorizonDecision"];
type DataRequestContract = components["schemas"]["DataRequest"];
type AlphaSignalContract = components["schemas"]["AlphaSignal"];
type OpportunityRankContract = components["schemas"]["OpportunityRank"];
type TradePlanContract = components["schemas"]["TradePlan"];

export function TickerDecisionPanel({
  decision,
  snapshot,
  snapshotLoading,
  snapshotError,
  onLoadSnapshot,
  learning,
  collecting,
  onCollect,
}: {
  decision: TickerDecisionContract;
  snapshot?: TickerDecisionSnapshotContract | null;
  snapshotLoading: boolean;
  snapshotError: string | null;
  onLoadSnapshot: () => Promise<void>;
  learning?: TickerLearning;
  collecting: string | null;
  onCollect: (job: string) => Promise<void>;
}) {
  const action = decision.capital_action;
  const resolution = decision.resolution;
  const expressions = Object.values(decision.expressions ?? {});
  const alphaSignals = (snapshot?.alpha_signals ?? []) as AlphaSignalContract[];
  const opportunityRank = snapshot?.opportunity_rank as OpportunityRankContract | null | undefined;
  const tradePlan = snapshot?.trade_plan as TradePlanContract | null | undefined;
  const dataRequests = snapshot?.data_requests ?? [];
  const snapshotLearning = snapshot?.learning;
  const learningPayload = snapshotLearning && Object.keys(snapshotLearning).length
    ? snapshotLearning as TickerLearning
    : learning;
  const disagreement = learningPayload?.disagreement;
  const noNewTrade = ["CASH", "NO_TRADE", "AVOID"].includes(action.action);
  const blocker = opportunityRank?.blockers?.find((reason) => reason !== "cash_comparator")
    ?? (resolution?.primary_blocker !== "cash_comparator" ? resolution?.primary_blocker : undefined);
  const rationale = noNewTrade && blocker ? decisionReason(blocker) : decisionReason(action.rationale);

  return (
    <>
      <DataTableFrame title={noNewTrade ? "No new trade" : titleLabel(action.action)}>
        <div className="space-y-4 p-5">
          <div>
            <p className="text-sm text-muted-foreground">{action.owned ? "You hold this stock. A new trade decision does not replace the review of your existing position." : "You do not hold this stock."}</p>
            <p className="mt-3 max-w-2xl text-base leading-7">{rationale}</p>
            {resolution?.next_action ? <p className="mt-3 text-sm font-medium">Next: {decisionReason(resolution.next_action)}</p> : null}
            {action.action === "WAIT_FOR_PRICE" ? (
              <div className="mt-5 grid gap-3 rounded-md border border-[var(--warning)]/35 bg-[var(--warning)]/8 p-3 text-sm sm:grid-cols-3">
                <DecisionTerm label="Price" value={action.price_condition} field="price_condition" />
                <DecisionTerm label="Catalyst" value={action.catalyst} field="catalyst" />
                <DecisionTerm label="Expires" value={action.expires_at} field="expires_at" />
              </div>
            ) : null}
          </div>
          <div className="space-y-4">
            <details><summary className="cursor-pointer text-sm font-medium">Time horizon and price conditions</summary><div className="mt-3 grid gap-3 md:grid-cols-2">
              <HorizonCard view={decision.tactical} label="TACTICAL · 1–20 sessions" />
              <HorizonCard view={decision.fundamental} label="FUNDAMENTAL · 3–18 months" />
            </div></details>
            <details><summary className="cursor-pointer text-sm font-medium">Compare trade alternatives</summary><div className="mt-3"><ExpressionTable expressions={expressions} /></div></details>
            {decision.selected_expression?.kind !== "CASH" && decision.selected_expression?.execution_evidence ? <ExecutionEvidencePanel executionEvidence={decision.selected_expression.execution_evidence as Record<string, unknown>} /> : null}
          </div>
        </div>
      </DataTableFrame>
      {snapshot ? (
        <>
          {opportunityRank ? <OpportunityRankPanel signals={alphaSignals} rank={opportunityRank} /> : null}
          {noNewTrade ? <details><summary className="cursor-pointer text-sm font-medium">Trade eligibility details</summary><TradePlanCard plan={tradePlan} /></details> : <TradePlanCard plan={tradePlan} />}
          {dataRequests.length ? <details><summary className="cursor-pointer text-sm font-medium">Evidence needed ({dataRequests.length})</summary><DataRequestPanel requests={dataRequests} collecting={collecting} onCollect={onCollect} /></details> : null}
          {learningPayload ? <details><summary className="cursor-pointer text-sm font-medium">Past signal performance</summary><LearningLoopPanel learning={learningPayload} /></details> : null}
        </>
      ) : (
        <>
          {snapshotLoading ? <TradePlanCard pending /> : null}
          {snapshotError ? <div role="alert" className="rounded border border-border p-4 text-sm"><p>{snapshotError} Trade details could not be verified. Do not place a trade from this view.</p><Button type="button" variant="outline" disabled={snapshotLoading} onClick={() => void onLoadSnapshot()}>Retry decision details</Button></div> : null}

        </>
      )}
      <SelectedPortfolioImpact decision={decision} />
      <details><summary className="cursor-pointer text-sm font-medium">Market data checks</summary><TickerMarketEvidence decision={decision} /></details>
      {disagreement ? <DisagreementPanel learning={learningPayload} /> : null}
    </>
  );
}

export function ExecutionEvidencePanel({
  executionEvidence,
}: {
  executionEvidence?: Record<string, unknown> | null;
}) {
  const evidence = executionEvidence ?? {};
  const status = typeof evidence.status === "string" && evidence.status.trim() ? evidence.status : null;
  const fields = [
    ["Observed at", evidence.observed_at],
    ["Freshness", evidence.freshness_status === "available" ? null : evidence.freshness_status],
    ["Delta", evidence.delta],
    ["Gamma", evidence.gamma],
    ["Vega", evidence.vega],
    ["Theta", evidence.theta],
    ["BTC beta", evidence.btc_beta],
    ["ETH beta", evidence.eth_beta],
    ["Funding", evidence.funding],
    ["Basis", evidence.basis],
    ["Open interest", evidence.open_interest],
    ["Days to exit", evidence.days_to_exit],
    ["Capacity", evidence.capacity],
  ].filter(([, value]) => typeof value === "string" || typeof value === "number");
  const blockers = Array.isArray(evidence.blockers) ? evidence.blockers.map(String) : [];
  return (
    <DataTableFrame title="Execution-grade evidence" action={status && status !== "available" ? <StatusBadge tone="warn">{decisionReason(status)}</StatusBadge> : undefined}>
      {fields.length ? <div className="grid gap-2 p-4 text-xs sm:grid-cols-3">
        {fields.map(([label, value]) => <DecisionKeyValue key={String(label)} label={String(label)} value={String(value)} field={String(label).toLowerCase().replace(/[^a-z0-9]+/g, "_")} source="execution_evidence" />)}
      </div> : <div className="p-4"><DataFieldStateNotice state={missingFieldState({ field: "execution_evidence", source: "ticker_decision_snapshot", reason: "execution_evidence_missing", nextAction: "Refresh execution evidence before placing an order." })} /></div>}
      {blockers.length ? <ReasonList title="Evidence blockers" rows={blockers.map(decisionReason)} empty="No blockers" /> : null}
    </DataTableFrame>
  );
}

export function TickerMarketEvidence({ decision }: { decision: TickerDecisionContract }) {
  const assessment = decision.market_evidence_assessment;
  const requiredDimensions = assessment?.required_dimensions ?? [];
  const blockers = assessment?.blockers ?? [];
  return (
    <DataTableFrame title="Market evidence for this decision">
      <div className="grid gap-2 p-4 text-xs sm:grid-cols-2">
        {assessment ? (
          <div className="rounded border border-border/70 p-2">
            <p className="font-semibold">{assessment.expression_kind} · {assessment.decision_horizon}</p>
            <p className="mt-1 text-muted-foreground">{assessment.status === "available" ? "" : `${titleLabel(assessment.status)} · `}Checks: {requiredDimensions.map(titleLabel).join(", ") || "No required checks supplied"}</p>
            {blockers.length ? <p className="mt-1 text-muted-foreground">Blocking: {blockers.map(decisionReason).join(" ")}</p> : null}
          </div>
        ) : <DataFieldStateNotice compact state={missingFieldState({ field: "market_evidence_assessment", source: "ticker_decision", reason: "market_evidence_assessment_missing", nextAction: "Refresh the canonical ticker decision before using market evidence." })} />}
      </div>
    </DataTableFrame>
  );
}

function SelectedPortfolioImpact({ decision }: { decision: TickerDecisionContract }) {
  const kind = decision.selected_expression?.kind;
  if (!kind || kind === "CASH") return null;
  const impact = decision.portfolio_impacts?.[kind];
  return impact ? <PortfolioImpactCard impact={{ ...impact, ticker: decision.ticker, expression_kind: kind }} /> : <p className="text-sm text-muted-foreground">Portfolio impact has not been calculated. Do not size this trade yet.</p>;
}

function HorizonCard({ view, label }: { view: HorizonDecisionContract; label: string }) {
  return (
    <article className="rounded-md border border-border/80 bg-background/60 p-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">{label}</p>
          <p className="mt-1 text-lg font-semibold">{titleLabel(view.stance)} <span className="text-sm font-normal text-muted-foreground">· {titleLabel(view.action)}</span></p>
        </div>
        <StatusBadge tone={toneFromText(view.stance)}>{titleLabel(view.conviction_tier)}</StatusBadge>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 text-xs">
        <DecisionTerm label="Review" value={view.expiry_date} field="expiry_date" />
        <DecisionTerm label="Entry" value={priceRangeText(view.entry_range)} field="entry_range" />
        <DecisionTerm label="Target" value={priceRangeText(view.target_range)} field="target_range" />
        <DecisionTerm label="Invalidation" value={invalidationText(view.invalidation)} field="invalidation" />
        <DecisionTerm label="Confidence" value={percentText(view.confidence)} field="confidence" />
      </div>
      <ScenarioRail scenarios={view.scenarios} />
    </article>
  );
}

function ScenarioRail({ scenarios }: { scenarios: TickerDecisionContract["tactical"]["scenarios"] }) {
  const complete = scenarios.length === 3 && scenarios.every((scenario) => scenario.probability != null);
  return (
    <div className="mt-4">
      {complete ? <div className="flex h-2 overflow-hidden rounded-full bg-muted" aria-label="Scenario probabilities">
        {scenarios.map((scenario) => (
          <span
            key={scenario.name}
            className={scenario.name === "bull" ? "bg-[var(--success)]" : scenario.name === "bear" ? "bg-[var(--destructive)]" : "bg-[var(--warning)]"}
            style={{ width: `${(scenario.probability ?? 0) * 100}%` }}
            title={`${scenario.name} ${percent(scenario.probability)}`}
          />
        ))}
      </div> : <DataFieldStateNotice compact state={missingFieldState({ field: "scenario_probabilities", source: "ticker_decision_snapshot", reason: "scenario_probabilities_missing", nextAction: "Refresh scenario evidence before placing an order." })} />}
      <div className="mt-2 grid grid-cols-3 gap-2 text-[11px] text-muted-foreground">
        {scenarios.map((scenario) => <span key={scenario.name}><strong className="text-foreground">{scenario.name}</strong> {percent(scenario.probability)}</span>)}
      </div>
    </div>
  );
}

export function OpportunityRankPanel({
  signals,
  rank,
}: {
  signals: AlphaSignalContract[];
  rank?: OpportunityRankContract | null;
}) {
  const signal = signals.find((item) => item.signal_id === rank?.alpha_signal_id);
  return <DataTableFrame title="Research and trade ranking"><div className="grid gap-2 p-4 text-sm sm:grid-cols-3">
    {rank?.research_rank != null ? <KeyValue label="Research rank" value={`#${rank.research_rank} within this ranking`} /> : null}
    {rank?.trade_rank != null ? <KeyValue label="Paper trade rank" value={`#${rank.trade_rank}`} /> : null}
    {rank?.trade_rank_unavailable_reason ? <KeyValue label="Before trading" value={decisionReason(rank.blockers?.find((reason) => reason !== "cash_comparator") ?? rank.trade_rank_unavailable_reason)} /> : null}
    {signal?.horizon ? <KeyValue label="Horizon" value={signal.horizon} /> : null}
    {signal?.effective_sample_size != null ? <KeyValue label="Evidence sample" value={String(signal.effective_sample_size)} /> : null}
  </div></DataTableFrame>;
}

function ExpressionTable({ expressions }: { expressions: components["schemas"]["ExpressionDecision"][] }) {
  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold">Trade alternatives</h3>
        <span className="text-xs text-muted-foreground">same thesis · same invalidation</span>
      </div>
      {expressions.length ? <div className="overflow-x-auto rounded-md border border-border">
        <table className="w-full min-w-[900px] text-sm">
          <thead className="border-b border-border bg-muted/60 text-left text-xs text-muted-foreground">
            <tr>{["Expression", "State", "Qty", "Planned loss", "Net utility", "Costs", "Horizon"].map((label) => <th key={label} className="px-3 py-3 font-medium">{label}</th>)}</tr>
          </thead>
          <tbody>
            {expressions.map((expression) => (
              <tr key={expression.kind} className="border-b border-border last:border-b-0 align-top">
                <td className="px-3 py-3">{titleLabel(expression.kind)}</td>
                <td className="px-3 py-3">{expression.selected ? "Selected" : titleLabel(expression.status)}</td>
                <td className="px-3 py-3"><ExpressionTerm field="quantity" value={expression.quantity == null ? null : expression.quantity.toLocaleString()} /></td>
                <td className="px-3 py-3"><ExpressionTerm field="planned_loss" value={moneyText(expression.planned_loss)} /></td>
                <td className="px-3 py-3"><ExpressionTerm field="net_expected_value_per_loss_dollar" value={numberTextOrNull(expression.net_expected_value_per_loss_dollar)} /></td>
                <td className="px-3 py-3"><ExpressionTerm field="expected_transaction_costs" value={moneyText(expression.expected_transaction_costs)} /></td>
                <td className="px-3 py-3"><ExpressionTerm field="horizon_fit" value={percentText(expression.horizon_fit)} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div> : <p className="text-sm text-muted-foreground">No expression comparison is available.</p>}
    </div>
  );
}

function DataRequestPanel({ requests, collecting, onCollect }: { requests: DataRequestContract[]; collecting: string | null; onCollect: (job: string) => Promise<void> }) {
  return (
    <DataTableFrame title="Evidence needed" action={<StatusBadge tone="warn">{requests.length} open</StatusBadge>}>
      <div className="divide-y divide-border">
        {requests.map((request) => (
          <div key={`${request.ticker}:${request.field}`} className="grid gap-3 p-4 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center">
            <div>
              <div className="flex flex-wrap items-center gap-2"><span className="font-semibold">{titleLabel(request.field)}</span><span className="text-xs text-muted-foreground">{request.ticker}</span></div>
              <p className="mt-1 text-sm leading-6 text-muted-foreground">{decisionReason(request.why_it_matters)}</p>
              <p className="mt-1 text-xs text-muted-foreground">Result: {decisionReason(request.expected_completion)} Change: {decisionReason(request.decision_impact)}</p>
            </div>
            <Button type="button" variant="outline" size="sm" disabled={collecting !== null} onClick={() => void onCollect(request.collect_now)}>
              {collecting === request.collect_now ? "Running…" : "Refresh evidence"}
            </Button>
          </div>
        ))}
      </div>
    </DataTableFrame>
  );
}

function DisagreementPanel({ learning }: { learning?: TickerLearning }) {
  const disagreement = learning?.disagreement;
  if (!disagreement) return null;
  return (
    <DataTableFrame title="Case and countercase">
      <div className="grid gap-0 md:grid-cols-3">
        <KeyValueBlock title="Strongest bull case" value={disagreement.strongest_bull_case} />
        <KeyValueBlock title="Opposing evidence" value={disagreement.strongest_bear_case ?? "No opposing source evidence is recorded in this decision. Review the thesis invalidation conditions above; this gap does not confirm the bull case."} />
        <KeyValueBlock title="Fact that resolves it" value={disagreement.resolving_fact} />
      </div>
    </DataTableFrame>
  );
}

function LearningLoopPanel({ learning }: { learning: TickerLearning }) {
  const policy = learning.strategy_learning;
  const governance = learning.governance;
  const metrics = policy?.metrics ?? {};
  const expressionRows = (learning.expression_tournament ?? []).map((row) => {
    const outcomes = arrayValue(row.outcomes);
    const latest = outcomes.length ? objectValue(outcomes[0]) : undefined;
    return {
      expression: textValue(row.expression_kind),
      state: textValue(row.selected) === "true" ? "SELECTED" : textValue(row.status).toUpperCase(),
      horizon: latest ? `${textValue(latest.horizon)} · ${textValue(latest.horizon_sessions)} sessions` : "—",
      result: latest ? percentValue(latest.expression_return) : "—",
    };
  });
  const mistakeRows = (learning.mistake_cards ?? []).slice(0, 6).map((row) => {
    const card = objectValue(row.card);
    return {
      error: titleLabel(textValue(row.error_type, "unclassified")),
      horizon: `${textValue(row.horizon)} · ${textValue(row.horizon_sessions)} sessions`,
      lesson: textValue(card?.proposed_rule_change, "No deterministic rule change recorded."),
    };
  });
  const status = textValue(policy?.status, "collecting").toUpperCase();
  const promotionLabel = policy?.automatic_promotion ? "AUTO-PROMOTION READY" : "PROMOTION GATED";
  return (
    <DataTableFrame
      title="Signal learning loop"
      action={<div className="flex flex-wrap items-center gap-2"><StatusBadge tone={toneFromText(status)}>{status}</StatusBadge><span className="text-xs text-muted-foreground">{promotionLabel} · paper only</span></div>}
    >
      <p className="border-b border-border px-4 py-3 text-xs text-muted-foreground">
        Authority: {textValue(learning.outcome_authority, "legacy compatibility")}
        {learning.outcome_evidence_label ? ` · Evidence: ${learning.outcome_evidence_label}` : ""}
        {learning.outcome_authority_blocker ? ` · ${learning.outcome_authority_blocker}` : ""}
      </p>
      {governance ? (
        <div className="border-b border-border bg-muted/10 px-4 py-3 text-xs text-muted-foreground">
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            {textValue(governance.status) ? <span>Governance: <strong className="text-foreground">{textValue(governance.status).toUpperCase()}</strong></span> : <DataFieldStateNotice compact state={missingFieldState({ field: "governance_status", source: "strategy_learning", reason: "governance_status_missing", nextAction: "Refresh governance evidence before using learning output." })} />}
            <span>Paper only: {governance.paper_only ? "YES" : "NO"}</span>
            {textValue(governance.live_eligibility) ? <span>Live: {textValue(governance.live_eligibility)}</span> : <DataFieldStateNotice compact state={missingFieldState({ field: "live_eligibility", source: "strategy_learning", reason: "live_eligibility_missing", nextAction: "Refresh governance evidence before using learning output." })} />}
          </div>
          {governance.blockers?.length ? <p className="mt-1">Blocked by: {governance.blockers.map((item) => textValue(item)).join(" · ")}</p> : null}
        </div>
      ) : null}
      <div className="grid gap-0 border-b border-border sm:grid-cols-2 xl:grid-cols-5">
        <LearningMetric label="Effective episodes" value={String(learning.effective_sample_count ?? learning.independent_episode_count ?? 0)} detail="one decision-horizon unit" />
        <LearningMetric label="Trading days" value={numberText(metrics.trading_day_count)} detail="independent span" />
        <LearningMetric label="Lower 95%" value={percentValue(metrics.selected_lower_95_net_expectancy)} detail="net expectancy after costs" />
        <LearningMetric label="Brier" value={numberText(metrics.brier_score)} detail="probability calibration" />
        <LearningMetric label="Policy" value={policy?.paper_only === false ? "LIVE" : "PAPER"} detail={policy?.active_policy_change ?? "paper signal policy"} />
      </div>
      <div className="grid gap-0 xl:grid-cols-2">
        <div className="border-b border-border p-4 xl:border-b-0 xl:border-r">
          <div className="mb-2 flex items-center justify-between gap-3"><h3 className="text-sm font-semibold">Trade alternatives outcomes</h3><span className="text-xs text-muted-foreground">same ticker thesis</span></div>
          <SimpleTable rows={expressionRows} empty="No executable expression outcome is measured yet." columns={[["expression", "Expression"], ["state", "State"], ["horizon", "Horizon"], ["result", "Return"]]} />
        </div>
        <div className="p-4">
          <div className="mb-2 flex items-center justify-between gap-3"><h3 className="text-sm font-semibold">Mistake cards</h3><span className="text-xs text-muted-foreground">deterministic lessons</span></div>
          <SimpleTable rows={mistakeRows} empty="No resolved mistake card yet." columns={[["error", "Error"], ["horizon", "Horizon"], ["lesson", "Proposed rule change"]]} />
        </div>
      </div>
      {policy?.blockers?.length ? <div className="border-t border-border bg-muted/20 px-4 py-3 text-xs leading-5 text-muted-foreground"><span className="font-semibold text-foreground">Promotion blockers:</span> {policy.blockers.map((blocker) => textValue(blocker)).join(" · ")}</div> : null}
    </DataTableFrame>
  );
}

function LearningMetric({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <div className="border-b border-border px-4 py-3 last:border-b-0 sm:border-b-0 sm:border-r xl:last:border-r-0"><p className="text-[11px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">{label}</p><p className="mt-1 text-lg font-semibold tabular-nums">{value}</p><p className="mt-1 text-xs leading-5 text-muted-foreground">{detail}</p></div>;
}

function objectValue(value: JsonValue | undefined): Record<string, JsonValue | undefined> | undefined {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, JsonValue | undefined> : undefined;
}

function arrayValue(value: JsonValue | undefined): JsonValue[] {
  return Array.isArray(value) ? value : [];
}

function textValue(value: JsonValue | undefined, fallback = "—"): string {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

function numberText(value: JsonValue | undefined): string {
  if (typeof value === "number" && Number.isFinite(value)) return value.toLocaleString(undefined, { maximumFractionDigits: 3 });
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value).toLocaleString(undefined, { maximumFractionDigits: 3 });
  return "—";
}

function numberTextOrNull(value: number | null | undefined): string | null {
  return value == null || !Number.isFinite(value) ? null : numberText(value);
}

function percentValue(value: JsonValue | undefined): string {
  if (typeof value === "number" && Number.isFinite(value)) return `${(value * 100).toFixed(1)}%`;
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return `${(Number(value) * 100).toFixed(1)}%`;
  return "—";
}

function KeyValue({ label, value }: { label: string; value: string }) {
  return <div><span className="block uppercase tracking-[0.08em] text-muted-foreground">{label}</span><strong className="mt-0.5 block break-words font-medium text-foreground">{decisionReason(value)}</strong></div>;
}

function DecisionTerm({
  label,
  value,
  field,
  source = "ticker_decision",
  nextAction = "Refresh the canonical ticker decision before acting.",
}: {
  label: string;
  value: string | null | undefined;
  field: string;
  source?: string;
  nextAction?: string;
}) {
  if (value != null && value.trim()) return <KeyValue label={label} value={value} />;
  return <KeyValue label={label} value="Unknown" />;
}

function ExpressionTerm({ field, value }: { field: string; value: string | null }) {
  if (value != null) return <>{value}</>;
  return <span className="text-muted-foreground">Unknown</span>;
}

function DecisionKeyValue({ label, value, field, source }: { label: string; value: string | null | undefined; field: string; source: string }) {
  if (value != null && value.trim()) return <KeyValue label={label} value={value} />;
  return <DataFieldStateNotice compact state={missingFieldState({ field, source, reason: `${field}_missing`, nextAction: "Refresh the validated ticker decision snapshot before acting." })} />;
}

function KeyValueBlock({ title, value }: { title: string; value?: string | null }) {
  return <div className="border-b border-border p-4 last:border-b-0 md:border-b-0 md:border-r md:last:border-r-0"><p className="text-xs font-semibold uppercase tracking-[0.1em] text-muted-foreground">{title}</p><p className="mt-2 text-sm leading-6">{value ? decisionReason(value) : "Not loaded"}</p></div>;
}

function money(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: value >= 100 ? 0 : 2 });
}

function moneyText(value: number | null | undefined): string | null {
  return value == null || !Number.isFinite(value) ? null : money(value);
}

function percent(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(0)}%`;
}

function percentText(value: number | null | undefined): string | null {
  return value == null || !Number.isFinite(value) ? null : percent(value);
}

function priceRange(value: components["schemas"]["PriceRange"] | null | undefined): string {
  if (!value) return "—";
  return value.low === value.high ? money(value.low) : `${money(value.low)}–${money(value.high)}`;
}

function priceRangeText(value: components["schemas"]["PriceRange"] | null | undefined): string | null {
  return value ? priceRange(value) : null;
}

function invalidation(value: components["schemas"]["Invalidation"] | null | undefined): string {
  if (!value) return "—";
  return value.statement || String(value.value);
}

function invalidationText(value: components["schemas"]["Invalidation"] | null | undefined): string | null {
  if (!value) return null;
  if (value.statement?.trim()) return value.statement;
  return value.value == null ? null : String(value.value);
}

export function DecisionPanel({ brief }: { brief: RowRecord }) {
  const verdict = objectField(brief, "verdict");
  const setup = objectField(brief, "setup");
  const riskPlan = objectField(brief, "risk_plan");
  const quote = objectField(brief, "canonical_quote");
  const action = displayField(verdict, ["action"], "WAIT_FOR_PRICE");
  const supports = listField(brief, ["evidence_for"]).slice(0, 4);
  const concerns = listField(brief, ["evidence_against"]).slice(0, 4);
  const unknowns = listField(brief, ["unknowns"]).slice(0, 3);
  const setupRows = [
    { label: "Entry", value: displayField(setup, ["entry_zone"], "No entry plan loaded") },
    { label: "Invalidation", value: displayField(riskPlan, ["invalidation"], displayField(setup, ["invalidation_level"], "No invalidation loaded")) },
    { label: "Target", value: displayField(setup, ["target_range"], "No target loaded") },
    { label: "Review", value: displayField(setup, ["review_date"], "No review date loaded") },
  ];
  return (
    <DataTableFrame title="Decision" action={<StatusBadge tone={toneFromText(action)}>{action}</StatusBadge>}>
      <div className="grid gap-0 xl:grid-cols-[minmax(0,0.85fr)_minmax(360px,0.65fr)]">
        <div className="border-b border-border p-4 xl:border-b-0 xl:border-r">
          <div className="mb-4 grid gap-2 sm:grid-cols-3">
            <DecisionStat label="Confidence" value={displayField(verdict, ["confidence"], "-")} detail={displayField(verdict, ["freshness"], "freshness not loaded")} />
            <DecisionStat label="Price" value={moneyMetric(quote, "price")} detail={displayField(quote, ["observed_at"], "quote timestamp missing")} />
            <DecisionStat label="Timeframe" value={displayField(setup, ["timeframe"], "-")} detail={displayField(setup, ["catalyst"], "no catalyst loaded")} />
          </div>
          <p className="mb-3 text-base font-medium leading-7">{displayField(verdict, ["summary"], "No decision summary loaded.")}</p>
          <p className="text-sm leading-6 text-muted-foreground">{displayField(verdict, ["next_action"], "No next action loaded.")}</p>
          <div className="mt-4 overflow-x-auto">
            <SimpleTable rows={setupRows} empty="No decision setup is loaded." columns={[["label", "Plan"], ["value", "Value"]]} />
          </div>
        </div>
        <div className="grid gap-4 p-4">
          <ReasonList title="Why It Could Work" rows={supports} empty="No positive evidence loaded." />
          <ReasonList title="Why It Is Gated" rows={concerns} empty="No risk evidence loaded." />
          {unknowns.length ? <ReasonList title="Still Unknown" rows={unknowns} empty="" /> : null}
        </div>
      </div>
    </DataTableFrame>
  );
}

export function TradingViewChart({ symbol, ticker }: { symbol: string; ticker: TickerPayload | null }) {
  const tradingViewSymbol = resolveTradingViewSymbol(symbol, ticker);
  const chartUrl = tradingViewEmbedUrl(tradingViewSymbol);
  const externalUrl = `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(tradingViewSymbol)}`;
  return (
    <DataTableFrame
      title="External live chart"
      action={
        <Button asChild type="button" variant="outline" size="sm">
          <a href={externalUrl} target="_blank" rel="noreferrer"><ExternalLink /> Open TradingView</a>
        </Button>
      }
    >
      <p className="px-4 pt-3 text-xs text-muted-foreground">Live external reference only. It is not Market snapshot data and cannot authorize a decision.</p>
      <div className="h-[360px] w-full bg-muted/30 sm:h-[440px]">
        <iframe
          title={`${symbol} TradingView chart`}
          src={chartUrl}
          className="h-full w-full border-0"
          loading="lazy"
          referrerPolicy="no-referrer-when-downgrade"
          sandbox="allow-scripts allow-same-origin allow-popups allow-forms"
        />
      </div>
    </DataTableFrame>
  );
}

export function FundamentalsPanel({ fundamentals }: { fundamentals: TickerDossier["fundamentals"] }) {
  const sec = fundamentals.sec ?? {};
  const market = fundamentals.market ?? {};
  const hasSec = numberFrom(sec.revenue) !== null;

  const metricRows = presentMetricCells([
    ["Revenue", moneyMetric(sec, "revenue"), "SEC company facts revenue"],
    ["Revenue YoY", ratioMetric(sec, "revenue_growth"), "latest annual period"],
    ["Net Income", moneyMetric(sec, "net_income"), "SEC company facts net income"],
    ["Net Margin", ratioMetric(sec, "net_margin"), "net income / revenue"],
    ["Free Cash Flow", moneyMetric(sec, "free_cash_flow"), "operating cash flow minus capex"],
    ["FCF Margin", ratioMetric(sec, "fcf_margin"), "free cash flow / revenue"],
    ["Assets", moneyMetric(sec, "assets"), "latest balance sheet"],
    ["Liabilities", moneyMetric(sec, "liabilities"), "latest balance sheet"],
    ["Cash", moneyMetric(sec, "cash"), "cash and equivalents"],
    ["Debt / Assets", ratioMetric(sec, "debt_to_assets"), "liabilities / assets"],
  ]);

  const marketRows = presentMetricCells([
    ["Market Cap", moneyMetric(market, "market_cap"), "market data"],
    ["P/S", multipleMetric(market, "ps_ratio"), "sales multiple"],
    ["P/E", multipleMetric(market, "pe_ratio"), "earnings multiple"],
    ["Forward P/E", multipleMetric(market, "forward_pe"), textField(market, ["forward_pe_source"], "forward estimate")],
    ["FCF Yield", ratioMetric(market, "fcf_yield"), "free cash flow yield"],
    ["ROIC", percentMetric(market, "roic"), textField(market, ["roic_source"], "capital returns")],
  ]);

  return (
    <DataTableFrame
      title="Authoritative Fundamentals"
      action={sec.source_url ? (
        <Button asChild type="button" variant="outline" size="sm">
          <a href={String(sec.source_url)} target="_blank" rel="noreferrer"><ExternalLink /> SEC source</a>
        </Button>
      ) : <CoverageBadge coverage={fundamentals.coverage} />}
    >
      <div className="grid gap-0 lg:grid-cols-[1fr_0.65fr]">
        <div className="border-b border-border p-4 lg:border-b-0 lg:border-r">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <StatusBadge tone={hasSec ? "good" : "warn"}>{hasSec ? textField(sec, ["form_type"], "SEC filing") : "No SEC row"}</StatusBadge>
            <span className="text-sm text-muted-foreground">
              {hasSec ? `${displayField(sec, ["filing_date", "period_end"])} from SEC company facts` : "Direct company-facts metrics are not loaded for this ticker."}
            </span>
          </div>
          <MetricGrid rows={metricRows} empty="No authoritative SEC metrics are loaded." />
        </div>
        <div className="p-4">
          <h3 className="mb-3 text-sm font-semibold">Market-Derived Complements</h3>
          <MetricGrid rows={marketRows} empty="No market-derived valuation metrics are loaded." />
        </div>
      </div>
    </DataTableFrame>
  );
}

export function TechnicalsPanel({ technicals }: { technicals: TickerDossier["technicals"] }) {
  const trend = technicals.trend ?? {};
  const momentum = technicals.momentum ?? {};
  const sepa = technicals.sepa ?? {};
  const liquidity = technicals.liquidity ?? {};

  const trendRows = presentMetricCells([
    ["Close", moneyMetric(trend, "close"), displayField(momentum, ["as_of"], "latest bar")],
    ["20D MA", moneyMetric(trend, "ma20"), "short-term trend"],
    ["50D MA", moneyMetric(trend, "ma50"), "intermediate trend"],
    ["200D MA", moneyMetric(trend, "ma200"), "long-term trend"],
    ["Drawdown", ratioMetric(trend, "drawdown_from_high"), "from 52w high"],
    ["Tech Score", scoreMetric(momentum, "technical_score"), "composite momentum"],
  ]);
  const momentumRows = presentMetricCells([
    ["20D Return", ratioMetric(momentum, "return_20d"), "1 month"],
    ["3M Return", ratioMetric(momentum, "return_3m"), "quarter"],
    ["YTD Return", ratioMetric(momentum, "return_ytd"), "year to date"],
    ["1Y Return", ratioMetric(momentum, "return_1y"), "trailing year"],
    ["Rel Volume", numberMetric(momentum, "rel_volume_1m"), "1m vs baseline"],
    ["ATR %", ratioMetric(momentum, "atr_pct_1m"), "1m average true range"],
  ]);
  const checklist = objectField(sepa, "checklist");
  const checklistRows = Object.entries(checklist).map(([key, value]) => ({
    check: titleLabel(key),
    status: value === true ? "Pass" : value === false ? "Fail" : displayField({ value } as RowRecord, ["value"], "-"),
  }));

  return (
    <DataTableFrame
      title="Technicals & Trend"
      action={<CoverageBadge coverage={technicals.coverage} />}
    >
      <div className="grid gap-0 xl:grid-cols-2">
        <div className="border-b border-border p-4 xl:border-b-0 xl:border-r">
          <h3 className="mb-3 text-sm font-semibold">Trend Structure</h3>
          <MetricGrid rows={trendRows} empty="No technical trend metrics are loaded." />
          <h3 className="mb-3 mt-4 text-sm font-semibold">Momentum</h3>
          <MetricGrid rows={momentumRows} empty="No momentum metrics are loaded." />
        </div>
        <div className="p-4">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold">SEPA Stage</h3>
            {sepa.stage ? <StatusBadge tone={toneFromText(textField(sepa, ["verdict", "stage"], "info"))}>{titleLabel(textField(sepa, ["stage"], "-"))}</StatusBadge> : null}
            <span className="text-sm text-muted-foreground">{displayField(sepa, ["verdict"])} · score {scoreMetric(sepa, "score")}</span>
          </div>
          <SimpleTable rows={checklistRows} empty="No SEPA checklist is loaded." columns={[["check", "Trend check"], ["status", "Status"]]} />
          <h3 className="mb-3 mt-4 text-sm font-semibold">Liquidity</h3>
          <MetricGrid
            rows={presentMetricCells([
              ["Grade", displayField(liquidity, ["grade"], "-").replace(/_/g, " "), "tradeability"],
              ["Avg $ Vol", moneyMetric(liquidity, "avg_dollar_volume"), "60d average"],
              ["1% ADV Impact", liquidity.impact_1pct_adv_bps != null ? `${numberMetric(liquidity, "impact_1pct_adv_bps")} bps` : "-", "modeled slippage"],
            ])}
            empty="No liquidity metrics are loaded."
          />
        </div>
      </div>
    </DataTableFrame>
  );
}

export function EstimatesPanel({ estimates }: { estimates: TickerDossier["estimates"] }) {
  const analyst = estimates.analyst ?? {};
  const setup = estimates.earnings_setup ?? {};
  const event = estimates.earnings_event ?? {};
  const earnings = arrayField(analyst as RowRecord, "earnings_estimate");
  const revenue = arrayField(analyst as RowRecord, "revenue_estimate");
  const targets = objectField(analyst as RowRecord, "price_targets");
  const currentYearEps = estimateForPeriod(earnings, "0y");
  const nextYearEps = estimateForPeriod(earnings, "+1y");
  const currentYearRevenue = estimateForPeriod(revenue, "0y");
  const nextYearRevenue = estimateForPeriod(revenue, "+1y");
  const estimateRows = presentMetricCells([
    ["CY Revenue", moneyMetric(currentYearRevenue, "avg"), ratioMetric(currentYearRevenue, "growth")],
    ["NY Revenue", moneyMetric(nextYearRevenue, "avg"), ratioMetric(nextYearRevenue, "growth")],
    ["CY EPS", numberMetric(currentYearEps, "avg"), ratioMetric(currentYearEps, "growth")],
    ["NY EPS", numberMetric(nextYearEps, "avg"), ratioMetric(nextYearEps, "growth")],
    ["Target Mean", moneyMetric(targets, "mean"), "analyst price targets"],
    ["Target Range", targetRange(targets), "low / high"],
  ]);
  const setupRows = presentMetricCells([
    ["Next Event", displayField(event, ["event_date"], "Not scheduled"), displayField(event, ["event_type"], "earnings")],
    ["Setup", displayField(setup, ["verdict"], "Not loaded"), `score ${scoreMetric(setup, "score")}`],
    ["Revision", scoreMetric(setup, "revision_score"), "estimate revisions"],
    ["Surprise", scoreMetric(setup, "surprise_score"), "historical surprise"],
    ["Sentiment", scoreMetric(setup, "sentiment_score"), "pre-earnings sentiment"],
    ["Est. Spread", scoreMetric(setup, "estimate_spread_score"), "analyst dispersion"],
  ]);
  return (
    <DataTableFrame title="Estimates & Earnings" action={<CoverageBadge coverage={estimates.coverage} />}>
      <div className="grid gap-0 xl:grid-cols-2">
        <div className="border-b border-border p-4 xl:border-b-0 xl:border-r">
          <div className="mb-3 text-sm text-muted-foreground">{analyst.as_of ? `yfinance snapshot ${displayField(analyst as RowRecord, ["as_of"])}` : "No analyst estimate row is loaded."}</div>
          <MetricGrid rows={estimateRows} empty="No analyst estimate metrics are loaded." />
        </div>
        <div className="p-4">
          <h3 className="mb-3 text-sm font-semibold">Earnings Setup</h3>
          <MetricGrid rows={setupRows} empty="No earnings setup is loaded." />
        </div>
      </div>
    </DataTableFrame>
  );
}

export function OptionsIntelligencePanel({ options }: { options: TickerDossier["options"] }) {
  const signal = options.signal ?? {};
  const expiries = rowList(options.expiries).slice(0, 8);
  const capability = rowList(options.capabilities).find((row) => textField(row, ["provider"]) === "tradingview");
  const ivRegime = displayField(signal, ["iv_regime"], "");
  const unavailableRows = rowList(options.unavailable_signals).slice(0, 6).map((row) => ({
    signal: displayField(row, ["signal"], "Signal"),
    reason: displayField(row, ["reason"], "Not supplied by TradingView V1"),
  }));
  const metrics = presentMetricCells([
    ["Status", displayField(signal, ["status"], "Missing"), displayField(signal, ["source"], "No options signal row")],
    ["ATM IV", ratioMetric(signal, "atm_iv"), ivRegime || "See the IV regime field state below"],
    ["Expected Move", optionMove(signal), displayField(signal, ["nearest_expiry"], "No expiry")],
    ["Skew", displayField(signal, ["skew_signal"], "-"), skewDetail(signal)],
    ["Spread", displayField(signal, ["spread_quality"], "-"), "bid/ask quality"],
    ["Hedge", displayField(signal, ["hedge_summary"], "-"), "25-delta put candidate"],
    ["Income", displayField(signal, ["income_summary"], "-"), "30-delta call candidate"],
  ]);
  const expiryRows = expiries.map((row) => ({
    expiry: displayField(row, ["expiry"], "-"),
    dte: displayField(row, ["dte"], "-"),
    atm: moneyOrNumber(row, "atm_strike"),
    iv: ratioMetric(row, "atm_iv"),
    move: optionMove(row),
    skew: skewDetail(row),
    spread: displayField(row, ["spread_quality"], "-"),
  }));
  return (
    <DataTableFrame
      title="Options Intelligence"
      action={capability ? <StatusBadge tone={capability.supports_open_interest ? "good" : "warn"}>{displayField(capability, ["status"], "limited")}</StatusBadge> : <CoverageBadge coverage={options.coverage} />}
    >
      <div className="grid gap-0 xl:grid-cols-[minmax(0,0.95fr)_minmax(360px,0.65fr)]">
        <div className="border-b border-border p-4 xl:border-b-0 xl:border-r">
          <MetricGrid rows={metrics} empty="No options signal row is loaded for this ticker." />
          {!ivRegime ? <div className="mt-3"><DataFieldStateNotice compact state={missingFieldState({ field: "iv_regime", source: "options_signal", reason: "iv_regime_missing", nextAction: "Refresh the options signal before selecting an options expression." })} /></div> : null}
          <div className="mt-4 overflow-x-auto">
            <SimpleTable
              rows={expiryRows}
              empty="No expiry-level options signals are loaded."
              columns={[["expiry", "Expiry"], ["dte", "DTE"], ["atm", "ATM"], ["iv", "IV"], ["move", "Move"], ["skew", "Skew"], ["spread", "Spread"]]}
            />
          </div>
        </div>
        <div className="p-4">
          <div className="mb-3 flex items-center justify-between gap-3">
            <h3 className="text-sm font-semibold">Positioning Gates</h3>
            <StatusBadge tone="warn">OI/volume missing</StatusBadge>
          </div>
          <SimpleTable rows={unavailableRows} empty="No unavailable options signal metadata is loaded." columns={[["signal", "Signal"], ["reason", "Why"]]} />
        </div>
      </div>
    </DataTableFrame>
  );
}

export function ThesisPanel({ thesis }: { thesis: TickerDossier["thesis"] }) {
  const state = thesis.state ?? {};
  const packet = thesis.research_packet ?? {};
  const bull = listField(packet as RowRecord, ["bull_case"]).slice(0, 4);
  const bear = listField(packet as RowRecord, ["bear_case"]).slice(0, 4);
  const whyNow = listField(packet as RowRecord, ["why_now"]).slice(0, 3);
  const thesisText = textField(state, ["thesis"]);
  const invalidation = textField(state, ["invalidation"]);
  return <DataTableFrame title="Investment thesis" action={state.needs_review ? <StatusBadge tone="warn">Needs review</StatusBadge> : undefined}>
    <div className="space-y-4 p-4 text-sm leading-6">
      <p>{thesisText ? decisionReason(thesisText.split(/(?<=[.!?])\s+/)[0]) : "No supported investment thesis is available yet."}</p>
      {thesisText ? <details><summary className="cursor-pointer font-medium">Full research case</summary><p className="mt-2 whitespace-pre-line">{decisionReason(thesisText)}</p></details> : null}
      {invalidation ? <p><strong>What would change the case: </strong>{decisionReason(invalidation)}</p> : null}
      {bull.length ? <ReasonList title="Supporting case" rows={bull} empty="" /> : null}
      {bear.length ? <ReasonList title="Countercase" rows={bear} empty="" /> : null}
      {whyNow.length ? <ReasonList title="Why now" rows={whyNow} empty="" /> : null}
    </div>
  </DataTableFrame>;
}

export function OwnershipPanel({ ownership }: { ownership: TickerDossier["ownership"] }) {
  const institutional = ownership.institutional ?? {};
  const filings = rowList(ownership.filings).slice(0, 12);
  const metrics = presentMetricCells([
    ["Tracked Holders", numberMetric(institutional, "holders"), "13F + disclosures"],
    ["Net Activity", numberMetric(institutional, "net_activity"), `${numberMetric(institutional, "net_buys")} buys / ${numberMetric(institutional, "net_sells")} sells`],
    ["Disclosed Value", moneyMetric(institutional, "total_value"), "estimated notional"],
    ["Latest Filed", displayField(institutional, ["latest_filed"], "-"), "most recent filing"],
  ]);
  const filingRows = filings.map((row) => ({
    filer: displayField(row, ["filer_name", "trader_name"], "Filer"),
    action: displayField(row, ["action"], "-"),
    amount: displayField(row, ["amount"], "-"),
    date: displayField(row, ["filed_date", "event_date"], "-"),
  }));
  const holders = listField(institutional as RowRecord, ["holder_names", "investors"]).slice(0, 8);
  return (
    <DataTableFrame
      title="Ownership & Filings"
      action={<CoverageBadge coverage={ownership.coverage} />}
    >
      <div className="grid gap-0 xl:grid-cols-[minmax(0,0.8fr)_minmax(0,1fr)]">
        <div className="border-b border-border p-4 xl:border-b-0 xl:border-r">
          <MetricGrid rows={metrics} empty="No ownership consensus is loaded." />
          {holders.length ? (
            <div className="mt-3 flex flex-wrap gap-1.5">
              {holders.map((holder) => <StatusBadge key={holder} tone="info">{holder}</StatusBadge>)}
            </div>
          ) : null}
        </div>
        <div className="overflow-x-auto p-0">
          <SimpleTable
            rows={filingRows}
            empty="No tracked insider or institutional filings are loaded."
            columns={[["filer", "Filer"], ["action", "Action"], ["amount", "Amount"], ["date", "Filed"]]}
          />
        </div>
      </div>
    </DataTableFrame>
  );
}

export function PortfolioPanel({ portfolio }: { portfolio: TickerDossier["portfolio"] }) {
  const position = portfolio.position ?? {};
  const fit = portfolio.fit ?? {};
  const correlations = rowList(portfolio.correlations).slice(0, 6);
  const riskCards = rowList(portfolio.risk_cards).slice(0, 4);
  const metrics = presentMetricCells([
    ["Market Value", moneyMetric(position, "market_value"), "current position"],
    ["Weight", percentMetric(position, "weight"), "of portfolio"],
    ["Quantity", numberMetric(position, "quantity"), "shares"],
    ["Cost Basis", moneyMetric(position, "cost_basis"), "average price"],
    ["Unrealized", ratioMetric(position, "unrealized_pnl_pct"), "open P&L"],
    ["Exposure", displayField(fit, ["current_exposure"], "-"), displayField(fit, ["theme_concentration"], "")],
  ]);
  const correlationRows = correlations.map((row) => ({
    peer: displayField(row, ["peer_symbol"], "-"),
    correlation: numberMetric(row, "correlation"),
  }));
  const riskRows = riskCards.map((row) => ({
    risk: displayField(row, ["risk_level", "title", "name"], "Risk"),
    detail: displayField(row, ["summary", "action", "detail"], "-"),
  }));
  return (
    <DataTableFrame
      title="Portfolio Fit"
      action={<StatusBadge tone={portfolio.owned ? "good" : "muted"}>{portfolio.owned ? "Owned" : "Unowned"}</StatusBadge>}
    >
      <div className="grid gap-0 xl:grid-cols-2">
        <div className="border-b border-border p-4 xl:border-b-0 xl:border-r">
          <MetricGrid rows={metrics} empty="No portfolio position is loaded." />
        </div>
        <div className="p-4">
          <h3 className="mb-3 text-sm font-semibold">Risk & Correlation</h3>
          <SimpleTable rows={riskRows} empty="No portfolio risk cards are loaded." columns={[["risk", "Risk"], ["detail", "Detail"]]} />
          {correlationRows.length ? (
            <div className="mt-3 overflow-x-auto">
              <SimpleTable rows={correlationRows} empty="" columns={[["peer", "Peer"], ["correlation", "Correlation"]]} />
            </div>
          ) : null}
        </div>
      </div>
    </DataTableFrame>
  );
}

export function SourceCoveragePanel({ sources, onOpenTicker }: { sources: TickerDossier["sources"]; onOpenTicker: OpenTicker }) {
  const consensusRows = rowList(sources.consensus);
  const signalRows = rowList(sources.signals);
  const visibleRows = [
    ...consensusRows.slice(0, 8).map((row) => ({
      source: displayField(row, ["source_name"], "Source"),
      type: displayField(row, ["content_type"], "-"),
      net: displayField(row, ["net_consensus"], "-"),
      latest: displayField(row, ["latest_at"], "-"),
    })),
    ...signalRows.slice(0, 6).map((row) => ({
      source: displayField(row, ["source_name"], "Signal"),
      type: displayField(row, ["signal_type"], "-"),
      net: displayField(row, ["sentiment", "confidence"], "-"),
      latest: displayField(row, ["observed_at"], "-"),
    })),
  ];
  const tickers = [...new Set(consensusRows.flatMap(symbolList).filter(Boolean))].slice(0, 8);
  return (
    <DataTableFrame
      title="Source Coverage"
      action={tickers.length ? (
        <div className="flex flex-wrap justify-end gap-1.5">
          {tickers.map((ticker) => (
            <Button key={ticker} type="button" variant="ghost" size="sm" onClick={() => onOpenTicker(ticker)}>{ticker}</Button>
          ))}
        </div>
      ) : <CoverageBadge coverage={sources.coverage} />}
    >
      <SimpleTable
        rows={visibleRows}
        empty="No source coverage rows are loaded."
        columns={[["source", "Source"], ["type", "Type"], ["net", "Signal"], ["latest", "Latest"]]}
      />
    </DataTableFrame>
  );
}

export function EvidencePanel({ sources, thesisEvidence = [] }: { sources: TickerDossier["sources"]; thesisEvidence?: RowRecord[] }) {
  const visibleRows = [...thesisEvidence, ...rowList(sources.evidence)].slice(0, 12);
  return <DataTableFrame title="Source evidence">
    <div className="divide-y divide-border">{visibleRows.map((row, index) => {
      const reference = textField(row, ["reference", "url"]);
      const href = /^https?:\/\//i.test(reference) ? reference : undefined;
      const title = textField(row, ["title"], "Evidence item");
      return <article key={index} className="space-y-1 p-4 text-sm">
        {href ? <a className="font-medium underline" href={href} target="_blank" rel="noreferrer">{title.length > 240 ? `${title.slice(0, 240)}…` : title}</a> : <p className="font-medium">{title}</p>}
        <p className="text-xs text-muted-foreground">{textField(row, ["source_name", "source"], "Source")}{textField(row, ["date", "observed_at"]) ? ` · ${textField(row, ["date", "observed_at"])}` : ""}</p>
      </article>;
    })}</div>
    {!visibleRows.length ? <p className="p-4 text-sm text-muted-foreground">No linked source evidence is available for this ticker. Treat its thesis as unverified.</p> : null}
  </DataTableFrame>;
}
