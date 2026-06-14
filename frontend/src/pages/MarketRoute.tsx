import { usePanelScope } from "../hooks";
import { useMarketData } from "../marketData";
import { WorkspacePage } from "@/views/workspacePage";
import { rows } from "@/utils";
import { textField } from "@/views/rowFormat";
import { isMarketDriver } from "@/views/market/format";
import { MarketAssetMatrix, MarketEnvironmentPanel, ReferenceValuationCharts } from "@/views/market/panels";

export function MarketRoute() {
  const { data } = useMarketData();
  usePanelScope("market");

  const referenceRows = rows(data.marketValuationReferenceCharts);
  const assetRows = rows(data.marketEnvironmentAssets);
  const environmentRows = rows(data.marketEnvironmentModel);
  const drivers = environmentRows.filter((row) => textField(row, ["category"]) !== "Overall");
  const marketDrivers = drivers.filter((row) => isMarketDriver(textField(row, ["category"])));

  return (
    <WorkspacePage
      eyebrow="Market stance"
      title="Where the Market Stands"
      subtitle="Broad market valuation, trend, breadth, risk appetite, and leadership."
    >
      <MarketEnvironmentPanel rows={marketDrivers} referenceRows={referenceRows} assetRows={assetRows} />

      <ReferenceValuationCharts rows={referenceRows} />

      <MarketAssetMatrix rows={assetRows} />
    </WorkspacePage>
  );
}
