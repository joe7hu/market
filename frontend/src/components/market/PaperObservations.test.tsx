import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import type { PaperObservation } from "@/api/paper";
import { PaperObservationRow } from "./PaperObservations";

const observation: PaperObservation = {
  id: "observation-1", symbol: "AMD", status: "rejected", strategy: "Test strategy", structure: "long_call",
  decision_id: "decision-1", decision_at: "2026-09-18T14:00:00Z", created_at: "2026-09-18T14:00:01Z",
  entry_at: null, exit_at: null, entry_price: null, exit_price: null, net_return: null,
  reason: "candidate_gate_rejected", exit_reason: null, entry_deadline: null,
  blockers: [], thesis_summary: null, required_next_action: "Review the rejected setup",
  ticket: {}, entry_quotes: null, exit_quotes: null,
};
const render = (row = observation) => renderToStaticMarkup(<MemoryRouter><PaperObservationRow row={row} /></MemoryRouter>);

describe("paper observation rows", () => {
  it("links company evidence to the registered ticker route", () => {
    expect(render()).toContain('href="/tickers/AMD"');
    expect(render()).not.toContain('href="/ticker/AMD"');
  });
  it("keeps the next action visible without expanding the row", () => {
    const summary = render().split("</summary>")[0];
    expect(summary).toContain("Review the rejected setup");
  });
  it("does not display meaningless entry and exit prices for a rejected observation", () => {
    const html = render();
    expect(html).not.toContain("Entry quote per share");
    expect(html).not.toContain("Exit quote per share");
  });
  it("does not hide required missing fill evidence on a supposedly closed observation", () => {
    const html = render({ ...observation, status: "closed" });
    expect(html).toContain("Entry price evidence missing");
    expect(html).toContain("Exit price evidence missing");
  });
});
