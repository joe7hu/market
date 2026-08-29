import { DataTableFrame, StatusBadge } from "@/components/market/workstation";
import type { components } from "@/generated/apiSchema";

type TradePlan = components["schemas"]["TradePlan"];
type PriceRange = components["schemas"]["PriceRange"];
type Invalidation = components["schemas"]["Invalidation"];
type TradePlanLeg = NonNullable<TradePlan["selected_expression"]["legs"]>[number];

export function TradePlanCard({ plan }: { plan?: TradePlan | null }) {
  const actionable = isRenderableActionable(plan);
  return (
    <DataTableFrame
      title="Canonical trade plan"
      action={<StatusBadge tone={actionable ? "good" : "warn"}>{actionable ? authorizationLabel(plan.authorization_mode) : "NO TRADE"}</StatusBadge>}
    >
      {actionable ? <ActionablePlan plan={plan} /> : <BlockedPlan plan={plan} />}
    </DataTableFrame>
  );
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
          <Field label="Expression" value={displayText(plan.selected_expression_kind)} />
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
        <p className="mt-2 leading-6 text-muted-foreground">{displayText(plan.rationale)}</p>
      </section>

      <section>
        <h3 className="text-sm font-semibold">Selected portfolio impact</h3>
        <dl className="mt-3 grid min-w-0 gap-3 sm:grid-cols-2 xl:grid-cols-3">
          <Field label="Availability" value={displayText(impact?.availability)} />
          <Field label="Marginal risk" value={numberValue(impact?.marginal_risk)} />
          <Field label="Risk budget consumed" value={numberValue(impact?.risk_budget_consumed)} />
          <Field label="Diversification benefit" value={numberValue(impact?.diversification_benefit)} />
          <Field label="Position to trim or replace" value={displayText(impact?.position_to_trim_or_replace)} />
          <Field label="Positions most correlated" value={impact?.positions_most_correlated?.length ? impact.positions_most_correlated.join(", ") : "Unavailable"} />
        </dl>
      </section>

      <details className="rounded-md border border-border p-3">
        <summary className="cursor-pointer font-semibold">Provenance</summary>
        <dl className="mt-3 grid min-w-0 gap-3 sm:grid-cols-2">
          <Field label="Trade plan" value={displayText(plan.trade_plan_id)} />
          <Field label="Opportunity episode" value={displayText(plan.opportunity_episode_id)} />
          <Field label="Decision revision" value={displayText(plan.decision_revision)} />
          <Field label="Policy" value={displayText(plan.policy_version)} />
          <Field label="Rank" value={displayText(plan.rank_id)} />
          <Field label="Alpha signal" value={displayText(plan.alpha_signal_id)} />
          <Field label="Market snapshot" value={displayText(plan.market_snapshot_id)} />
          <Field label="Market state publication" value={displayText(plan.market_state_publication_id)} />
          <Field label="Publication" value={displayText(plan.publication_id)} />
          <Field label="Expression identity" value={displayText(plan.selected_expression_identity)} />
          <Field label="Portfolio impact" value={displayText(plan.portfolio_impact_id)} />
        </dl>
      </details>
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
              <Field label="Bid / ask" value={`${legValue(leg, ["bid"])} / ${legValue(leg, ["ask"])}`} />
            </dl>
          </li>
        ))}
      </ul>
    </section>
  );
}

function BlockedPlan({ plan }: { plan?: TradePlan | null }) {
  return (
    <div className="min-w-0 p-4 text-sm">
      <dl className="grid min-w-0 gap-3 sm:grid-cols-2">
        <Field label="State" value="NO TRADE" />
        <Field label="Expression" value="CASH" />
        <Field label="Primary blocker" value={displayText(plan?.primary_blocker)} />
        <Field label="Next action" value={displayText(plan?.next_action)} />
        {plan?.trade_plan_id ? <Field label="Plan" value={plan.trade_plan_id} /> : null}
      </dl>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs uppercase tracking-[0.08em] text-muted-foreground">{label}</dt>
      <dd className="mt-1 break-words font-medium text-foreground">{value}</dd>
    </div>
  );
}

function isRenderableActionable(plan: TradePlan | null | undefined): plan is TradePlan {
  return Boolean(plan && plan.eligibility === "ACTIONABLE" && (plan.authorization_mode === "PAPER" || plan.authorization_mode === "ADVISORY"));
}

function authorizationLabel(mode: string): "PAPER ONLY" | "ADVISORY" {
  return mode === "PAPER" ? "PAPER ONLY" : "ADVISORY";
}

function displayText(value: unknown): string {
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "Unavailable";
}

function numberValue(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString(undefined, { maximumFractionDigits: 20 }) : "Unavailable";
}

function money(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 20 })
    : "Unavailable";
}

function priceRange(value: PriceRange | null | undefined): string {
  if (!value) return "Unavailable";
  return value.low === value.high ? money(value.low) : `${money(value.low)}–${money(value.high)}`;
}

function invalidation(value: Invalidation | null | undefined): string {
  if (!value) return "Unavailable";
  return `${displayText(value.kind)} · ${displayText(value.statement)} · ${displayText(value.value)}`;
}

function legValue(leg: TradePlanLeg, keys: string[]): string {
  const record = leg as Record<string, unknown>;
  for (const key of keys) {
    const value = record[key];
    if (value !== undefined && value !== null && value !== "") return displayText(value);
  }
  return "Unavailable";
}
