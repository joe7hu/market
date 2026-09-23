import { useCallback, useEffect, useRef, useState } from "react";
import { loadDecisionFunnel, type DecisionFunnel, type RefreshJob } from "@/api/panel";
import { decisionRefreshKey } from "./qualification";

/** Read-only refresh; never launch another decision job to make the UI green. */
export function useDecisionFunnel(jobs: readonly RefreshJob[]) {
  const request = useRef(0);
  const [state, setState] = useState<{ funnel: DecisionFunnel | null; loading: boolean; error: string | null }>({ funnel: null, loading: true, error: null });
  const key = decisionRefreshKey(jobs);
  const refresh = useCallback(async () => {
    const current = ++request.current;
    setState(previous => ({ ...previous, loading: true }));
    try {
      const funnel = await loadDecisionFunnel();
      if (current === request.current) setState({ funnel, loading: false, error: null });
    } catch (reason) {
      if (current === request.current) setState(previous => ({ ...previous, loading: false, error: reason instanceof Error ? reason.message : "Decision funnel could not be refreshed." }));
    }
  }, []);
  useEffect(() => {
    void refresh();
    return () => { request.current += 1; };
  }, [key, refresh]);
  return { ...state, refresh };
}
