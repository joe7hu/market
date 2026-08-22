import { describe, expect, it } from "vitest";

import { retryDelayForAttempt } from "./hooks";

describe("panel scope retry policy", () => {
  it("returns bounded delays and stops after the configured attempts", () => {
    const delays = [1000, 3000, 8000];

    expect(retryDelayForAttempt(0, delays)).toBe(1000);
    expect(retryDelayForAttempt(2, delays)).toBe(8000);
    expect(retryDelayForAttempt(3, delays)).toBeNull();
    expect(retryDelayForAttempt(-1, delays)).toBeNull();
  });

  it("rejects invalid delay values", () => {
    expect(retryDelayForAttempt(0, [Number.NaN])).toBeNull();
    expect(retryDelayForAttempt(0, [-1])).toBeNull();
  });
});
