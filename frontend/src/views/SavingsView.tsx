import { useState } from "react";
import ReactECharts from "echarts-for-react";
import { api, BacktestResponse, HourlyComparisonPoint } from "../api";
import { usePolling } from "../hooks";

const TEXT_COLOR = "#dcdfe4";
const AXIS_COLOR = "#8a909c";
const INACTIVE_COLOR = "#6b7280";
const SPLIT_COLOR = "#ffffff10";

function isoDaysAgo(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - days);
  d.setUTCHours(0, 0, 0, 0);
  return d.toISOString();
}

// Charge shown as positive bars, discharge as negative, so battery action reads at a glance.
function comparisonChartOption(points: HourlyComparisonPoint[]) {
  const x = points.map((p) => p.interval_start.slice(5, 16).replace("T", " "));
  const actualCharge = points.map((p) => +p.actual_charge_kwh.toFixed(3));
  const actualDischarge = points.map((p) => -+p.actual_discharge_kwh.toFixed(3));
  const optCharge = points.map((p) =>
    p.optimiser_charge_kwh === null ? null : +p.optimiser_charge_kwh.toFixed(3)
  );
  const optDischarge = points.map((p) =>
    p.optimiser_discharge_kwh === null ? null : -+p.optimiser_discharge_kwh.toFixed(3)
  );
  const buy = points.map((p) => +p.buy_price.toFixed(3));
  const actualSoc = points.map((p) => p.actual_soc_pct);
  const optSoc = points.map((p) => p.optimiser_soc_pct);

  return {
    tooltip: { trigger: "axis" },
    legend: {
      data: [
        "Actual charge",
        "Actual discharge",
        "Optimiser charge",
        "Optimiser discharge",
        "Buy price",
        "Actual SoC",
        "Optimiser SoC",
      ],
      selected: { "Actual SoC": false, "Optimiser SoC": false },
      textStyle: { color: TEXT_COLOR },
      inactiveColor: INACTIVE_COLOR,
      top: 0,
      type: "scroll",
    },
    grid: { left: 52, right: 56, top: 56, bottom: 60 },
    xAxis: {
      type: "category",
      data: x,
      axisLabel: { color: AXIS_COLOR, rotate: 45, fontSize: 9 },
      axisLine: { lineStyle: { color: "#ffffff22" } },
    },
    yAxis: [
      {
        type: "value",
        name: "kWh  (charge + / discharge −)",
        nameTextStyle: { color: AXIS_COLOR },
        axisLabel: { color: AXIS_COLOR },
        splitLine: { lineStyle: { color: SPLIT_COLOR } },
      },
      {
        type: "value",
        name: "PLN/kWh · SoC %",
        position: "right",
        nameTextStyle: { color: AXIS_COLOR },
        axisLabel: { color: AXIS_COLOR },
        splitLine: { show: false },
      },
    ],
    dataZoom: [{ type: "inside" }, { type: "slider", height: 16, bottom: 8 }],
    series: [
      { name: "Actual charge", type: "bar", stack: "actual", data: actualCharge, itemStyle: { color: "#61afef" } },
      { name: "Actual discharge", type: "bar", stack: "actual", data: actualDischarge, itemStyle: { color: "#c678dd" } },
      { name: "Optimiser charge", type: "bar", stack: "optimiser", data: optCharge, itemStyle: { color: "#98c379" } },
      { name: "Optimiser discharge", type: "bar", stack: "optimiser", data: optDischarge, itemStyle: { color: "#e5c07b" } },
      { name: "Buy price", type: "line", yAxisIndex: 1, data: buy, step: "middle", showSymbol: false, lineStyle: { width: 1.5, color: "#e06c75", type: "dashed" } },
      { name: "Actual SoC", type: "line", yAxisIndex: 1, data: actualSoc, smooth: true, showSymbol: false, connectNulls: true, lineStyle: { width: 2, color: "#56b6c2" } },
      { name: "Optimiser SoC", type: "line", yAxisIndex: 1, data: optSoc, smooth: true, showSymbol: false, connectNulls: true, lineStyle: { width: 2, color: "#d19a66", type: "dashed" } },
    ],
  };
}

function ComparisonChart() {
  const [hours, setHours] = useState(48);
  const { data, error, loading } = usePolling(() => api.hourlyComparison(hours), 300000);
  const points = data?.points ?? [];
  const infeasible = data?.optimiser_status && data.optimiser_status !== "optimal";

  return (
    <div className="grid-single" style={{ marginBottom: 16 }}>
      <section className="panel">
        <h2>
          Charge / discharge — actual vs optimiser{" "}
          <span className="muted">(hourly)</span>
        </h2>
        <div className="controls">
          <label>
            Range
            <select value={hours} onChange={(e) => setHours(Number(e.target.value))}>
              <option value={24}>24 h</option>
              <option value={48}>48 h</option>
              <option value={72}>72 h</option>
              <option value={168}>7 days</option>
              <option value={336}>14 days</option>
            </select>
          </label>
        </div>
        {error && <div className="badge badge-block">{error}</div>}
        {infeasible && (
          <div className="badge badge-warn">optimiser: {data?.optimiser_status}</div>
        )}
        {points.length > 0 ? (
          <ReactECharts option={comparisonChartOption(points)} style={{ height: 420 }} notMerge />
        ) : (
          <p className="muted">{loading ? "Loading…" : "No history yet."}</p>
        )}
      </section>
    </div>
  );
}

export default function SavingsView() {
  const [result, setResult] = useState<BacktestResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [start, setStart] = useState(isoDaysAgo(7).slice(0, 16));
  const [end, setEnd] = useState(isoDaysAgo(0).slice(0, 16));

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.backtest({
        start: new Date(start).toISOString(),
        end: new Date(end).toISOString(),
        policies: ["pv_only", "self_consumption"],
      });
      setResult(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const chart = result && {
    tooltip: { trigger: "axis" },
    grid: { left: 60, right: 24, top: 24, bottom: 40 },
    xAxis: { type: "category", data: result.results.map((r) => r.policy) },
    yAxis: { type: "value", name: "Net cost PLN" },
    series: [
      {
        type: "bar",
        data: result.results.map((r) => +r.net_cost_pln.toFixed(2)),
        itemStyle: { color: "#61afef" },
        label: { show: true, position: "top" },
      },
    ],
  };

  return (
    <>
      <ComparisonChart />
      <div className="grid-single">
      <section className="panel">
        <h2>Backtest / counterfactuals</h2>
        <div className="controls">
          <label>
            Start
            <input type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} />
          </label>
          <label>
            End
            <input type="datetime-local" value={end} onChange={(e) => setEnd(e.target.value)} />
          </label>
          <button className="btn" onClick={run} disabled={busy}>
            {busy ? "Running…" : "Run backtest"}
          </button>
        </div>
        {error && <div className="badge badge-block">{error}</div>}
        {result && (
          <>
            <p className="muted">{result.intervals} intervals valued</p>
            <ReactECharts option={chart} style={{ height: 320 }} notMerge />
            <table className="table">
              <thead>
                <tr>
                  <th>Policy</th>
                  <th>Net cost (PLN)</th>
                  <th>Import (kWh)</th>
                  <th>Export (kWh)</th>
                  <th>Throughput (kWh)</th>
                </tr>
              </thead>
              <tbody>
                {result.results.map((r) => (
                  <tr key={r.policy}>
                    <td>{r.policy}</td>
                    <td>{r.net_cost_pln.toFixed(2)}</td>
                    <td>{r.import_kwh.toFixed(2)}</td>
                    <td>{r.export_kwh.toFixed(2)}</td>
                    <td>{r.battery_throughput_kwh.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </section>
      </div>
    </>
  );
}
