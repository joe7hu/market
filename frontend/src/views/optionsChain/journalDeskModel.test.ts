import { describe, expect, it } from "vitest";

import type { OptionsPaperJournalRow } from "@/api/options";
import { buildJournalDeskModel, observationLabel, researchBlockerLabel } from "./journalDeskModel";

function observation(overrides: Partial<OptionsPaperJournalRow>): OptionsPaperJournalRow {
  return {
    record_kind: "shadow_observation",
    paper_order_id: null,
    shadow_id: "shadow-1",
    decision_id: "decision-1",
    lifecycle: "pending",
    structure: "long_call",
    entry_at: null,
    conservative_entry_price: null,
    conservative_fill_basis: "pending_next_cohort",
    latest_mark: null,
    missing_mark_gap: false,
    current_return: null,
    outcome_state: null,
    pending_entry_reason: "next_valid_cohort_required",
    assignment_warning: null,
    admission: {
      decision_at: "2026-07-30T09:45:00-04:00",
      decision_state: "WATCH",
      paper_state: "WATCH",
      discovery_lane: "anomaly",
      reasons: [],
      blockers: ["thesis_direction_required"],
      model_revision: "r3",
      market_regime: "above_200d:normal",
    },
    contract: { expiration: "2026-09-04", strike: 685, option_type: "call", multiplier: 100, legs: [] },
    thesis: { revision: 2, direction: "neutral", core_thesis: "No directional edge.", invalidation: "Reassess", horizon_date: "2026-12-31" },
    forecast: {
      probability_profit: 0.4, expected_value: 25, lower_95_expected_value: -10, max_loss: 500,
      risk_adjusted_expectancy: null, modeled_net_edge: 0.1, fair_value_low: 4, fair_value_high: 6,
      scenario_count: 26, data_confidence: 0.98, execution_confidence: 0.9,
    },
    execution: {
      staged_at: null, signal_quote_at: null, entry_cohort_id: null, entry_at: null,
      entry_price: null, fill_basis: null, latest_mark: null, exit_at: null, exit_price: null,
      holding_period_hours: null,
    },
    outcome: {
      state: null, observed_through: null, current_return: null, return_1d: null, return_5d: null,
      return_20d: null, return_60d: null, peak_return: null, max_drawdown: null,
      realized_exit_return: null, realized_exit_basis: null,
      attribution: { underlying: null, iv: null, theta: null, spread: null, unexplained: null },
    },
    metrics: {},
    ...overrides,
  };
}

describe("journal desk model", () => {
  it("summarizes current experiments without hiding research rows", () => {
    const pending = observation({});
    const tracking = observation({
      shadow_id: "shadow-2",
      lifecycle: "observing",
      latest_mark: 6,
      current_return: 0.12,
    });
    const model = buildJournalDeskModel({
      journal: [],
      journalCount: 0,
      shadow: [pending, tracking],
      shadowCount: 3,
    });

    expect(model.paperStatus).toBe("No paper track record yet");
    expect(model.currentExperiments).toBe(3);
    expect(model.tracking).toBe(1);
    expect(model.awaitingEntry).toBe(1);
    expect(model.marked).toBe(1);
    expect(observationLabel(pending)).toBe("Awaiting next quote");
    expect(observationLabel(tracking)).toBe("Tracking path");
    expect(researchBlockerLabel("thesis_direction_required")).toBe("Neutral thesis — no directional trade");
    expect(researchBlockerLabel("thesis_upgrade_required")).toBe("Thesis revision required");
  });
});
