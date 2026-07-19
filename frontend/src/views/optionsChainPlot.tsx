import Plot from "react-plotly.js";
import type { OptionHistoryCurves, OptionHistorySurface } from "@/api";

export function OptionSurfacePlot({ surface, optionType }: { surface: OptionHistorySurface; optionType: "call" | "put" }) {
  const z = surface.surfaces[optionType] ?? [];
  const data: any[] = [{
    type: "surface", x: surface.x, y: surface.y, z, colorscale: "Viridis", connectgaps: false,
    hovertemplate: "log-moneyness %{x:.3f}<br>DTE %{y}<br>IV %{z:.2%}<extra></extra>",
    contours: { z: { show: true, usecolormap: true, project: { z: true } } },
  }];
  const layout: any = {
    autosize: true, height: 560, margin: { l: 0, r: 0, b: 0, t: 30 },
    title: { text: `${optionType === "call" ? "Call" : "Put"} IV surface` },
    scene: { xaxis: { title: { text: "Log-moneyness" } }, yaxis: { title: { text: "DTE" } }, zaxis: { title: { text: "Provider IV" } } },
  };
  return <Plot data={data} layout={layout} config={{ responsive: true, displaylogo: false }} className="h-[560px] w-full" />;
}

export function OptionCurvePlots({ curves }: { curves: OptionHistoryCurves }) {
  const smiles = curves.smiles.map((smile, index) => ({
    type: "scatter", mode: "lines+markers", name: `${String(smile.expiration)} ${String(smile.option_type)}`,
    x: points(smile).map((point) => numberValue(point.moneyness)), y: points(smile).map((point) => numberValue(point.iv)),
  }));
  const term = curves.term_structure.map((row) => ({ x: numberValue(row.dte), y: numberValue(row.atm_iv), text: `${String(row.expiration)} ${String(row.option_type)}` }));
  const history = curves.history.map((row) => ({ x: String(row.slot_at ?? ""), y: numberValue(row.atm_iv), text: `${String(row.expiration)} ${String(row.option_type)}` }));
  const base: any = { autosize: true, height: 300, margin: { l: 52, r: 16, b: 42, t: 42 }, showlegend: false };
  return <div className="grid gap-4 2xl:grid-cols-3">
    <Plot data={smiles as any} layout={{ ...base, title: { text: "Volatility smiles" }, xaxis: { title: { text: "Log-moneyness" } }, yaxis: { title: { text: "Provider IV" }, tickformat: ".0%" } }} config={{ responsive: true, displaylogo: false }} className="h-[300px] w-full" />
    <Plot data={[{ type: "scatter", mode: "lines+markers", x: term.map((row) => row.x), y: term.map((row) => row.y), text: term.map((row) => row.text), hovertemplate: "%{text}<br>DTE %{x}<br>ATM IV %{y:.2%}<extra></extra>" }] as any} layout={{ ...base, title: { text: "ATM term structure" }, xaxis: { title: { text: "DTE" } }, yaxis: { title: { text: "ATM IV" }, tickformat: ".0%" } }} config={{ responsive: true, displaylogo: false }} className="h-[300px] w-full" />
    <Plot data={[{ type: "scatter", mode: "lines+markers", x: history.map((row) => row.x), y: history.map((row) => row.y), text: history.map((row) => row.text), hovertemplate: "%{text}<br>%{x}<br>ATM IV %{y:.2%}<extra></extra>" }] as any} layout={{ ...base, title: { text: "Historical ATM IV" }, xaxis: { title: { text: "As of" } }, yaxis: { title: { text: "ATM IV" }, tickformat: ".0%" } }} config={{ responsive: true, displaylogo: false }} className="h-[300px] w-full" />
  </div>;
}

function points(smile: Record<string, unknown>): Array<Record<string, unknown>> { return Array.isArray(smile.points) ? smile.points.filter((point): point is Record<string, unknown> => Boolean(point && typeof point === "object")) : []; }
function numberValue(value: unknown): number | null { return typeof value === "number" && Number.isFinite(value) ? value : null; }
