import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ExecutionEvidencePanel } from "@/views/ticker/panels";

describe("ExecutionEvidencePanel Phase 6 evidence", () => {
  it("renders backend-owned execution fields without rebuilding policy", () => {
    const html = renderToStaticMarkup(<ExecutionEvidencePanel executionEvidence={{
      status: "available",
      version: "execution-grade.v1",
      freshness_status: "available",
      delta: 0.42,
      gamma: 0.08,
      funding: 0.001,
      basis: 0.002,
      blockers: [],
    }} />);

    for (const text of ["available", "execution-grade.v1", "Freshness", "0.42", "0.08", "0.001", "0.002"]) {
      expect(html).toContain(text);
    }
  });
});
