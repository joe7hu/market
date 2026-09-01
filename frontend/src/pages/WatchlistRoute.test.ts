import { describe, expect, it } from "vitest";

import { latestWatchlistRefreshFinishedAt, latestWatchlistRefreshJob } from "./WatchlistRoute";

describe("latestWatchlistRefreshJob", () => {
  it("uses the newest relevant data refresh instead of an older full refresh", () => {
    const job = latestWatchlistRefreshJob([
      { job_name: "full_market_refresh", status: "running", started_at: "2026-07-24T20:00:00Z" },
      { job_name: "update_market_data", status: "succeeded", started_at: "2026-07-24T21:00:00Z" },
    ]);

    expect(job?.job_name).toBe("update_market_data");
  });
});

describe("latestWatchlistRefreshFinishedAt", () => {
  it("uses the latest successful market-data job instead of legacy status metadata", () => {
    const finishedAt = latestWatchlistRefreshFinishedAt([
      { job_name: "full_market_refresh", status: "failed", finished_at: "2026-06-17T01:49:24Z" },
      { job_name: "update_market_data", status: "succeeded", finished_at: "2026-07-24T21:41:50Z" },
      { job_name: "update_social_sources", status: "succeeded", finished_at: "2026-07-24T22:12:15Z" },
    ]);

    expect(finishedAt?.toISOString()).toBe("2026-07-24T21:41:50.000Z");
  });

  it("does not claim freshness when every watchlist data job failed", () => {
    const finishedAt = latestWatchlistRefreshFinishedAt([
      { job_name: "update_market_data", status: "failed", finished_at: "2026-07-24T21:41:50Z" },
      { job_name: "update_social_sources", status: "succeeded", finished_at: "2026-07-24T22:12:15Z" },
    ]);

    expect(finishedAt).toBeNull();
  });
});
