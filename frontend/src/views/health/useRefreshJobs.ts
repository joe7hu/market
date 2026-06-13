import { useCallback, useEffect, useMemo, useState } from "react";

import { loadRefreshJobs, startRefreshJob, type RefreshJobsPayload } from "@/api";
import { latestByJob } from "@/views/health/aggregate";

export type UseRefreshJobs = ReturnType<typeof useRefreshJobs>;

export function useRefreshJobs() {
  const [payload, setPayload] = useState<RefreshJobsPayload | null>(null);
  const [pendingJobs, setPendingJobs] = useState<Set<string>>(() => new Set());
  const [startError, setStartError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setPayload(await loadRefreshJobs());
    } catch {
      // Keep the last good payload; the reload button can retry.
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const jobRows = payload?.rows ?? [];
  const jobStates = useMemo(() => latestByJob(jobRows), [jobRows]);
  const anyRunning = useMemo(
    () => pendingJobs.size > 0 || Object.values(jobStates).some((state) => state.status === "running"),
    [jobStates, pendingJobs],
  );

  useEffect(() => {
    if (!anyRunning) return;
    const id = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(id);
  }, [anyRunning, refresh]);

  const start = useCallback(
    async (jobName: string) => {
      setStartError(null);
      setPendingJobs((prev) => new Set(prev).add(jobName));
      try {
        await startRefreshJob(jobName);
        await refresh();
      } catch (error) {
        setStartError(error instanceof Error ? error.message : "Failed to start refresh job");
      } finally {
        setPendingJobs((prev) => {
          const next = new Set(prev);
          next.delete(jobName);
          return next;
        });
      }
    },
    [refresh],
  );

  return {
    allowlist: payload?.allowlist ?? [],
    latestStatus: payload?.latest_status ?? null,
    rows: jobRows,
    jobStates,
    pendingJobs,
    anyRunning,
    start,
    startError,
    refresh,
  };
}
