import { useCallback, useEffect, useRef, useState } from "react";
import { loadRefreshJobs, startRefreshJob, type RefreshJob, type RefreshJobsPayload } from "../api";
import { useMarketData } from "../marketData";
import { WatchlistPage } from "@/views/watchlist";

const CANDIDATE_PAGE_SIZE = 80;
const WATCHLIST_REFRESH_JOB = "full_market_refresh";
const POLL_INTERVAL_MS = 5000;
// Pull fresh rows + the latest refresh status on a timer so a tab left open
// reflects the backend scheduler's periodic refreshes without a manual click.
const AUTO_REFRESH_INTERVAL_MS = 120000;

export type WatchlistRefreshStatus = "idle" | "starting" | "running" | "succeeded" | "failed";

export function WatchlistRoute() {
  const { data, loadScope, openTicker } = useMarketData();
  const activeRefreshJobId = useRef<string | null>(null);
  const [refreshStatus, setRefreshStatus] = useState<WatchlistRefreshStatus>("idle");
  const [refreshFinishedAt, setRefreshFinishedAt] = useState<Date | null>(null);
  const [refreshError, setRefreshError] = useState<string | null>(null);

  const loadWatchlist = useCallback(async () => {
    await loadScope("watchlist-watched");
  }, [loadScope]);
  const loadUnwatchedPage = useCallback(async (offset: number) => {
    await loadScope("watchlist-unwatched", { offset, limit: CANDIDATE_PAGE_SIZE, append: offset > 0 });
  }, [loadScope]);
  const reloadVisibleWatchlist = useCallback(async () => {
    await loadWatchlist();
    await loadUnwatchedPage(0);
  }, [loadWatchlist, loadUnwatchedPage]);

  const updateRefreshState = useCallback(async (payload: RefreshJobsPayload, targetJobId: string | null = activeRefreshJobId.current) => {
    const latestJob = latestFullRefreshJob(payload.rows ?? []);
    const targetJob = targetJobId ? (payload.rows ?? []).find((job) => job.id === targetJobId) : latestJob;
    const latestFinishedAt = latestRefreshFinishedAt(payload, latestJob);
    if (latestFinishedAt) {
      setRefreshFinishedAt(latestFinishedAt);
    }
    if (!targetJob) return;
    if (!targetJobId && latestStatusIsNewerThanJob(payload, targetJob)) {
      setRefreshStatus("idle");
      setRefreshError(null);
      return;
    }
    if (targetJob.status === "running") {
      setRefreshStatus("running");
      return;
    }
    if (targetJob.status === "failed") {
      activeRefreshJobId.current = null;
      setRefreshStatus("failed");
      setRefreshError(targetJob.error || "Refresh failed");
      return;
    }
    if (targetJob.status === "succeeded") {
      const failure = refreshFailureMessage(targetJob);
      if (failure) {
        activeRefreshJobId.current = null;
        setRefreshStatus("failed");
        setRefreshError(failure);
        return;
      }
      activeRefreshJobId.current = null;
      setRefreshStatus("succeeded");
      setRefreshError(null);
      setRefreshFinishedAt(parseDate(targetJob.finished_at) ?? latestFinishedAt ?? new Date());
      await reloadVisibleWatchlist();
    }
  }, [reloadVisibleWatchlist]);

  const refreshJobStatus = useCallback(async (targetJobId: string | null = activeRefreshJobId.current) => {
    const payload = await loadRefreshJobs();
    await updateRefreshState(payload, targetJobId);
  }, [updateRefreshState]);

  const startManualRefresh = useCallback(async () => {
    if (refreshStatus === "starting" || refreshStatus === "running") return;
    setRefreshStatus("starting");
    setRefreshError(null);
    try {
      const job = await startRefreshJob(WATCHLIST_REFRESH_JOB);
      activeRefreshJobId.current = job.id ?? null;
      setRefreshStatus("running");
    } catch (error) {
      activeRefreshJobId.current = null;
      setRefreshStatus("failed");
      setRefreshError(error instanceof Error ? error.message : "Refresh failed");
    }
  }, [refreshStatus]);

  useEffect(() => {
    void loadWatchlist().catch(() => undefined);
  }, [loadWatchlist]);

  useEffect(() => {
    void refreshJobStatus(null).catch(() => undefined);
  }, [refreshJobStatus]);

  useEffect(() => {
    if (refreshStatus !== "running") return;
    const intervalId = window.setInterval(() => {
      void refreshJobStatus().catch(() => undefined);
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(intervalId);
  }, [refreshJobStatus, refreshStatus]);

  useEffect(() => {
    const intervalId = window.setInterval(() => {
      // While a manual refresh is in flight the running-poll above already
      // reloads on success; avoid double-loading and clobbering its state.
      if (refreshStatus === "starting" || refreshStatus === "running") return;
      void refreshJobStatus(null).catch(() => undefined);
      void loadWatchlist().catch(() => undefined);
    }, AUTO_REFRESH_INTERVAL_MS);
    return () => window.clearInterval(intervalId);
  }, [loadWatchlist, refreshJobStatus, refreshStatus]);

  return (
    <WatchlistPage
      data={data}
      refreshStatus={refreshStatus}
      refreshFinishedAt={refreshFinishedAt}
      refreshError={refreshError}
      onManualRefresh={startManualRefresh}
      onOpenTicker={openTicker}
      onRefresh={loadWatchlist}
      onLoadUnwatchedPage={loadUnwatchedPage}
    />
  );
}

function latestFullRefreshJob(jobs: RefreshJob[]): RefreshJob | null {
  return jobs
    .filter((job) => job.job_name === WATCHLIST_REFRESH_JOB)
    .sort((a, b) => (parseDate(b.started_at)?.getTime() ?? 0) - (parseDate(a.started_at)?.getTime() ?? 0))[0] ?? null;
}

function latestRefreshFinishedAt(payload: RefreshJobsPayload, latestJob: RefreshJob | null): Date | null {
  const fromJob = latestJob?.status === "succeeded" && !refreshFailureMessage(latestJob) ? parseDate(latestJob.finished_at) : null;
  const status = payload.latest_status;
  const fromStatus = status && statusDataIsFresh(status) && (status.job === WATCHLIST_REFRESH_JOB || status.dataFinishedAt || status.finishedAt)
    ? parseDate(status.dataFinishedAt ?? status.finishedAt)
    : null;
  if (fromJob && fromStatus) {
    return fromJob.getTime() >= fromStatus.getTime() ? fromJob : fromStatus;
  }
  return fromJob ?? fromStatus ?? null;
}

// Data freshness ignores the housekeeping tail (snapshot/prune): a snapshot
// failure still leaves the panel's data refreshed. Prefer the backend's dataOk
// flag and fall back to the overall outcome for older status payloads.
function statusDataIsFresh(status: NonNullable<RefreshJobsPayload["latest_status"]>): boolean {
  if (typeof status.dataOk === "boolean") return status.dataOk;
  return status.ok !== false && status.status !== "failed";
}

function latestStatusIsNewerThanJob(payload: RefreshJobsPayload, job: RefreshJob): boolean {
  const status = payload.latest_status;
  if (!status || !statusDataIsFresh(status)) return false;
  const statusFinishedAt = parseDate(status.dataFinishedAt ?? status.finishedAt);
  if (!statusFinishedAt) return false;
  const jobTime = parseDate(job.finished_at) ?? parseDate(job.started_at);
  return Boolean(jobTime && statusFinishedAt.getTime() > jobTime.getTime());
}

function refreshFailureMessage(job: RefreshJob): string | null {
  const summary = isRecord(job.summary) ? job.summary : null;
  if (!summary) return null;
  // Data refreshed successfully; only the housekeeping tail failed — not a
  // watchlist-facing failure.
  if (summary.dataOk === true) return null;
  const ok = summary.ok;
  const status = summary.status;
  if (ok !== false && status !== "failed") return null;
  const failedStep = typeof summary.failedStep === "string" ? summary.failedStep : null;
  const error = typeof summary.error === "string" ? summary.error : null;
  return error || (failedStep ? `Refresh failed at ${failedStep}` : "Refresh failed");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function parseDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}
