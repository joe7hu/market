import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { buildDatasetProfile } from "@/viewModels/datasetProfiles";
import { DatasetPage } from "@/views/datasetPage";

export function MarketRoute() {
  const { data } = useMarketData();
  usePanelScope("market");

  return <DatasetPage profile={buildDatasetProfile("market", data)} />;
}
