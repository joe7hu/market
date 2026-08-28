import { useCallback, useEffect, useState } from "react";
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

  const loadActionQueue = useCallback(async () => {
    setActionQueueLoading(true);
    setActionQueueError(null);
    try {
      setActionQueue(await loadTodayResponse());
    } catch (error) {
      setActionQueueError(error instanceof Error ? error.message : "Action Queue unavailable.");
    } finally {
      setActionQueueLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadActionQueue();
  }, [loadActionQueue]);

  return (
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
  );
}
