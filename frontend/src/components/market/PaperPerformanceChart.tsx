import { useEffect, useMemo, useRef, useState } from "react";
import * as echarts from "echarts/core";
import type { EChartsOption, SeriesOption } from "echarts";
import { LineChart, ScatterChart } from "echarts/charts";
import { AriaComponent, DataZoomComponent, GridComponent, LegendComponent, MarkLineComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { chartMoney, eventCurveValue, eventName, financialLineData, normalizeFinancialPoints, paperEventTooltip, record, timeExtent, visibleTimeWindow, type FinancialPoint, type PaperEvent, type VisibleWindow } from "./financialChartModel";

export { paperEventTooltip } from "./financialChartModel";

echarts.use([LineChart, ScatterChart, AriaComponent, DataZoomComponent, GridComponent, LegendComponent, MarkLineComponent, TooltipComponent, CanvasRenderer]);

const EMPTY_EVENTS: PaperEvent[] = [];
const EVENT_STYLE: Record<string, { color: string; symbol: string }> = {
  entry: { color: "#059669", symbol: "triangle" },
  exit: { color: "#dc2626", symbol: "diamond" },
  partial_exit: { color: "#d97706", symbol: "diamond" },
  open_position: { color: "#7c3aed", symbol: "circle" },
  strategy_observed: { color: "#2563eb", symbol: "rect" },
};

function tooltip(input: unknown, label: string): string {
  const items = (Array.isArray(input) ? input : [input]).map(item => record(record(item).data));
  const point = items.map(item => item.point as FinancialPoint | undefined).find(Boolean);
  const events = items.map(item => item.event as PaperEvent | undefined).filter((event): event is PaperEvent => Boolean(event));
  const uniqueEvents = [...new Map(events.map(event => [`${event.at}:${event.kind}:${event.trade_id ?? event.symbol}`, event])).values()];
  return [point ? `${new Date(point.at).toLocaleString()}\n${label}: ${chartMoney(point.value)}` : "", ...uniqueEvents.slice(0, 8).map(paperEventTooltip)].filter(Boolean).join("\n\n");
}

export function PaperPerformanceChart({ points, label, onSelect, events = EMPTY_EVENTS, mode = "cumulative", onRangeChange, metric = "pnl" }: {
  points: FinancialPoint[]; label: string; onSelect: (tradeId: string) => void; events?: PaperEvent[];
  mode?: "cumulative" | "drawdown"; onRangeChange?: (from: string, to: string) => void; metric?: "pnl" | "nav";
}) {
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<ReturnType<typeof echarts.init> | null>(null);
  const callbacks = useRef({ onSelect, onRangeChange });
  callbacks.current = { onSelect, onRangeChange };
  const [visible, setVisible] = useState<VisibleWindow | null>(null);
  const [dark, setDark] = useState(false);
  const ordered = useMemo(() => normalizeFinancialPoints(points), [points]);
  const markers = useMemo(() => events.filter(event => Number.isFinite(Date.parse(event.at))).sort((a, b) => Date.parse(a.at) - Date.parse(b.at)), [events]);
  const extent = useMemo(() => timeExtent([...ordered, ...markers]), [ordered, markers]);
  const currentExtent = useRef(extent);
  currentExtent.current = extent;
  const hasValues = ordered.some(point => point.value !== null);
  const activityOnly = !hasValues && markers.length > 0;
  const hasEvidence = hasValues || markers.length > 0;

  useEffect(() => {
    if (!container.current) return;
    const instance = echarts.init(container.current);
    chart.current = instance;
    instance.on("click", (input: unknown) => {
      const data = record(record(input).data);
      const marker = data.event as PaperEvent | undefined;
      const point = data.point as FinancialPoint | undefined;
      const id = marker?.trade_id || point?.trade_id;
      if (id) callbacks.current.onSelect(id);
    });
    instance.on("datazoom", (input: unknown) => setVisible(visibleTimeWindow(input, currentExtent.current)));
    const resize = new ResizeObserver(() => instance.resize());
    resize.observe(container.current);
    const updateTheme = () => setDark(document.documentElement.classList.contains("dark"));
    const theme = new MutationObserver(updateTheme);
    theme.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    updateTheme();
    return () => { theme.disconnect(); resize.disconnect(); instance.dispose(); chart.current = null; };
  }, []);

  useEffect(() => {
    const foreground = dark ? "#cbd5e1" : "#475569";
    const gridColor = dark ? "#334155" : "#e2e8f0";
    const kinds = [...new Set(markers.map(event => event.kind))];
    const color = metric === "nav" ? "#3b82f6" : mode === "drawdown" ? "#d97706" : "#2563eb";
    const minimum = extent?.[0], maximum = extent?.[1];
    const single = minimum !== undefined && minimum === maximum;
    const axes = {
      type: "time" as const, min: single ? minimum - 3600000 : minimum, max: single ? maximum + 3600000 : maximum,
      axisLine: { lineStyle: { color: gridColor } }, axisTick: { show: false }, splitLine: { show: false },
      axisLabel: { color: foreground, hideOverlap: true, fontSize: 11 },
    };
    const showRail = markers.length > 0 && !activityOnly;
    const series: SeriesOption[] = activityOnly ? [] : [{
      id: "verified-values", name: label, type: "line", step: metric === "nav" ? false : "end",
      connectNulls: false, showSymbol: ordered.filter(point => point.value !== null).length < 3,
      symbol: "circle", symbolSize: 7, data: financialLineData(ordered),
      lineStyle: { color, width: 2.5 }, itemStyle: { color }, areaStyle: { color, opacity: 0.06 },
      emphasis: { focus: "series" },
      markLine: metric === "nav" ? undefined : { silent: true, symbol: "none", label: { show: false }, lineStyle: { color: foreground, type: "dashed", opacity: 0.45 }, data: [{ yAxis: 0 }] },
    }];
    kinds.forEach((kind, index) => {
      const style = EVENT_STYLE[kind] ?? { color: "#64748b", symbol: "circle" };
      const selected = markers.filter(event => event.kind === kind);
      const shared = { name: kind.replaceAll("_", " "), type: "scatter" as const, symbol: style.symbol, symbolSize: 11, itemStyle: { color: style.color, borderColor: dark ? "#0f172a" : "#ffffff", borderWidth: 1.5 } };
      if (!activityOnly) series.push({ ...shared, id: `curve-${kind}`, data: selected.flatMap(event => {
        const value = metric === "nav" ? null : eventCurveValue(event, mode);
        return value === null ? [] : [{ value: [Date.parse(event.at), value], event }];
      }) });
      // Every event has a separate activity rail. An unknown P&L never becomes $0.
      series.push({ ...shared, id: `events-${kind}`, xAxisIndex: activityOnly ? 0 : 1, yAxisIndex: activityOnly ? 0 : 1,
        data: selected.map(event => ({ value: [Date.parse(event.at), activityOnly ? kind.replaceAll("_", " ") : kinds.length > 1 ? index / (kinds.length - 1) : 0.5], event })) });
    });
    const option: EChartsOption = {
      animation: false, aria: { enabled: true, description: `${label}. Gaps mean missing evidence. Trade events are also available in the table below.` },
      textStyle: { fontFamily: "inherit", color: foreground },
      tooltip: { trigger: "axis", renderMode: "richText", confine: true, backgroundColor: dark ? "#0f172a" : "#ffffff", borderColor: gridColor, textStyle: { color: foreground, fontSize: 12 }, axisPointer: { type: "cross", label: { show: false } }, formatter: (input: unknown) => tooltip(input, label) },
      legend: { top: 0, right: 12, type: "scroll", textStyle: { color: foreground, fontSize: 11 }, data: [...(activityOnly ? [] : [label]), ...kinds.map(kind => kind.replaceAll("_", " "))] },
      grid: [{ left: activityOnly ? 125 : 76, right: 24, top: 42, bottom: showRail ? 140 : 70 }, ...(showRail ? [{ left: 76, right: 24, height: 28, bottom: 78 }] : [])],
      xAxis: [{ ...axes, gridIndex: 0 }, ...(showRail ? [{ ...axes, gridIndex: 1, show: false }] : [])],
      yAxis: activityOnly ? [{ type: "category", data: kinds.map(kind => kind.replaceAll("_", " ")), axisLabel: { color: foreground, fontSize: 11 }, axisTick: { show: false }, axisLine: { show: false }, splitLine: { show: true, lineStyle: { color: gridColor, type: "dashed" } } }] : [{ type: "value", scale: metric === "nav", axisLabel: { color: foreground, fontSize: 11, formatter: (value: number) => chartMoney(value, true) }, splitLine: { lineStyle: { color: gridColor, type: "dashed" } } }, ...(showRail ? [{ type: "value" as const, gridIndex: 1, min: -0.25, max: 1.25, show: false }] : [])],
      dataZoom: [{ id: "inside", type: "inside", xAxisIndex: showRail ? [0, 1] : [0], filterMode: "none" }, { id: "slider", type: "slider", xAxisIndex: showRail ? [0, 1] : [0], filterMode: "none", bottom: 8, height: 24, showDetail: false, borderColor: gridColor, textStyle: { color: foreground } }],
      series,
    };
    // Keep the instance and its zoom across ordinary React callback/data updates.
    chart.current?.setOption(option, { replaceMerge: ["series", "xAxis", "yAxis", "grid"] });
    chart.current?.resize();
  }, [activityOnly, dark, extent, label, markers, metric, mode, ordered]);

  const zoomDays = (days: number | null) => {
    if (!extent) return;
    chart.current?.dispatchAction({ type: "dataZoom", startValue: days === null ? extent[0] : Math.max(extent[0], extent[1] - days * 86400000), endValue: extent[1] });
    if (days === null) setVisible(null);
  };
  return <div className="space-y-2">
    <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
      <div className="flex gap-1" role="group" aria-label="Chart zoom"><button type="button" className="rounded border px-3 py-1.5 hover:bg-muted" onClick={() => zoomDays(7)}>1W</button><button type="button" className="rounded border px-3 py-1.5 hover:bg-muted" onClick={() => zoomDays(30)}>1M</button><button type="button" className="rounded border px-3 py-1.5 hover:bg-muted" onClick={() => zoomDays(null)}>Reset view</button></div>
      {visible && onRangeChange ? <button type="button" className="rounded border border-primary px-3 py-1.5 text-primary" onClick={() => callbacks.current.onRangeChange?.(visible.from, visible.to)}>Use visible dates · {visible.from} – {visible.to} UTC</button> : <span className="text-muted-foreground">{Intl.DateTimeFormat().resolvedOptions().timeZone} · scroll to zoom</span>}
    </div>
    {ordered.length < points.length || markers.length < events.length ? <p role="alert" className="rounded border border-amber-500/40 p-3 text-sm">Some records have invalid timestamps. The timeline is incomplete.</p> : null}
    {!hasEvidence ? <p className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">No verified chart observations in this range.</p> : null}
    {activityOnly ? <p className="text-xs text-muted-foreground">Trade activity is available; no verified P&L observations exist in this range.</p> : null}
    <div ref={container} className={!hasEvidence ? "hidden" : activityOnly ? "h-[18rem] w-full" : "h-[25rem] w-full sm:h-[28rem]"} role="img" aria-label={label} />
    {markers.length > 0 ? <><p className="text-xs text-muted-foreground">Trade activity is shown beneath the curve; missing valuations are not plotted as profit or loss.</p><details className="rounded-lg border border-border"><summary className="cursor-pointer px-3 py-2 text-sm">Trade events · {markers.length}</summary><div className="max-h-72 overflow-auto"><table className="w-full text-left text-xs"><thead><tr className="border-t border-border text-muted-foreground"><th className="p-3">Time</th><th className="p-3">Trade</th><th className="p-3">Price</th><th className="p-3">Quantity</th><th className="p-3">P&L</th></tr></thead><tbody>{[...markers].reverse().map((event, index) => <tr className="border-t border-border" key={`${event.at}:${event.trade_id}:${event.kind}:${index}`}><td className="whitespace-nowrap p-3">{new Date(event.at).toLocaleString()}</td><td className="p-3">{event.trade_id ? <button type="button" className="text-left font-medium text-primary hover:underline" onClick={() => onSelect(event.trade_id!)}>{event.symbol} · {eventName(event)}</button> : <span>{event.symbol} · {eventName(event)}</span>}{event.strategy ? <div className="text-muted-foreground">{event.strategy}</div> : null}</td><td className="p-3 tabular-nums">{chartMoney(event.price)}</td><td className="p-3 tabular-nums">{event.quantity ?? "—"}</td><td className="p-3 tabular-nums">{chartMoney(event.pnl)}</td></tr>)}</tbody></table></div></details></> : null}
  </div>;
}
