import { useEffect, useRef } from "react";
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import { AriaComponent, DataZoomComponent, GridComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([LineChart, AriaComponent, DataZoomComponent, GridComponent, TooltipComponent, CanvasRenderer]);

type Point = { at: string; value: number | null; trade_id: string };

export function PaperPerformanceChart({ points, label, onSelect }: { points: Point[]; label: string; onSelect: (tradeId: string) => void }) {
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!container.current) return;
    const chart = echarts.init(container.current);
    chart.setOption({
      aria: { enabled: true, description: label },
      tooltip: { trigger: "axis", renderMode: "richText" },
      grid: { left: 65, right: 20, top: 20, bottom: 70 },
      xAxis: { type: "time" },
      yAxis: { type: "value", name: "USD", scale: true },
      dataZoom: [{ type: "inside" }, { type: "slider", bottom: 5 }],
      series: [{ type: "line", step: "end", connectNulls: false, symbolSize: 8,
        data: points.map((point) => [point.at, point.value]), lineStyle: { color: "#2563eb" } }],
    });
    chart.on("click", (event) => { const point = points[event.dataIndex]; if (point) onSelect(point.trade_id); });
    const resize = new ResizeObserver(() => chart.resize());
    resize.observe(container.current);
    return () => { resize.disconnect(); chart.dispose(); };
  }, [points, label, onSelect]);
  return <div ref={container} className="h-72 w-full" role="img" aria-label={label} />;
}
