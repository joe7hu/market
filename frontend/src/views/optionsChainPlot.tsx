import { useMemo, useState } from "react";
import Plot from "react-plotly.js";
import type { OptionHistoryCurves, OptionHistorySurface, OptionHistorySurfaceGrid } from "@/api";
import { buildOptionCurvePlotData, buildProviderIVSurfaceData, curveChoices } from "./optionsChainPlotData";

export function OptionSurfacePlot({ surface }: { surface: OptionHistorySurface }) {
  const observed = surface.observed.filter((row) => finiteNumber(row.strike) && finiteNumber(row.provider_iv));
  const fitted = surface.fitted.filter((row) => finiteNumber(row.strike) && finiteNumber(row.fair_low) && finiteNumber(row.fair_high));
  const data: any[] = [{
    type: "scatter", mode: "markers", name: "Observed provider IV",
    x: observed.map((row) => row.strike), y: observed.map((row) => row.provider_iv),
    marker: { size: 5, color: "#38bdf8" }, hovertemplate: "Strike %{x:.2f}<br>Provider IV %{y:.2%}<extra></extra>",
  }, {
    type: "scatter", mode: "lines", name: "Fair-value band",
    x: fitted.map((row) => row.strike), y: fitted.map((row) => midpoint(row.fair_low, row.fair_high)),
    line: { color: "#a78bfa" }, hovertemplate: "Strike %{x:.2f}<br>Fitted IV %{y:.2%}<extra></extra>",
  }];
  const layout: any = {
    autosize: true, height: 560, margin: { l: 0, r: 0, b: 0, t: 30 },
    title: { text: `${surface.expiration} ${surface.option_type} evidence` },
    xaxis: { title: { text: "Strike" } }, yaxis: { title: { text: "Provider / fitted IV" }, tickformat: ".0%" },
  };
  return <Plot data={data} layout={layout} config={{ responsive: true, displaylogo: false }} className="h-[560px] w-full" />;
}

export function OptionSurface3dPlot({ surface, optionType }: { surface: OptionHistorySurfaceGrid; optionType: "call" | "put" }) {
  const plot = useMemo(() => buildProviderIVSurfaceData(surface, optionType), [surface, optionType]);
  const hasGrid = plot.z.some((row) => row.some((value) => value !== null));
  if (!hasGrid) return <p className="p-4 text-sm text-muted-foreground">No provider-IV grid is available for this snapshot.</p>;
  const data: any[] = [{
    type: "surface", name: "Interpolated provider IV", x: plot.x, y: plot.y, z: plot.z,
    colorscale: "Viridis", connectgaps: false, showscale: true,
    colorbar: { title: { text: "Provider IV" }, tickformat: ".0%" },
    hovertemplate: "Interpolated provider IV<br>Log-moneyness %{x:.3f}<br>DTE %{y}<br>IV %{z:.2%}<extra></extra>",
  }, {
    type: "scatter3d", mode: "markers", name: "Observed provider IV",
    x: plot.observed.map((row) => row.logMoneyness), y: plot.observed.map((row) => row.dte), z: plot.observed.map((row) => row.providerIV),
    text: plot.observed.map((row) => row.strike === null ? "Strike —" : `Strike ${row.strike.toFixed(2)}`),
    marker: { size: 2, color: "#f8fafc", opacity: 0.7 },
    hovertemplate: "%{text}<br>Observed provider IV<br>Log-moneyness %{x:.3f}<br>DTE %{y}<br>IV %{z:.2%}<extra></extra>",
  }];
  const layout: any = {
    autosize: true, height: 620, margin: { l: 0, r: 0, b: 0, t: 34 },
    title: { text: `${surface.symbol} ${optionType} provider-IV surface` },
    scene: {
      xaxis: { title: { text: "Log-moneyness" } },
      yaxis: { title: { text: "DTE" } },
      zaxis: { title: { text: "Provider IV" }, tickformat: ".0%" },
    },
  };
  return <Plot data={data} layout={layout} config={{ responsive: true, displaylogo: false }} className="h-[620px] w-full" />;
}

export function OptionCurvePlots({ curves }: { curves: OptionHistoryCurves }) {
  const choices = useMemo(() => curveChoices(curves), [curves]);
  const [requestedKey, setRequestedKey] = useState("");
  const selectedKey = choices.some((choice) => choice.key === requestedKey) ? requestedKey : (choices[0]?.key ?? "");
  const plots = useMemo(() => buildOptionCurvePlotData(curves, selectedKey), [curves, selectedKey]);
  const base: any = { autosize: true, height: 300, margin: { l: 52, r: 16, b: 42, t: 42 }, showlegend: false };
  return <div className="space-y-3">
    <label className="grid max-w-sm gap-1 text-xs text-muted-foreground">Smile and history series<select value={plots.selectedKey} onChange={(event) => setRequestedKey(event.target.value)} className="h-9 rounded-md border border-input bg-background px-2 text-sm text-foreground">{choices.map((choice) => <option key={choice.key} value={choice.key}>{choice.label}</option>)}</select></label>
    <div className="grid gap-4 2xl:grid-cols-3">
      <Plot data={plots.smile ? [{ type: "scatter", mode: "lines+markers", ...plots.smile, hovertemplate: "%{text}<br>Log-moneyness %{x:.3f}<br>IV %{y:.2%}<extra></extra>" }] as any : []} layout={{ ...base, title: { text: "Volatility smile" }, xaxis: { title: { text: "Log-moneyness" } }, yaxis: { title: { text: "Provider IV" }, tickformat: ".0%" } }} config={{ responsive: true, displaylogo: false }} className="h-[300px] w-full" />
      <Plot data={plots.term.map((trace) => ({ type: "scatter", mode: "lines+markers", ...trace, hovertemplate: "%{text}<br>DTE %{x}<br>ATM IV %{y:.2%}<extra></extra>" })) as any} layout={{ ...base, title: { text: "ATM term structure" }, showlegend: true, xaxis: { title: { text: "DTE" } }, yaxis: { title: { text: "ATM IV" }, tickformat: ".0%" } }} config={{ responsive: true, displaylogo: false }} className="h-[300px] w-full" />
      <Plot data={plots.history ? [{ type: "scatter", mode: "lines+markers", ...plots.history, hovertemplate: "%{text}<br>%{x}<br>ATM IV %{y:.2%}<extra></extra>" }] as any : []} layout={{ ...base, title: { text: "Historical ATM IV" }, xaxis: { title: { text: "Capture completed" } }, yaxis: { title: { text: "ATM IV" }, tickformat: ".0%" } }} config={{ responsive: true, displaylogo: false }} className="h-[300px] w-full" />
    </div>
  </div>;
}

function finiteNumber(value: unknown): value is number { return typeof value === "number" && Number.isFinite(value); }
function midpoint(low: unknown, high: unknown) { return finiteNumber(low) && finiteNumber(high) ? (low + high) / 2 : null; }
