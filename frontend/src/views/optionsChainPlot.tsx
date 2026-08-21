import { useEffect, useMemo, useRef, useState } from "react";
import * as echarts from "echarts/core";
import { AriaComponent, DataZoomComponent, GridComponent, LegendComponent, TitleComponent, ToolboxComponent, TooltipComponent, VisualMapComponent } from "echarts/components";
import { HeatmapChart, LineChart, ScatterChart } from "echarts/charts";
import { CanvasRenderer } from "echarts/renderers";
import type { EChartsCoreOption, EChartsType } from "echarts/core";
import type { OptionHistoryCurves, OptionHistorySurface, OptionHistorySurfaceGrid } from "@/api/options";
import { buildOptionCurvePlotData, buildProviderIVSurfaceData, curveChoices, type SurfaceViewportPreset } from "./optionsChainPlotData";

echarts.use([
  AriaComponent, CanvasRenderer, DataZoomComponent, GridComponent, HeatmapChart,
  LegendComponent, LineChart, ScatterChart, TitleComponent,
  ToolboxComponent, TooltipComponent, VisualMapComponent,
]);

const INK = "#17211c";
const MUTED = "#68756d";
const GRID = "#dfe6e1";
const ACCENT = "#0f766e";
const OBSERVED = "#f8fafc";
const SURFACE_COLORS = ["#16324f", "#1f5f78", "#2f8992", "#70ad91", "#d0c978", "#f2bd45"];
let registered3D = false;

async function register3DCharts() {
  if (registered3D) return;
  const [{ Grid3DComponent }, { Scatter3DChart, SurfaceChart }] = await Promise.all([
    import("echarts-gl/components"),
    import("echarts-gl/charts"),
  ]);
  echarts.use([Grid3DComponent, Scatter3DChart, SurfaceChart]);
  registered3D = true;
}

type SurfaceObserved = {
  contract_id?: unknown;
  strike?: unknown;
  provider_iv?: unknown;
  bid?: unknown;
  ask?: unknown;
  provider_delta?: unknown;
};

export function OptionSurfacePlot({ surface }: { surface: OptionHistorySurface }) {
  const rows = surface.observed
    .filter((row): row is SurfaceObserved => finiteNumber(row.strike) && finiteNumber(row.provider_iv))
    .sort((left, right) => Number(left.strike) - Number(right.strike));
  const classifications = new Map(surface.fitted.map((row) => [String(row.contract_id), String(row.classification ?? "observed")]));
  const data = rows.map((row) => ({
    value: [Number(row.strike), Number(row.provider_iv)],
    strike: Number(row.strike),
    iv: Number(row.provider_iv),
    bid: finiteNumber(row.bid) ? row.bid : null,
    ask: finiteNumber(row.ask) ? row.ask : null,
    delta: finiteNumber(row.provider_delta) ? row.provider_delta : null,
    classification: classifications.get(String(row.contract_id)) ?? "observed",
  }));
  const strikeMin = rows.length ? Number(rows[0]!.strike) : undefined;
  const strikeMax = rows.length ? Number(rows.at(-1)!.strike) : undefined;
  const strikePadding = strikeMin !== undefined && strikeMax !== undefined ? Math.max(5, (strikeMax - strikeMin) * 0.04) : 0;
  const option = useMemo<EChartsCoreOption>(() => ({
    animationDuration: 350,
    aria: { enabled: true },
    color: [ACCENT],
    grid: { left: 64, right: 28, top: 28, bottom: 58 },
    tooltip: {
      trigger: "item",
      confine: true,
      formatter: (input: unknown) => {
        const row = tooltipData(input);
        return [`<strong>Strike ${formatNumber(row.strike)}</strong>`, `Provider IV ${formatPercent(row.iv)}`, `Bid / ask ${formatNumber(row.bid)} / ${formatNumber(row.ask)}`, `Delta ${formatNumber(row.delta, 3)}`, `Evidence ${String(row.classification ?? "observed")}`].join("<br>");
      },
    },
    toolbox: { right: 8, feature: { restore: {}, saveAsImage: { name: `${surface.symbol}-${surface.expiration}-${surface.option_type}-iv-smile` } } },
    xAxis: { type: "value", min: strikeMin === undefined ? undefined : Math.floor(strikeMin - strikePadding), max: strikeMax === undefined ? undefined : Math.ceil(strikeMax + strikePadding), name: "Strike", nameLocation: "middle", nameGap: 36, axisLine: { lineStyle: { color: MUTED } }, splitLine: { lineStyle: { color: GRID } } },
    yAxis: { type: "value", name: "Provider IV", nameLocation: "middle", nameGap: 46, axisLabel: { formatter: (value: number) => formatPercent(value) }, axisLine: { lineStyle: { color: MUTED } }, splitLine: { lineStyle: { color: GRID } } },
    series: [{
      type: "line", name: "Observed provider IV", data, showSymbol: true, symbolSize: 5, smooth: 0.12,
      lineStyle: { width: 2, color: ACCENT }, itemStyle: { color: "#14b8a6", borderColor: "#ffffff", borderWidth: 1 },
    }],
  }), [data, strikeMax, strikeMin, strikePadding, surface.expiration, surface.option_type, surface.symbol]);
  const eligible = Number((surface.diagnostics.row_metrics as Record<string, unknown> | undefined)?.eligible_rows ?? 0);
  const rejected = Number((surface.diagnostics.row_metrics as Record<string, unknown> | undefined)?.rejected_rows ?? 0);
  return <div className="space-y-3 p-2">
    <div className="flex flex-wrap items-end justify-between gap-3 px-2">
      <div><p className="text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">Expiry smile</p><h3 className="text-lg font-semibold">{surface.expiration} {surface.option_type}</h3></div>
      <p className="text-xs text-muted-foreground">{eligible.toLocaleString()} eligible · {rejected.toLocaleString()} rejected · market IV only</p>
    </div>
    <EChart option={option} className="h-[520px] w-full" label={`${surface.symbol} ${surface.expiration} ${surface.option_type} provider implied volatility smile`} />
    <p className="px-2 text-xs text-muted-foreground">Option fair-value bounds are dollar prices, so they are intentionally excluded from this IV axis.</p>
  </div>;
}

export function OptionSurfaceExplorer({ surface, optionType, selectedDte, selectedExpiration, webgl }: { surface: OptionHistorySurfaceGrid; optionType: "call" | "put"; selectedDte?: number; selectedExpiration?: string; webgl: boolean | null }) {
  const [preset, setPreset] = useState<SurfaceViewportPreset>("focus");
  const [mode, setMode] = useState<"map" | "3d">("map");
  const [threeDReady, setThreeDReady] = useState(false);
  const compact = useCompactChart();
  useEffect(() => { if (webgl === false && mode === "3d") setMode("map"); }, [mode, webgl]);
  useEffect(() => {
    if (mode !== "3d" || threeDReady) return;
    let cancelled = false;
    void register3DCharts().then(() => { if (!cancelled) setThreeDReady(true); });
    return () => { cancelled = true; };
  }, [mode, threeDReady]);
  const plot = useMemo(() => buildProviderIVSurfaceData(surface, optionType, preset, selectedDte), [optionType, preset, selectedDte, surface]);
  const option = useMemo(() => mode === "3d" && threeDReady ? surface3dOption(plot, surface.symbol, optionType) : surfaceMapOption(plot, surface.symbol, optionType, compact), [compact, mode, optionType, plot, surface.symbol, threeDReady]);
  const hasGrid = plot.heatmap.length > 0;
  if (!hasGrid) return <p className="p-4 text-sm text-muted-foreground">No provider-IV grid is available for this snapshot and viewport.</p>;
  return <div className="space-y-3 p-2">
    <div className="grid gap-3 rounded-lg border border-border bg-muted/40 p-3 lg:grid-cols-[1fr_auto] lg:items-center">
      <div>
        <p className="text-xs font-semibold uppercase tracking-[0.14em] text-muted-foreground">Volatility surface explorer</p>
        <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1"><h3 className="text-lg font-semibold">{surface.symbol} {optionType} IV</h3><span className="text-xs text-muted-foreground">{selectedExpiration ?? `${plot.selectedDte ?? "—"} DTE`} cross-section</span></div>
      </div>
      <div className="flex flex-wrap gap-2">
        <SegmentedButton active={preset === "focus"} onClick={() => setPreset("focus")}>Focus · ±12% / 120d</SegmentedButton>
        <SegmentedButton active={preset === "standard"} onClick={() => setPreset("standard")}>Wider · ±30% / 365d</SegmentedButton>
        <span className="mx-1 hidden h-8 w-px bg-border sm:block" />
        <SegmentedButton active={mode === "map"} onClick={() => setMode("map")}>Surface map</SegmentedButton>
        <SegmentedButton active={mode === "3d"} disabled={webgl !== true} onClick={() => setMode("3d")}>{webgl === false ? "3D unavailable" : "3D inspect"}</SegmentedButton>
      </div>
    </div>
    <div className="grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-border bg-border sm:grid-cols-4">
      <Metric label="Window" value={preset === "focus" ? "±12% · 120d" : "±30% · 365d"} />
      <Metric label="Grid evidence" value={`${plot.heatmap.length.toLocaleString()} cells`} />
      <Metric label="Observed overlay" value={`${plot.observed.length.toLocaleString()} points`} />
      <Metric label="Color range" value={`${formatPercent(plot.ivDomain[0])}–${formatPercent(plot.ivDomain[1])}`} />
    </div>
    {mode === "3d" && !threeDReady ? <p className="rounded-lg border border-border p-4 text-sm text-muted-foreground">Loading 3D adapter…</p> : null}
    <EChart option={option} className="h-[620px] w-full rounded-lg border border-border bg-[#fbfcfa] sm:h-[680px]" label={`${surface.symbol} ${optionType} provider implied volatility ${mode === "3d" && threeDReady ? "3D surface" : "heatmap and linked expiry slice"}`} />
    <p className="px-2 text-xs text-muted-foreground">Color and height show provider IV. Blank cells preserve the no-extrapolation boundary. The default view prioritizes the near-ATM, near-term region; 3D is available as a secondary inspection mode.</p>
  </div>;
}

export function OptionCurvePlots({ curves }: { curves: OptionHistoryCurves }) {
  const choices = useMemo(() => curveChoices(curves), [curves]);
  const [requestedKey, setRequestedKey] = useState("");
  const selectedKey = choices.some((choice) => choice.key === requestedKey) ? requestedKey : (choices[0]?.key ?? "");
  const plots = useMemo(() => buildOptionCurvePlotData(curves, selectedKey), [curves, selectedKey]);
  const smileOption = lineOption("Volatility smile", "Log-moneyness", "Provider IV", plots.smile ? [{ name: plots.smile.name, x: plots.smile.x, y: plots.smile.y }] : [], "value");
  const termOption = lineOption("ATM term structure", "DTE", "ATM IV", plots.term, "value");
  const historyOption = lineOption("Historical ATM IV", "Capture", "ATM IV", plots.history ? [{ name: plots.history.name, x: plots.history.x, y: plots.history.y }] : [], "category");
  return <div className="space-y-3">
    <label className="grid max-w-sm gap-1 text-xs text-muted-foreground">Smile and history series<select value={plots.selectedKey} onChange={(event) => setRequestedKey(event.target.value)} className="h-11 rounded-md border border-input bg-background px-2 text-sm text-foreground">{choices.map((choice) => <option key={choice.key} value={choice.key}>{choice.label}</option>)}</select></label>
    <div className="grid gap-4 2xl:grid-cols-3">
      <EChart option={smileOption} className="h-[320px] rounded-lg border border-border bg-card" label="Selected volatility smile" />
      <EChart option={termOption} className="h-[320px] rounded-lg border border-border bg-card" label="Call and put ATM implied volatility term structure" />
      <EChart option={historyOption} className="h-[320px] rounded-lg border border-border bg-card" label="Historical ATM implied volatility" />
    </div>
  </div>;
}

type SurfacePlot = ReturnType<typeof buildProviderIVSurfaceData>;

function surfaceMapOption(plot: SurfacePlot, symbol: string, optionType: string, compact: boolean): EChartsCoreOption {
  const xLabels = plot.x.map(formatMoneyness);
  const yLabels = plot.y.map((value) => `${value}d`);
  const mainGrid = compact ? { left: 48, right: 12, top: 68, height: 340 } : { left: 76, right: 96, top: 78, height: 400 };
  const sliceGrid = compact ? { left: 48, right: 12, top: 486, height: 82 } : { left: 76, right: 96, top: 560, height: 78 };
  const observed = plot.observed.flatMap((row) => {
    const xIndex = nearestIndex(plot.x, row.logMoneyness);
    const yIndex = plot.y.indexOf(row.dte);
    return xIndex < 0 || yIndex < 0 ? [] : [[xIndex, yIndex, row.providerIV, row.strike] as [number, number, number, number | null]];
  });
  return {
    animationDuration: 320,
    aria: { enabled: true },
    title: [
      { text: "Surface map", subtext: compact ? "Color = provider IV" : "Log-moneyness × DTE, colored by provider IV", left: compact ? 48 : 64, top: 12, textStyle: { color: INK, fontSize: compact ? 14 : 16 }, subtextStyle: { color: MUTED, fontSize: 10 } },
      { text: `${plot.selectedDte ?? "—"} DTE cross-section`, left: compact ? 48 : 64, top: compact ? 450 : 520, textStyle: { color: INK, fontSize: compact ? 12 : 14 } },
    ],
    grid: [mainGrid, sliceGrid],
    tooltip: {
      trigger: "item", confine: true,
      formatter: (input: unknown) => {
        const value = tooltipValue(input);
        if (value.length < 3) return "";
        const strike = value[3];
        return [`<strong>${symbol} ${optionType} IV</strong>`, `Log-moneyness ${xLabels[Number(value[0])] ?? formatNumber(value[0], 3)}`, `DTE ${plot.y[Number(value[1])] ?? value[1]}`, `Provider IV ${formatPercent(value[2])}`, strike === undefined || strike === null ? "Interpolated grid" : `Observed strike ${formatNumber(strike)}`].join("<br>");
      },
    },
    toolbox: { right: compact ? 4 : 12, top: 12, itemSize: compact ? 12 : 15, feature: { restore: {}, saveAsImage: { name: `${symbol}-${optionType}-iv-surface-map` } } },
    visualMap: { show: !compact, min: plot.ivDomain[0], max: plot.ivDomain[1], calculable: true, orient: "vertical", right: 12, top: 100, itemHeight: 280, text: ["High IV", "Low IV"], formatter: (value: number) => formatPercent(value), textStyle: { color: MUTED }, inRange: { color: SURFACE_COLORS }, seriesIndex: 0 },
    xAxis: [
      { type: "category", data: xLabels, gridIndex: 0, name: "Log-moneyness", nameLocation: "middle", nameGap: compact ? 27 : 34, axisLabel: { interval: Math.max(0, Math.floor(xLabels.length / (compact ? 5 : 8))), color: MUTED, fontSize: compact ? 9 : 12 }, axisLine: { lineStyle: { color: MUTED } }, splitArea: { show: false } },
      { type: "value", gridIndex: 1, name: "Log-moneyness", nameLocation: "middle", nameGap: 30, axisLabel: { formatter: (value: number) => formatMoneyness(value), color: MUTED }, axisLine: { lineStyle: { color: MUTED } }, splitLine: { lineStyle: { color: GRID } } },
    ],
    yAxis: [
      { type: "category", data: yLabels, gridIndex: 0, name: compact ? "" : "DTE", axisLabel: { color: MUTED, fontSize: compact ? 9 : 12 }, axisLine: { lineStyle: { color: MUTED } } },
      { type: "value", gridIndex: 1, name: "IV", axisLabel: { formatter: (value: number) => formatPercent(value), color: MUTED }, splitLine: { lineStyle: { color: GRID } } },
    ],
    series: [
      { type: "heatmap", name: "Interpolated provider IV", data: plot.heatmap, progressive: 2_000, itemStyle: { borderColor: "rgba(255,255,255,0.18)", borderWidth: 0.5 }, emphasis: { itemStyle: { borderColor: "#ffffff", borderWidth: 2, shadowBlur: 8, shadowColor: "rgba(15,118,110,.35)" } } },
      { type: "scatter", name: "Observed provider IV", data: observed, symbolSize: compact ? 2.5 : 4, itemStyle: { color: OBSERVED, borderColor: "#16324f", borderWidth: 0.8, opacity: 0.82 } },
      { type: "line", name: `${plot.selectedDte ?? "—"} DTE slice`, xAxisIndex: 1, yAxisIndex: 1, data: plot.selectedSlice, showSymbol: false, smooth: 0.16, lineStyle: { color: ACCENT, width: 2.5 }, areaStyle: { color: "rgba(15,118,110,.10)" } },
    ],
  } as EChartsCoreOption;
}

function surface3dOption(plot: SurfacePlot, symbol: string, optionType: string): EChartsCoreOption {
  return {
    animationDurationUpdate: 300,
    aria: { enabled: true },
    tooltip: {
      confine: true,
      formatter: (input: unknown) => {
        const value = tooltipValue(input);
        return [`<strong>${symbol} ${optionType} IV</strong>`, `Log-moneyness ${formatMoneyness(Number(value[0]))}`, `DTE ${formatNumber(value[1], 0)}`, `Provider IV ${formatPercent(value[2])}`, value[3] === undefined ? "Interpolated surface" : `Observed strike ${formatNumber(value[3])}`].join("<br>");
      },
    },
    visualMap: { min: plot.ivDomain[0], max: plot.ivDomain[1], dimension: 2, calculable: true, orient: "vertical", right: 24, top: 120, itemWidth: 18, itemHeight: 240, text: ["High IV", "Low IV"], formatter: (value: number) => formatPercent(value), textStyle: { color: MUTED }, inRange: { color: SURFACE_COLORS } },
    xAxis3D: { type: "value", name: "Log-moneyness", axisLabel: { formatter: (value: number) => formatMoneyness(value) } },
    yAxis3D: { type: "value", name: "DTE" },
    zAxis3D: { type: "value", name: "Provider IV", axisLabel: { formatter: (value: number) => formatPercent(value) } },
    grid3D: {
      boxWidth: 150, boxDepth: 110, boxHeight: 72, top: 18, bottom: 52,
      environment: "#fbfcfa",
      axisLine: { lineStyle: { color: "#748178" } },
      axisPointer: { lineStyle: { color: "#0f766e", width: 2 }, label: { show: true } },
      splitLine: { lineStyle: { color: "rgba(104,117,109,.25)" } },
      viewControl: { projection: "perspective", alpha: 24, beta: -38, distance: 180, minDistance: 90, maxDistance: 300, rotateSensitivity: 0.8, zoomSensitivity: 0.7, panSensitivity: 0.6 },
      light: { main: { intensity: 1.15, shadow: true, alpha: 35, beta: 25 }, ambient: { intensity: 0.65 } },
      postEffect: { enable: true, SSAO: { enable: true, radius: 2, intensity: 1.2 }, FXAA: { enable: true } },
      temporalSuperSampling: { enable: true },
    },
    series: [
      { type: "surface", name: "Interpolated provider IV", coordinateSystem: "cartesian3D", data: plot.surfacePoints, dataShape: plot.dataShape, shading: "lambert", wireframe: { show: true, lineStyle: { color: "rgba(13,48,62,.18)", width: 0.7 } }, itemStyle: { opacity: 0.94 }, silent: false },
      { type: "scatter3D", name: "Observed provider IV", coordinateSystem: "cartesian3D", data: plot.observed.map((row) => [row.logMoneyness, row.dte, row.providerIV, row.strike]), symbolSize: 3.5, itemStyle: { color: OBSERVED, borderColor: "#16324f", borderWidth: 0.7, opacity: 0.78 } },
    ],
  } as unknown as EChartsCoreOption;
}

function lineOption(title: string, xTitle: string, yTitle: string, traces: Array<{ name: string; x: Array<number | string>; y: number[] }>, xType: "value" | "category"): EChartsCoreOption {
  const categoryData = xType === "category" ? [...new Set(traces.flatMap((trace) => trace.x.map(String)))] : undefined;
  return {
    animationDuration: 300,
    aria: { enabled: true },
    title: { text: title, left: 18, top: 14, textStyle: { color: INK, fontSize: 15, fontWeight: 600 } },
    color: [ACCENT, "#d97706", "#2563eb"],
    grid: { left: 58, right: 22, top: 62, bottom: 52 },
    legend: { top: 38, right: 18, textStyle: { color: MUTED, fontSize: 10 } },
    tooltip: { trigger: "axis", confine: true, valueFormatter: (value: unknown) => formatPercent(value) },
    xAxis: { type: xType, data: categoryData, name: xTitle, nameLocation: "middle", nameGap: 32, axisLabel: { color: MUTED, hideOverlap: true }, axisLine: { lineStyle: { color: MUTED } }, splitLine: { lineStyle: { color: GRID } } },
    yAxis: { type: "value", name: yTitle, axisLabel: { formatter: (value: number) => formatPercent(value), color: MUTED }, splitLine: { lineStyle: { color: GRID } } },
    series: traces.map((trace) => ({ type: "line", name: trace.name, showSymbol: true, symbolSize: 5, smooth: 0.14, data: trace.x.map((x, index) => xType === "category" ? trace.y[index] : [x, trace.y[index]]), lineStyle: { width: 2 } })),
  } as EChartsCoreOption;
}

function EChart({ option, className, label }: { option: EChartsCoreOption; className: string; label: string }) {
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!container.current) return;
    const chart = echarts.init(container.current, undefined, { renderer: "canvas" });
    chart.setOption(option, { notMerge: true, lazyUpdate: false });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(container.current);
    return () => { observer.disconnect(); chart.dispose(); };
  }, []);
  useEffect(() => {
    const chart = container.current ? echarts.getInstanceByDom(container.current) as EChartsType | undefined : undefined;
    chart?.setOption(option, { notMerge: true, lazyUpdate: false });
  }, [option]);
  return <div ref={container} className={className} role="img" aria-label={label} />;
}

function SegmentedButton({ active, disabled = false, onClick, children }: { active: boolean; disabled?: boolean; onClick: () => void; children: string }) {
  return <button type="button" disabled={disabled} onClick={onClick} className={`min-h-10 rounded-md border px-3 text-xs font-medium transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary disabled:cursor-not-allowed disabled:opacity-45 ${active ? "border-primary bg-primary text-primary-foreground" : "border-border bg-background text-muted-foreground hover:text-foreground"}`}>{children}</button>;
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="bg-card px-3 py-2"><p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">{label}</p><p className="mt-0.5 text-sm font-semibold tabular-nums">{value}</p></div>;
}

function tooltipData(input: unknown): Record<string, unknown> {
  if (!input || typeof input !== "object") return {};
  const data = (input as { data?: unknown }).data;
  return data && typeof data === "object" && !Array.isArray(data) ? data as Record<string, unknown> : {};
}

function tooltipValue(input: unknown): unknown[] {
  if (!input || typeof input !== "object") return [];
  const value = (input as { value?: unknown }).value;
  return Array.isArray(value) ? value : [];
}

function nearestIndex(values: number[], target: number): number {
  if (values.length === 0) return -1;
  let best = 0;
  for (let index = 1; index < values.length; index += 1) if (Math.abs(values[index]! - target) < Math.abs(values[best]! - target)) best = index;
  return best;
}

function useCompactChart(): boolean {
  const [compact, setCompact] = useState(() => window.matchMedia("(max-width: 639px)").matches);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 639px)");
    const update = () => setCompact(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  return compact;
}

function finiteNumber(value: unknown): value is number { return typeof value === "number" && Number.isFinite(value); }
function formatPercent(value: unknown): string { return finiteNumber(value) ? value.toLocaleString(undefined, { style: "percent", maximumFractionDigits: 1 }) : "—"; }
function formatNumber(value: unknown, digits = 2): string { return finiteNumber(value) ? value.toLocaleString(undefined, { maximumFractionDigits: digits }) : "—"; }
function formatMoneyness(value: number): string { return finiteNumber(value) ? `${value >= 0 ? "+" : ""}${(value * 100).toFixed(0)}%` : "—"; }
