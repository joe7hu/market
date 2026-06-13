import { useEffect, useMemo, useRef } from "react";
import {
  ColorType,
  CrosshairMode,
  createChart,
  LineSeries,
  PriceScaleMode,
} from "lightweight-charts";

import type { MetricPoint } from "./types";
import { metricLineData, metricPriceFormat } from "./format";

export function LightweightValuationChart({ data, suffix, metricLabel }: { data: MetricPoint[]; suffix: string; metricLabel: string }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const metricData = useMemo(() => metricLineData(data, "value"), [data]);
  const overlayData = useMemo(() => metricLineData(data, "index_price"), [data]);

  useEffect(() => {
    const element = containerRef.current;
    if (!element || !metricData.length) return;

    const chart = createChart(element, {
      autoSize: true,
      height: 360,
      layout: {
        background: { type: ColorType.Solid, color: "#ffffff" },
        textColor: "#64748b",
        fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif",
      },
      grid: {
        vertLines: { color: "#f1f5f9" },
        horzLines: { color: "#f1f5f9" },
      },
      leftPriceScale: {
        visible: overlayData.length > 0,
        mode: PriceScaleMode.Logarithmic,
        borderColor: "#e2e8f0",
      },
      rightPriceScale: {
        borderColor: "#e2e8f0",
      },
      timeScale: {
        borderColor: "#e2e8f0",
        rightOffset: 2,
        minBarSpacing: 0.001,
        timeVisible: false,
      },
      crosshair: {
        mode: CrosshairMode.Normal,
      },
      handleScroll: false,
      handleScale: false,
    });

    if (overlayData.length) {
      const overlay = chart.addSeries(LineSeries, {
        color: "rgba(59, 130, 246, 0.35)",
        lineWidth: 1,
        priceScaleId: "left",
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: true,
        crosshairMarkerRadius: 3,
        priceFormat: {
          type: "price",
          precision: 0,
          minMove: 1,
        },
      });
      overlay.setData(overlayData);
    }

    const metric = chart.addSeries(LineSeries, {
      color: "#64748b",
      lineWidth: 2,
      priceScaleId: "right",
      priceLineVisible: false,
      lastValueVisible: true,
      crosshairMarkerVisible: true,
      crosshairMarkerRadius: 4,
      title: metricLabel,
      priceFormat: metricPriceFormat(suffix),
    });
    metric.setData(metricData);
    chart.timeScale().fitContent();

    return () => chart.remove();
  }, [metricData, metricLabel, overlayData, suffix]);

  return <div ref={containerRef} className="h-[360px] min-h-[360px] w-full overflow-hidden rounded-lg" />;
}
