import { describe, expect, it, vi } from "vitest";

import { CANDIDATE_PAGE_SIZE, loadVisibleWatchlistScopes } from "./watchlistScopeLoader";

describe("loadVisibleWatchlistScopes", () => {
  it("loads watched symbols and the first candidate page", async () => {
    const loadScope = vi.fn().mockResolvedValue(undefined);

    await loadVisibleWatchlistScopes(loadScope);

    expect(loadScope.mock.calls).toEqual([
      ["watchlist-watched"],
      ["watchlist-unwatched", { offset: 0, limit: CANDIDATE_PAGE_SIZE, append: false }],
    ]);
  });
});
