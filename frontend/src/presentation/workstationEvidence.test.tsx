import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { calibrationPoints } from "./prediction";
import { strategyProgress, strategySteps } from "./strategy";
import { learningProgress } from "./lifecycle";
import { operationCount } from "@/components/market/WorkflowReadiness";
import { navSegments } from "@/components/market/PaperAccountCurve";
import { ComparisonVisual } from "@/pages/ResearchWorkbenchRoute";
import { numberField } from "@/shared/rowFormat";
import { numeric } from "@/views/market/format";
import { entrySwitchLabel } from "@/components/market/WorkflowReadiness";
import { ValuationContext, IndexBaseline, Phase2Evidence } from "@/views/market/panels";

describe("evidence cannot be fabricated by presentation", () => {
  it("keeps real zero probabilities but rejects missing or invalid bins", () => {
    const good = { bin: 0, mean_predicted: 0, observed_rate: 0, sample_count: 10 };
    expect(calibrationPoints({ calibration_bins: [good, {}, { ...good, mean_predicted: null },
      { ...good, observed_rate: NaN }, { ...good, mean_predicted: 1.1 }, { ...good, sample_count: 0 }] })).toHaveLength(1);
    expect(calibrationPoints({ calibration_bins: [good] })[0].predicted).toBe(0);
  });
  it("does not count evaluations or attempts as independent historical outcomes", () => {
    const progress = strategyProgress({ evidence_counts: { evaluations: 40000 }, historical_required: 100 });
    expect(progress.historical.state).toBe("unavailable");
    expect(progress.historical.percent).toBe(0);
  });
  it("meeting a sample floor does not manufacture a passing evaluation", () => {
    const steps = strategySteps({ progress: { historical_completed: 100, historical_required: 100, historical_verdict: "fail" } });
    expect(steps[1].state).not.toBe("complete");
    expect(learningProgress(Infinity, 30).state).toBe("unavailable");
    expect(learningProgress(null, 30).state).toBe("unavailable");
  });
  it("failed count reads are not zero", () => {
    expect(operationCount({ status: "unavailable", counts: {} }, ["entered"])).toBe("Not available");
    expect(operationCount({ status: "available", counts: {} }, ["entered"])).toBe("0");
  });
  it("does not interpolate NAV through a missing or malformed mark", () => {
    const point = (at: string, nav: number | null, status = "complete") => ({ at, nav, status, net_pnl: null });
    const points = [point("2026-09-18T10:00:00Z", 100), point("2026-09-18T11:00:00Z", null, "incomplete"),
      point("2026-09-18T12:00:00Z", 101), point("2026-09-19T10:00:00Z", 102), point("invalid", 103)];
    expect(navSegments(points).map(segment => segment.length)).toEqual([1, 1, 1]);
  });
  it("an absent advanced model does not diagnose missing history", () => {
    const html = renderToStaticMarkup(<Phase2Evidence posteriorRows={[]} coverageVectorRows={[]} scenarioRows={[]} optionSlaRows={[]} observationRows={[]} />);
    expect(html).not.toContain("MISSING_HISTORY");
    expect(html).toContain("No advanced-model evidence");
  });
});

describe("hourly NAV gaps", () => {
  const point = (at: string) => ({ at, nav: 100, status: "complete", net_pnl: 0 });
  it("does not bridge a completely missing hour even when timestamps are only 65 minutes apart", () => {
    expect(navSegments([point("2026-09-18T10:55:00Z"), point("2026-09-18T12:00:00Z")])).toHaveLength(2);
  });
  it("keeps adjacent hourly samples and separates reversed time", () => {
    expect(navSegments([point("2026-09-18T10:01:00Z"), point("2026-09-18T11:59:00Z")])).toHaveLength(1);
    expect(navSegments([point("2026-09-18T11:59:00Z"), point("2026-09-18T10:01:00Z")])).toHaveLength(2);
  });
});

describe("observed context and signed comparisons", () => {
  it("uses an actual valuation reference, not a missing model score", () => {
    const html = renderToStaticMarkup(<ValuationContext rows={[{ metric: "equity_risk_premium", label: "Equity risk premium", latest_value: 0, suffix: "%", latest_date: "2026-09-18" }]} />);
    expect(html).toContain("0.00%");
    expect(html).toContain("Context, not a score");
    expect(html).not.toContain("/ 100");
  });
  it("shows available index evidence even when no broad tracked-universe model exists", () => {
    const html = renderToStaticMarkup(<IndexBaseline rows={[{ symbol: "SPY", price: 123, return_1d: 0, as_of: "2026-09-18", source: "test" }]} />);
    expect(html).toContain("SPY");
    expect(html).toContain("$123.00");
    expect(html).toContain("independent of tracked-universe model coverage");
  });
  it("does not turn whitespace or formatting-only fields into zero", () => {
    for (const value of ["", "  ", "$", "%", " ,_ "]) {
      expect(Number.isNaN(numberField({ score: value }, ["score"], NaN))).toBe(true);
      expect(numeric(value)).toBeUndefined();
    }
    expect(numberField({ score: "0" }, ["score"], NaN)).toBe(0);
  });
  it("does not report unread entry permission as disabled", () => {
    expect(entrySwitchLabel(undefined)).toContain("could not be read");
    expect(entrySwitchLabel(false)).toContain("disabled");
  });
  it("draws losses left of zero and avoids duplicate P&L aliases", () => {
    const html = renderToStaticMarkup(<ComparisonVisual row={{ metrics: { baseline: { net_pnl: -100, pnl: 999 }, challenger: { net_pnl: 200, pnl: 999 } } }} />);
    expect(html).toContain("right:50%;width:25%");
    expect(html).toContain("left:50%;width:50%");
    expect(html).not.toContain("999");
    expect(html.match(/font-medium">P&amp;L/g)).toHaveLength(1);
  });
});
