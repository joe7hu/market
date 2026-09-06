import { beforeEach, expect, it, vi } from "vitest";

// Exercise the provider's async workflow without a DOM or an extra renderer.
const harness = vi.hoisted(() => ({ slots: [] as any[], cursor: 0, request: vi.fn() }));
vi.mock("react", async (original) => ({
  ...await original<typeof import("react")>(),
  useState: (initial: any) => {
    const index = harness.cursor++;
    if (!(index in harness.slots)) harness.slots[index] = typeof initial === "function" ? initial() : initial;
    return [harness.slots[index], (value: any) => { harness.slots[index] = typeof value === "function" ? value(harness.slots[index]) : value; }];
  },
  useRef: (value: any) => {
    const index = harness.cursor++;
    return harness.slots[index] ??= { current: value };
  },
  useCallback: (value: any) => value,
  useMemo: (factory: any) => factory(),
}));
vi.mock("react-router-dom", () => ({ useNavigate: () => vi.fn() }));
vi.mock("./apiTransport", () => ({ getJson: harness.request, patchJson: vi.fn(), sendJson: vi.fn() }));
vi.mock("./model", () => ({ buildModel: () => ({}) }));

import { MarketDataProvider } from "./marketData";

function render() {
  harness.cursor = 0;
  return MarketDataProvider({ children: null }).props.value;
}
function deferred() {
  let resolve!: (value: any) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
beforeEach(() => { harness.slots = []; harness.request.mockReset(); });

it.each([false, true])("keeps fresh independent scopes when responses complete in reverse order=%s", async (reverse) => {
  const market = deferred();
  const calendar = deferred();
  harness.request.mockImplementation((url: string) => url.includes("scope=market") ? market.promise : calendar.promise);
  const context = render();
  const first = context.loadScope("market");
  const second = context.loadScope("calendar");
  expect(render().loading).toBe(true);
  const finishMarket = () => market.resolve({ scope: "market", tables: { quotes: { rows: [{ ticker: "NEW" }] } } });
  const finishCalendar = () => calendar.resolve({ scope: "calendar", tables: { catalysts: { rows: [{ ticker: "EVENT" }] } } });
  (reverse ? finishCalendar : finishMarket)();
  await (reverse ? second : first);
  expect(render().loading).toBe(true);
  (reverse ? finishMarket : finishCalendar)();
  await Promise.all([first, second]);
  const result = render();
  expect(result.data.quotes.rows).toEqual([{ ticker: "NEW" }]);
  expect(result.data.catalysts.rows).toEqual([{ ticker: "EVENT" }]);
  expect(result.scopeStatus.market.state).toBe("ready");
  expect(result.scopeStatus.calendar.state).toBe("ready");
  expect(result.loading).toBe(false);
});

it("deduplicates a pending request and clears loading after failure without losing data", async () => {
  const pending = deferred();
  harness.request.mockReturnValue(pending.promise);
  const context = render();
  const first = context.loadScope("market");
  const duplicate = context.loadScope("market");
  const checks = Promise.allSettled([first, duplicate]);
  expect(harness.request).toHaveBeenCalledTimes(1);
  pending.reject(new Error("offline"));
  await checks;
  expect(render().loading).toBe(false);
  expect(render().scopeStatus.market).toMatchObject({ state: "failed", error: "offline" });
});
