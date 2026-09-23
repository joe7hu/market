import type { PaperPerformance } from "@/api/paper";
import { PaperPerformanceChart } from "./PaperPerformanceChart";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

export function PaperPerformancePanel({ performance, chart, points, drawdownPoints, onChart, onSelect, onRangeChange }: { performance: PaperPerformance; chart: string; points: Array<{ at: string; cumulative_net_pnl: number; trade_id: string }>; drawdownPoints: Array<{ at: string; drawdown: number; trade_id: string }>; onChart: (value: string) => void; onSelect: (tradeId: string) => void; onRangeChange: (from: string, to: string) => void }) {
  const drawdown = chart === "drawdown";
  const chartPoints = drawdown ? drawdownPoints.map((point) => ({ ...point, value: point.drawdown })) : points.map((point) => ({ ...point, value: point.cumulative_net_pnl }));
  const events = performance.series?.event_markers ?? [];
  return <Card><CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between"><div><CardTitle>Performance and trade events</CardTitle><p className="mt-1 text-xs text-muted-foreground">{drawdown ? "Drawdown from verified realized P&L." : "Realized P&L above; entry, exit and open-position activity below."}</p></div><Select value={chart} onValueChange={onChart}><SelectTrigger className="w-48" aria-label="Performance chart mode"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="cumulative_net_pnl">Cumulative P&L</SelectItem><SelectItem value="drawdown">Drawdown</SelectItem></SelectContent></Select></CardHeader><CardContent><PaperPerformanceChart label={drawdown ? "Verified realized drawdown" : "Cumulative verified realized P&L"} mode={drawdown ? "drawdown" : "cumulative"} points={chartPoints} events={events} onSelect={onSelect} onRangeChange={onRangeChange} /></CardContent></Card>;
}

