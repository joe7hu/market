import { useMemo, useState } from "react";
import Plot from "react-plotly.js";
import type { OptionHistoryCurves, OptionHistorySurface } from "@/api";
import { buildOptionCurvePlotData, curveChoices } from "./optionsChainPlotData";

export function OptionSurfacePlot({ surface, optionType }: { surface: OptionHistorySurface; optionType: "call" | "put" }) {
  const z = surface.surfaces[optionType] ?? [];
  const observed = surface.observed
    .filter((row) => row.option_type === optionType && finiteNumber(row.log_moneyness) && finiteNumber(row.dte) && finiteNumber(row.provider_iv));
  const data: any[] = [{
    type: "surface", name: "Interpolated grid", x: surface.x, y: surface.y, z, colorscale: "Viridis", connectgaps: false,
    hovertemplate: "Interpolated grid IV<br>log-moneyness %{x:.3f}<br>DTE %{y}<br>IV %{z:.2%}<extra></extra>",
    contours: { z: { show: true, usecolormap: true, project: { z: true } } },
  }, {
    type: "scatter3d", mode: "markers", name: "Observed provider IV",
    x: observed.map((row) => row.log_moneyness), y: observed.map((row) => row.dte), z: observed.map((row) => row.provider_iv),
    marker: { size: 2, color: "#f8fafc", opacity: 0.7 },
    hovertemplate: "Observed provider IV<br>log-moneyness %{x:.3f}<br>DTE %{y}<br>IV %{z:.2%}<extra></extra>",
  }];
  const layout: any = {
    autosize: true, height: 560, margin: { l: 0, r: 0, b: 0, t: 30 },
    title: { text: `${optionType === "call" ? "Call" : "Put"} IV surface` },
    scene: { xaxis: { title: { text: "Log-moneyness" } }, yaxis: { title: { text: "DTE" } }, zaxis: { title: { text: "Provider IV" } } },
  };
  return <Plot data={data} layout={layout} config={{ responsive: true, displaylogo: false }} className="h-[560px] w-full" />;
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
