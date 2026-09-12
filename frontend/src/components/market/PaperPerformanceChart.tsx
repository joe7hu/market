import { useEffect, useRef } from "react";
import * as echarts from "echarts/core";
import { LineChart, ScatterChart } from "echarts/charts";
import { AriaComponent, DataZoomComponent, GridComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([LineChart, ScatterChart, AriaComponent, DataZoomComponent, GridComponent, TooltipComponent, CanvasRenderer]);

type Point = { at: string; value: number | null; trade_id: string };
type EventMarker = { at: string; kind: string; trade_id?: string; symbol?: string; strategy?: string | null; label?: string; value?: number; cumulative_net_pnl?: number; drawdown?: number; pnl?: number | null; price?: number | null; quantity?: number | null; status?: string };

function escapeHtml(value: unknown) {
  return String(value ?? "").replace(/[&<>\"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '\"': "&quot;", "'": "&#39;" })[character] ?? character);
}

export function PaperPerformanceChart({ points, label, onSelect, events = [], mode = "cumulative", onRangeChange }: { points: Point[]; label: string; onSelect: (tradeId: string) => void; events?: EventMarker[]; mode?: "cumulative" | "drawdown"; onRangeChange?: (from: string, to: string) => void }) {
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!container.current) return;
    const chart = echarts.init(container.current);
    const valueFor = (event: EventMarker) => mode === "drawdown" ? event.drawdown ?? null : event.cumulative_net_pnl ?? null;
    const eventKinds = [...new Set(events.map((event) => event.kind))];
    const colors: Record<string, string> = { entry: "#059669", exit: "#dc2626", partial_exit: "#f59e0b", open_position: "#7c3aed", strategy_observed: "#2563eb" };
    chart.setOption({
      aria: { enabled: true, description: label },
      tooltip: { trigger: "item", renderMode: "richText", formatter: (params: any) => {
        if (params?.seriesType === "scatter") {
          const event = params.data?.[2] as EventMarker | undefined;
          if (!event) return "";
          const pnl = event.pnl == null ? "not verified" : `${event.pnl >= 0 ? "+" : ""}$${Math.abs(event.pnl).toFixed(2)}`;
          return `<strong>${escapeHtml(event.label ?? event.kind)}</strong><br/>${escapeHtml(new Date(event.at).toLocaleString())}<br/>${escapeHtml(event.symbol ?? "")}${event.strategy ? ` · ${escapeHtml(event.strategy)}` : ""}<br/>P&L: ${escapeHtml(pnl)}`;
        }
        const point = points[params?.dataIndex];
        return point ? `<strong>${escapeHtml(new Date(point.at).toLocaleString())}</strong><br/>${escapeHtml(label)}: ${point.value == null ? "—" : escapeHtml(`$${point.value.toFixed(2)}`)}` : "";
      } },
      legend: eventKinds.length ? { bottom: 0, type: "scroll" } : undefined,
      grid: { left: 65, right: 20, top: 25, bottom: eventKinds.length ? 92 : 70 },
      xAxis: { type: "time", axisLabel: { formatter: (value: number) => new Date(value).toLocaleDateString(undefined, { month: "short", day: "numeric" }) } },
      yAxis: { type: "value", name: "USD", scale: true },
      dataZoom: [{ type: "inside" }, { type: "slider", bottom: 5 }],
      series: [{ type: "line", step: "end", connectNulls: false, symbolSize: 8,
        name: label, data: points.map((point) => [point.at, point.value]), lineStyle: { color: "#2563eb", width: 3 }, itemStyle: { color: "#2563eb" } },
        ...eventKinds.map((kind) => ({ type: "scatter", name: kind.replaceAll("_", " "), symbolSize: 13, itemStyle: { color: colors[kind] ?? "#64748b", borderColor: "#fff", borderWidth: 2 }, data: events.filter((event) => event.kind === kind).map((event) => [event.at, valueFor(event), event]) })),
      ],
    });
    chart.on("click", (event: any) => {
      const marker = event?.seriesType === "scatter" ? event.data?.[2] as EventMarker | undefined : undefined;
      const point = points[event?.dataIndex];
      const tradeId = marker?.trade_id ?? point?.trade_id;
      if (tradeId) onSelect(tradeId);
    });
    chart.on("datazoom", (event: any) => {
      if (!onRangeChange || points.length < 2) return;
      const range = event?.batch?.[0] ?? event;
      const start = Number(range?.startValue);
      const end = Number(range?.endValue);
      const startIndex = Number.isFinite(start) ? points.findIndex((point) => new Date(point.at).getTime() >= start) : Math.round(((Number(range?.start) || 0) / 100) * (points.length - 1));
      const endIndex = Number.isFinite(end) ? [...points].reverse().findIndex((point) => new Date(point.at).getTime() <= end) : Math.round(((Number(range?.end) || 100) / 100) * (points.length - 1));
      const resolvedEnd = Number.isFinite(end) ? points.length - 1 - endIndex : endIndex;
      if (startIndex >= 0 && resolvedEnd >= startIndex && points[startIndex] && points[resolvedEnd]) onRangeChange(points[startIndex].at.slice(0, 10), points[resolvedEnd].at.slice(0, 10));
    });
    const resize = new ResizeObserver(() => chart.resize());
    resize.observe(container.current);
    return () => { resize.disconnect(); chart.dispose(); };
  }, [events, label, mode, onRangeChange, onSelect, points]);
  return <div ref={container} className="h-[26rem] w-full" role="img" aria-label={label} />;
}
