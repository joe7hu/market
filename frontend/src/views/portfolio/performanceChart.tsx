import { useEffect, useMemo, useRef } from "react";
import { AreaSeries, ColorType, CrosshairMode, createChart, type Time } from "lightweight-charts";

import type { RowRecord } from "@/types";
import { numberField, textField } from "@/views/rowFormat";

export function PortfolioPerformanceChart({ rows }: { rows: RowRecord[] }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const points = useMemo(() => rows
    .map((row) => ({ time: textField(row, ["date"]) as Time, value: numberField(row, ["total_pnl"]) }))
    .filter((point) => Boolean(point.time)), [rows]);

  useEffect(() => {
    const element = containerRef.current;
    if (!element || !points.length) return;
    const positive = (points.at(-1)?.value ?? 0) >= 0;
    const lineColor = positive ? "#15803d" : "#b91c1c";
    const chart = createChart(element, {
      autoSize: true,
      height: 300,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#64748b",
        fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif",
      },
      grid: { vertLines: { color: "#eef2f7" }, horzLines: { color: "#eef2f7" } },
      rightPriceScale: { borderColor: "#e2e8f0" },
      timeScale: { borderColor: "#e2e8f0", rightOffset: 2, timeVisible: false },
      crosshair: { mode: CrosshairMode.Normal },
      handleScroll: false,
      handleScale: false,
    });
    const series = chart.addSeries(AreaSeries, {
      lineColor,
      topColor: positive ? "rgba(21, 128, 61, 0.22)" : "rgba(185, 28, 28, 0.20)",
      bottomColor: "rgba(255, 255, 255, 0)",
      lineWidth: 2,
      priceLineVisible: false,
      priceFormat: { type: "price", precision: 2, minMove: 0.01 },
      title: "Total P&L",
    });
    series.setData(points);
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [points]);

  return <div ref={containerRef} aria-label="Portfolio total profit and loss chart" className="h-[300px] min-h-[300px] w-full" />;
}
