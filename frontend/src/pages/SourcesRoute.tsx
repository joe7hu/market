import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { buildDatasetProfile } from "@/viewModels/datasetProfiles";
import { DatasetPage } from "@/views/datasetPage";

export function SourcesRoute() {
  const { data, openTicker } = useMarketData();
  usePanelScope("sources");

  return <DatasetPage profile={buildDatasetProfile("sources", data)} onOpenTicker={openTicker} />;
}
