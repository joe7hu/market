import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { buildDatasetProfile } from "@/viewModels/datasetProfiles";
import { DatasetPage } from "@/views/datasetPage";

export function SuperinvestorsRoute() {
  const { data, openTicker } = useMarketData();
  usePanelScope("superinvestors");

  return <DatasetPage profile={buildDatasetProfile("superinvestors", data)} onOpenTicker={openTicker} />;
}
