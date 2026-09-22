import { describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { renderToStaticMarkup } from "react-dom/server";
import type { PaperObservationHistory } from "@/api/paper";
import { CapitalDecision } from "./CapitalDecision";
import { ExperimentHistory, experimentSegments } from "./ExperimentCurve";
import { paperEventTooltip } from "./PaperPerformanceChart";
import { ForecastCollection, CollectionProgress, PromptPipeline } from "@/pages/ResearchWorkbenchRoute";

type Event = NonNullable<PaperObservationHistory["events"]>[number];
const event = (kind: Event["kind"], at: string, pnl: number | null): Event => ({
  event_key: kind + at, kind, at, quote_observed_at: at, price: pnl == null ? null : .5,
  net_pnl: pnl, net_return: pnl == null ? null : pnl / 50, fees: pnl == null ? null : 1.3,
  reason: kind === "mark_gap" ? "quote_stale" : null,
});
const events = [event("entry", "2026-09-21T14:00:00Z", -3.3), event("mark", "2026-09-21T14:01:00Z", 0),
  event("mark_gap", "2026-09-21T14:07:00Z", null), event("exit", "2026-09-21T14:08:00Z", 58.7)];
const history: PaperObservationHistory = { observation_id: "test", symbol: "RBLX", status: "closed",
  as_of: "2026-09-21T14:10:00Z", events, truncated: false, coverage: "recorded_events",
  basis: "One-contract experiment, not funded paper NAV", entry_at: events[0].at, entry_price: .5,
  exit_at: events[3].at, exit_price: 1.1, quantity: 1, multiplier: 100 };

describe("decision loop presentation", () => {
  it("uses the current plan and never revives a stale resolution blocker", () => {
    const html = renderToStaticMarkup(<CapitalDecision plan={{ eligibility: "ACTIONABLE", authorization_mode: "PAPER", action: "BUY", primary_blocker: null, rationale: "Qualified positive utility", next_action: "Stage the published paper terms" }} resolution={{ primary_blocker: "trade_plan_missing", rationale: "Old missing evidence", next_action: "Refresh old publication" }} />);
    expect(html).toContain("PAPER — BUY"); expect(html).toContain("Qualified positive utility");
    expect(html).not.toContain("Old missing"); expect(html).not.toContain("Refresh old"); expect(html).not.toContain("trade_plan_missing");
  });
  it("explains a blocked CASH plan without pretending price conditions authorize it", () => {
    const html = renderToStaticMarkup(<CapitalDecision plan={{ eligibility: "BLOCKED", authorization_mode: "PAPER", primary_blocker: "Insufficient independent outcomes", rationale: "No qualified stock model passed validation", next_action: "Collect the pending outcomes" }} />);
    expect(html).toContain("WAIT — retain cash"); expect(html).toContain("No qualified stock model"); expect(html).not.toContain("publication failed");
  });
  it("names absent decision publication as an operational failure, not a market verdict", () => {
    const html = renderToStaticMarkup(<CapitalDecision missingIsFailure resolution={{ primary_blocker: "account_snapshot_missing" }} />);
    expect(html).toContain("TRADING PAUSED"); expect(html).toContain("refresh_decision_models"); expect(html).toContain('href="/health"');
  });
  it("keeps a published no-trade decision out of the publication-failure state", () => {
    const html = renderToStaticMarkup(<CapitalDecision resolution={{ lifecycle: "PUBLISHED", primary_blocker: "trade_plan_missing" }} />);
    expect(html).toContain("WAIT — retain cash"); expect(html).not.toContain("publication failed");
  });
  it("plots zero honestly and breaks history across quote gaps and invalid clocks", () => {
    const segments = experimentSegments([...events, event("mark", "bad", 99)]);
    expect(segments.map(segment => segment.map(point => point.net_pnl))).toEqual([[-3.3, 0], [58.7]]);
    const html = renderToStaticMarkup(<ExperimentHistory history={history} />);
    expect(html).toContain("RBLX net experiment P&amp;L"); expect(html).toContain("$58.70");
    expect(html).toContain("$0.50"); expect(html).toContain("$1.10"); expect(html).toContain("Gap — not zero"); expect(html).toContain("Source quote time");
    expect((html.match(/<polyline/g) ?? []).length).toBe(2);
  });
  it("never reconstructs an old observation's missing history", () => {
    const html = renderToStaticMarkup(<ExperimentHistory history={{ ...history, events: [], coverage: "snapshot_only" }} />);
    expect(html).toContain("predates the event journal"); expect(html).toContain("$0.50"); expect(html).not.toContain("<svg");
  });
  it("keeps exact trade prices and quantity in the funded chart tooltip", () => {
    const tooltip = paperEventTooltip({ kind: "entry", symbol: "RBLX", at: events[0].at, price: .5, quantity: 1, pnl: null });
    expect(tooltip).toContain("Price: $0.50 · Quantity: 1"); expect(tooltip).toContain("P&L: not verified"); expect(tooltip).not.toContain("<br");
  });
  it("reports pending claim settlement separately from paused generation", () => {
    const html = renderToStaticMarkup(<ForecastCollection lane={{ generation_enabled: false, settlement_enabled: true, pause_reason: "Existing forecasts still settle" }} quality={{ total_claims: 8, pending_claims: 5, valid_resolved_claims: 3 }} />);
    expect(html).toContain("New forecast generation is paused"); expect(html).toContain("8 issued · 5 pending · 3 independently resolved"); expect(html).toContain("Existing forecasts still settle");
  });
  it("shows entered experiments without claiming they are independent validation", () => {
    const html = renderToStaticMarkup(<MemoryRouter><CollectionProgress collection={{ counts: { entered: 18, pending: 2, closed: 1 }, management_status: "collecting", basis: "Open observations are not independent evidence" }} /></MemoryRouter>);
    expect(html).toContain("18"); expect(html).toContain("Research collection"); expect(html).toContain("independent");
  });
  it("does not claim settlement is running when both forecast controls are paused", () => {
    const lane = { status: "disabled", generation_enabled: false, settlement_enabled: false };
    const html = renderToStaticMarkup(<MemoryRouter><PromptPipeline lane={lane} quality={{}} /><ForecastCollection lane={lane} quality={{ total_claims: 8, pending_claims: 8 }} /></MemoryRouter>);
    expect(html).toContain("Forecast settlement is paused");
    expect(html).toContain("Settlement is disabled");
    expect(html).not.toContain("existing outcomes still settle");
  });
  it("never presents a failed collection read as zero observations", () => {
    const html = renderToStaticMarkup(<MemoryRouter><CollectionProgress collection={{ management_status: "unavailable" }} /></MemoryRouter>);
    expect(html).toContain("could not be read");
    expect(html).not.toContain("0 open");
    expect(html).toContain('href="/health"');
  });

});
