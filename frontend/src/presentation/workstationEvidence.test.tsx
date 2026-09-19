import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { calibrationPoints } from "./prediction";
import { strategyProgress, strategySteps } from "./strategy";
import { learningProgress } from "./lifecycle";
import { operationCount } from "@/components/market/WorkflowReadiness";
import { navSegments } from "@/components/market/PaperAccountCurve";
import { Phase2Evidence } from "@/views/market/panels";

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
