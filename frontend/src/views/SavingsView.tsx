import { useState } from "react";
import ReactECharts from "echarts-for-react";
import { api, BacktestResponse } from "../api";

function isoDaysAgo(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - days);
  d.setUTCHours(0, 0, 0, 0);
  return d.toISOString();
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
  );
}
