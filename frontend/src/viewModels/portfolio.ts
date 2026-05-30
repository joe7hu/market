import type { AppModel } from "@/model";
import type { PanelData, RowRecord } from "@/types";
import { rows } from "@/utils";

export type PortfolioViewModel = {
  riskRows: RowRecord[];
  reviewRows: RowRecord[];
  exposureClusterRows: RowRecord[];
  topHolding: AppModel["holdings"][number] | undefined;
};

export function buildPortfolioViewModel(data: PanelData, model: AppModel): PortfolioViewModel {
  return {
    riskRows: rows(data.portfolioRiskCards),
    reviewRows: rows(data.reviewActions),
    exposureClusterRows: rows(data.exposureClusters),
    topHolding: model.holdings.slice().sort((a, b) => b.weight - a.weight)[0],
  };
}
