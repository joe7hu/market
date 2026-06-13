export type MetricPoint = {
  date: string;
  value?: number;
  index_price?: number;
};

export type PeriodOption = {
  key: string;
  years: number;
};

export const DEFAULT_MARKET_PERIODS: PeriodOption[] = [
  { key: "5Y", years: 5 },
  { key: "10Y", years: 10 },
  { key: "20Y", years: 20 },
  { key: "30Y", years: 30 },
  { key: "50Y", years: 50 },
  { key: "All", years: 0 },
];

export const FORWARD_PE_PERIODS: PeriodOption[] = [
  { key: "1Y", years: 1 },
  { key: "2Y", years: 2 },
  { key: "5Y", years: 5 },
  { key: "10Y", years: 10 },
  { key: "20Y", years: 20 },
  { key: "All", years: 0 },
];
