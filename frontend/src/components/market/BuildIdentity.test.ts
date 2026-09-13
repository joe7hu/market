import { describe, expect, it } from "vitest";
import { buildIdsMatch } from "./BuildIdentity";

describe("build identity", () => {
  it("accepts a short UI SHA when it names the full API SHA", () => {
    expect(buildIdsMatch("2608de8", "2608de8406d19d98f5037c0b02b1469b0ac3af0e")).toBe(true);
  });

  it("rejects unrelated build identities", () => {
    expect(buildIdsMatch("2608de8", "e524be32d32e6b90b0f1a60108e62207ad06ad07")).toBe(false);
  });
});
