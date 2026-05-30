import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { buildDatasetProfile } from "@/viewModels/datasetProfiles";
import { DatasetPage } from "@/views/datasetPage";

export function CalendarRoute() {
  const { data, openTicker } = useMarketData();
  usePanelScope("calendar");

  return <DatasetPage profile={buildDatasetProfile("calendar", data)} onOpenTicker={openTicker} />;
}
