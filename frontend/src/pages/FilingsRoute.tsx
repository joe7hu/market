import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { buildDatasetProfile } from "@/viewModels/datasetProfiles";
import { DatasetPage } from "@/views/datasetPage";

export function FilingsRoute() {
  const { data, openTicker } = useMarketData();
  usePanelScope("filings");

  return <DatasetPage profile={buildDatasetProfile("filings", data)} onOpenTicker={openTicker} />;
}
