import { useCallback, useEffect, useRef, useState } from "react";
import { loadToday as loadTodayResponse, type TodayResponse } from "../api/panel";
import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { TodayPage } from "../views/today";

export function TodayRoute() {
  const { data, model, lastRefresh, loading, loadScope, openTicker, scopeStatus } = useMarketData();
  const [actionQueue, setActionQueue] = useState<TodayResponse | null>(null);
  const [actionQueueLoading, setActionQueueLoading] = useState(true);
  const [actionQueueError, setActionQueueError] = useState<string | null>(null);
  usePanelScope("today");
  const request = useRef<AbortController | null>(null);

  const loadActionQueue = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setActionQueueLoading(true);
    setActionQueueError(null);
    try {
      const response = await loadTodayResponse(controller.signal);
      if (!controller.signal.aborted) setActionQueue(response);
    } catch (error) {
      if (!controller.signal.aborted) setActionQueueError(error instanceof Error ? error.message : "Action Queue unavailable.");
    } finally {
      if (!controller.signal.aborted) { setActionQueueLoading(false); request.current = null; }
    }
  }, []);

  useEffect(() => {
    void loadActionQueue();
    const timer = setInterval(() => { if (!request.current) void loadActionQueue(); }, 30000);
    return () => { clearInterval(timer); request.current?.abort(); };
  }, [loadActionQueue]);

  return (
    <>

      <TodayPage
      data={data}
      model={model}
      lastRefresh={lastRefresh}
      actionQueue={actionQueue}
      actionQueueLoading={actionQueueLoading}
      actionQueueError={actionQueueError}
      loading={loading || actionQueueLoading}
      scopeStatus={scopeStatus.today}
      onRefresh={() => void Promise.allSettled([loadScope("today"), loadActionQueue()])}
      onOpenTicker={openTicker}
      />
    </>
  );
}
