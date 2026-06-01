import { useCallback, useEffect, useRef, useState } from "react";
import { loadRefreshJobs, startRefreshJob, type RefreshJob, type RefreshJobsPayload } from "../api";
import { useMarketData } from "../marketData";
import { WatchlistPage } from "@/views/watchlist";

const CANDIDATE_PAGE_SIZE = 80;
const WATCHLIST_REFRESH_JOB = "full_market_refresh";
const POLL_INTERVAL_MS = 5000;

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
  const fromStatus = (payload.latest_status?.job === WATCHLIST_REFRESH_JOB || payload.latest_status?.finishedAt) && payload.latest_status?.ok !== false && payload.latest_status?.status !== "failed"
    ? parseDate(payload.latest_status?.finishedAt)
    : null;
  if (fromJob && fromStatus) {
    return fromJob.getTime() >= fromStatus.getTime() ? fromJob : fromStatus;
  }
  return fromJob ?? fromStatus ?? null;
}

function latestStatusIsNewerThanJob(payload: RefreshJobsPayload, job: RefreshJob): boolean {
  if (payload.latest_status?.ok === false || payload.latest_status?.status === "failed") return false;
  const statusFinishedAt = parseDate(payload.latest_status?.finishedAt);
  if (!statusFinishedAt) return false;
  const jobTime = parseDate(job.finished_at) ?? parseDate(job.started_at);
  return Boolean(jobTime && statusFinishedAt.getTime() > jobTime.getTime());
}

function refreshFailureMessage(job: RefreshJob): string | null {
  const summary = isRecord(job.summary) ? job.summary : null;
  if (!summary) return null;
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
