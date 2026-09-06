import { DataTableFrame, StatusBadge } from "@/components/market/workstation";
import { DataFieldStateNotice, missingFieldState, decisionReason } from "@/components/market/dataFieldState";
import type { components } from "@/generated/apiSchema";
import { Children, isValidElement, type ReactNode } from "react";
import { expressionLabel } from "@/viewModels/expression";

type TradePlan = components["schemas"]["TradePlan"];
type PortfolioImpact = components["schemas"]["PortfolioImpact"];
type PortfolioImpactSummary = components["schemas"]["TickerPortfolioImpactSummaryResponse"] & { ticker?: string };
type PriceRange = components["schemas"]["PriceRange"];
type Invalidation = components["schemas"]["Invalidation"];
type TradePlanLeg = NonNullable<TradePlan["selected_expression"]["legs"]>[number];

export function TradePlanCard({ plan, pending = false }: { plan?: TradePlan | null; pending?: boolean }) {
  const actionable = isRenderableActionable(plan);
  return (
    <DataTableFrame
      title="Trade plan"
      action={<StatusBadge tone={actionable ? "good" : "warn"}>{actionable ? authorizationLabel(plan.authorization_mode) : pending ? "PENDING" : "NO TRADE"}</StatusBadge>}
    >
      {actionable ? <ActionablePlan plan={plan} /> : <BlockedPlan plan={plan} pending={pending} />}
    </DataTableFrame>
  );
}

export function PortfolioImpactCard({ impact }: { impact: PortfolioImpact | PortfolioImpactSummary }) {
  const fullImpact = "impact_id" in impact;
  if (impact.expression_kind?.toUpperCase() === "CASH") return null;
  const availability = fullImpact ? impact.availability_status : impact.availability;
  return (
    <DataTableFrame
      title={`${impact.ticker ?? "Selected"} proposed impact`}
      action={availability && availability !== "available" ? <StatusBadge tone="warn">Impact incomplete</StatusBadge> : undefined}
    >
      <div className="min-w-0 p-4 text-sm">
        {fullImpact ? <PortfolioImpactDetails impact={impact} /> : <PortfolioImpactSummaryDetails impact={impact} />}
      </div>
    </DataTableFrame>
  );
}

function PortfolioImpactSummaryDetails({ impact }: { impact: PortfolioImpactSummary }) {
  return <section className="space-y-4">
    <h3 className="text-sm font-semibold">Selected portfolio impact</h3>
    <ImpactSection title="Risk">
      <ImpactField label="Expression" value={displayText(impact.expression_kind)} />
      <ImpactField label="Risk budget consumed" value={numberValue(impact.risk_budget_consumed)} />
      <ImpactField label="Marginal risk" value={numberValue(impact.marginal_risk)} />
      <ImpactField label="Blockers" value={listValue(impact.blockers)} />
    </ImpactSection>
  </section>;
}

function ActionablePlan({ plan }: { plan: TradePlan }) {
  const impact = plan.portfolio_impact;
  const legs = plan.selected_expression?.legs ?? [];
  return (
    <div className="min-w-0 space-y-5 p-4 text-sm">
      <section>
        <h3 className="text-sm font-semibold">Stored terms</h3>
        <dl className="mt-3 grid min-w-0 gap-3 sm:grid-cols-2 xl:grid-cols-3">
          <Field label="Authorization" value={authorizationLabel(plan.authorization_mode)} />
          <Field label="Action" value={displayText(plan.action)} />
          <Field label="Ticker" value={displayText(plan.ticker)} />
          <Field label="Expression" value={expressionLabel(plan.selected_expression_kind)} />
          <Field label="Entry range" value={priceRange(plan.entry)} />
          <Field label="Entry limit" value={money(plan.entry_limit)} />
          <Field label="Cutoff" value={displayText(plan.cutoff)} />
          <Field label="Expiry" value={displayText(plan.expiry)} />
          <Field label="Quantity" value={numberValue(plan.quantity)} />
          <Field label="Maximum loss per unit" value={money(plan.max_loss_per_unit)} />
          <Field label="Planned loss" value={money(plan.planned_loss)} />
          <Field label="Invalidation" value={invalidation(plan.invalidation)} />
          <Field label="Profit exit" value={priceRange(plan.profit_exit)} />
        </dl>
      </section>

      {legs.length ? <OptionLegs legs={legs} /> : null}

      <section>
        <h3 className="text-sm font-semibold">Rationale</h3>
        <p className="mt-2 leading-6 text-muted-foreground">{decisionReason(displayText(plan.rationale))}</p>
      </section>

      <PortfolioImpactDetails impact={impact} />


    </div>
  );
}

function PortfolioImpactDetails({ impact }: { impact?: PortfolioImpact | null }) {
  if (!impact) return <p>Portfolio impact has not been calculated. Do not size this trade yet.</p>;
  return (
    <section className="space-y-4">
      <h3 className="text-sm font-semibold">Selected portfolio impact</h3>
      <ImpactSection title="Before and after exposure">
        <ImpactField label="Gross exposure before" value={numberValue(impact?.gross_exposure_before)} />
        <ImpactField label="Gross exposure after" value={numberValue(impact?.gross_exposure_after)} />
        <ImpactField label="Net exposure before" value={numberValue(impact?.net_exposure_before)} />
        <ImpactField label="Net exposure after" value={numberValue(impact?.net_exposure_after)} />
        <ImpactField label="Position weight before" value={numberValue(impact?.position_weight_before)} />
        <ImpactField label="Position weight after" value={numberValue(impact?.position_weight_after)} />
      </ImpactSection>
      <ImpactSection title="Concentration and shared risk">
        <ImpactField label="Symbol concentration delta" value={numberValue(impact?.symbol_concentration_delta)} />
        <ImpactField label="Sector concentration delta" value={numberValue(impact?.sector_concentration_delta)} />
        <ImpactField label="Beta delta" value={numberValue(impact?.beta_delta)} />
        <ImpactField label="Correlation cluster delta" value={numberValue(impact?.correlation_cluster_delta)} />
        <ImpactField label="Portfolio overlap penalty" value={numberValue(impact?.portfolio_overlap_penalty)} />
        <ImpactField label="Diversification benefit" value={numberValue(impact?.diversification_benefit)} />
        <ImpactField label="Positions most correlated" value={listValue(impact?.positions_most_correlated)} />
      </ImpactSection>
      <ImpactSection title="Loss and risk budget">
        <ImpactField label="Planned loss" value={money(impact?.planned_loss)} />
        <ImpactField label="Risk budget consumed" value={numberValue(impact?.risk_budget_consumed)} />
        <ImpactField label="Marginal risk" value={numberValue(impact?.marginal_risk)} />
        <ImpactField label="Tail risk penalty" value={numberValue(impact?.tail_risk_penalty)} />
      </ImpactSection>
      <ImpactSection title="Liquidity">
        <ImpactField label="ADV participation" value={numberValue(impact?.adv_participation)} />
        <ImpactField label="Days to exit" value={numberValue(impact?.days_to_exit)} />
        <ImpactField label="Expected transaction costs" value={money(impact?.expected_transaction_costs)} />
      </ImpactSection>
      <ImpactSection title="Stress and alternatives">
        <ImpactField label="Top alternative" value={displayText(impact?.top_alternative)} />
        <ImpactField label="Funding source or position to trim" value={displayText(impact?.funding_source_or_position_to_trim)} />
        <ImpactField label="Position to trim or replace" value={displayText(impact?.position_to_trim_or_replace)} />
      </ImpactSection>
      {impact?.blockers?.length ? <p>{impact.blockers.map(decisionReason).join(" ")}</p> : null}
    </section>
  );
}

function ImpactSection({ title, children }: { title: string; children: ReactNode }) {
  const fields = Children.toArray(children).filter((child) => isValidElement<{ value: string }>(child) && child.props.value !== "Not supplied");
  if (!fields.length) return null;
  return (
    <div>
      <h4 className="text-xs font-semibold uppercase tracking-[0.08em] text-muted-foreground">{title}</h4>
      <dl className="mt-2 grid min-w-0 gap-3 sm:grid-cols-2 xl:grid-cols-3">{fields}</dl>
    </div>
  );
}

function OptionLegs({ legs }: { legs: TradePlanLeg[] }) {
  return (
    <section>
      <h3 className="text-sm font-semibold">Stored option legs</h3>
      <ul className="mt-3 space-y-2">
        {legs.map((leg, index) => (
          <li key={`${legValue(leg, ["contract_id", "occ_symbol"])}-${index}`} className="min-w-0 rounded-md border border-border p-3">
            <dl className="grid min-w-0 gap-3 sm:grid-cols-2">
              <Field label="Contract" value={legValue(leg, ["contract_id", "occ_symbol"])} />
              <Field label="Side" value={legValue(leg, ["side"])} />
              <Field label="Option type" value={legValue(leg, ["option_type"])} />
              <Field label="Strike" value={legValue(leg, ["strike"])} />
              <Field label="Expiration" value={legValue(leg, ["expiration"])} />
              <Field label="Bid" value={legValue(leg, ["bid"])} source="option_quote" nextAction="Refresh the option quote before placing an order." />
              <Field label="Ask" value={legValue(leg, ["ask"])} source="option_quote" nextAction="Refresh the option quote before placing an order." />
            </dl>
          </li>
        ))}
      </ul>
    </section>
  );
}

function BlockedPlan({ plan, pending }: { plan?: TradePlan | null; pending: boolean }) {
  const missing = !plan;
  const state = missingFieldState({
    field: "trade_plan",
    source: pending ? "ticker_decision_snapshot" : missing ? "ticker_decision" : "canonical_trade_plan",
    reason: pending ? "trade_plan_snapshot_not_loaded" : missing ? "trade_plan_missing" : plan.primary_blocker || "trade_plan_required_field_missing",
    nextAction: pending ? "Load the full validated decision snapshot before acting." : missing ? "Refresh the ticker decision and publish its canonical TradePlan." : plan.next_action,
    availabilityStatus: pending ? "pending" : "missing",
  });
  return (
    <div className="min-w-0 p-4 text-sm">
      <p className="font-semibold">{pending ? "Loading trade details…" : "No new trade"}</p>
      <div className="mt-3"><DataFieldStateNotice state={state} /></div>
    </div>
  );
}

function ImpactField({ label, value }: { label: string; value: string }) {
  if (value === "Not supplied") return null;
  return <Field label={label} value={value} source="portfolio_impact" nextAction="Refresh the selected portfolio impact before sizing the trade." />;
}

function Field({ label, value, source = "canonical_trade_plan", nextAction = "Refresh the canonical TradePlan before acting." }: { label: string; value: string; source?: string; nextAction?: string }) {
  const missing = value === "Not supplied";
  return (
    <div className="min-w-0">
      <dt className="text-xs uppercase tracking-[0.08em] text-muted-foreground">{label}</dt>
      <dd className="mt-1 break-words font-medium text-foreground">{missing ? <DataFieldStateNotice compact state={missingFieldState({ field: fieldKey(label), source, reason: `${fieldKey(label)}_missing`, nextAction })} /> : value}</dd>
    </div>
  );
}

function isRenderableActionable(plan: TradePlan | null | undefined): plan is TradePlan {
  return Boolean(
    plan && plan.eligibility === "ACTIONABLE" && (plan.authorization_mode === "PAPER" || plan.authorization_mode === "ADVISORY")
    && plan.entry && plan.quantity != null && plan.max_loss_per_unit != null && plan.planned_loss != null
    && plan.invalidation && plan.profit_exit && plan.selected_expression_identity,
  );
}

function fieldKey(label: string): string {
  return label.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
}

function authorizationLabel(mode: string): "PAPER ONLY" | "ADVISORY" {
  return mode === "PAPER" ? "PAPER ONLY" : "ADVISORY";
}

function displayText(value: unknown): string {
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "Not supplied";
}

function numberValue(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString(undefined, { maximumFractionDigits: 20 }) : "Not supplied";
}

function listValue(value: string[] | null | undefined): string {
  return value?.length ? value.map(decisionReason).join(" ") : "Not supplied";
}

function money(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 20 })
    : "Not supplied";
}

function priceRange(value: PriceRange | null | undefined): string {
  if (!value) return "Not supplied";
  return value.low === value.high ? money(value.low) : `${money(value.low)}–${money(value.high)}`;
}

function invalidation(value: Invalidation | null | undefined): string {
  if (!value) return "Not supplied";
  return `${displayText(value.kind)} · ${displayText(value.statement)} · ${displayText(value.value)}`;
}

function legValue(leg: TradePlanLeg, keys: string[]): string {
  const record = leg as Record<string, unknown>;
  for (const key of keys) {
    const value = record[key];
    if (value !== undefined && value !== null && value !== "") return displayText(value);
  }
  return "Not supplied";
}
