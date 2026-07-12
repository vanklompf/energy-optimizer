import ReactECharts from "echarts-for-react";
import { api, PlanStep } from "../api";
import { usePolling } from "../hooks";

function buildOption(steps: PlanStep[]) {
  const x = steps.map((s) => s.interval_start.slice(5, 16).replace("T", " "));
  const soc = steps.map((s) => +s.soc_pct_end.toFixed(1));
  const gridImport = steps.map((s) => +(s.grid_to_load_kwh + s.grid_to_battery_kwh).toFixed(3));
  const gridExport = steps.map((s) => +(s.pv_to_grid_kwh + s.battery_to_grid_kwh).toFixed(3));
  const battCharge = steps.map((s) => +(s.pv_to_battery_kwh + s.grid_to_battery_kwh).toFixed(3));
  const battDischarge = steps.map((s) => +(s.battery_to_load_kwh + s.battery_to_grid_kwh).toFixed(3));

  return {
    tooltip: { trigger: "axis" },
    legend: { data: ["SoC %", "Grid import", "Grid export", "Charge", "Discharge"] },
    grid: { left: 48, right: 48, top: 40, bottom: 60 },
    xAxis: { type: "category", data: x, axisLabel: { rotate: 45, fontSize: 9 } },
    yAxis: [
      { type: "value", name: "kWh" },
      { type: "value", name: "SoC %", min: 0, max: 100, position: "right" },
    ],
    dataZoom: [{ type: "inside" }, { type: "slider", height: 16, bottom: 8 }],
    series: [
      { name: "Grid import", type: "bar", stack: "grid", data: gridImport, itemStyle: { color: "#e06c75" } },
      { name: "Grid export", type: "bar", stack: "grid", data: gridExport.map((v) => -v), itemStyle: { color: "#98c379" } },
      { name: "Charge", type: "bar", stack: "batt", data: battCharge, itemStyle: { color: "#61afef" } },
      { name: "Discharge", type: "bar", stack: "batt", data: battDischarge.map((v) => -v), itemStyle: { color: "#c678dd" } },
      { name: "SoC %", type: "line", yAxisIndex: 1, data: soc, smooth: true, symbol: "none", lineStyle: { width: 2, color: "#e5c07b" } },
    ],
  };
}

export default function PlanView() {
  const { data, error, loading } = usePolling(api.plan, 30000);

  if (loading && !data) return <div className="panel">Loading…</div>;
  if (error) return <div className="panel error">Error: {error}</div>;
  if (!data || !data.run) return <div className="panel">No plan yet. Waiting for an optimiser run.</div>;

  return (
    <div className="grid-single">
      <section className="panel">
        <h2>
          48h plan <span className="muted">({data.steps.length} steps, run {data.run.run_id.slice(0, 8)})</span>
        </h2>
        <ReactECharts option={buildOption(data.steps)} style={{ height: 460 }} notMerge />
      </section>
    </div>
  );
}
