import ReactECharts from "echarts-for-react";
import { api, PlanStep } from "../api";
import { usePolling } from "../hooks";

const TEXT_COLOR = "#dcdfe4";
const AXIS_COLOR = "#8a909c";
const INACTIVE_COLOR = "#6b7280";
const SPLIT_COLOR = "#ffffff10";

function priceChartOption(prices: { interval_start: string; buy_gross: number | null; sell_gross: number | null }[], nowIso: string) {
  const buy = prices.map((p) => [p.interval_start, p.buy_gross] as [string, number | null]);
  const sell = prices.map((p) => [p.interval_start, p.sell_gross] as [string, number | null]);
  return {
    tooltip: { trigger: "axis" },
    legend: { data: ["Buy", "Sell"], textStyle: { color: TEXT_COLOR }, inactiveColor: INACTIVE_COLOR, top: 0 },
    grid: { left: 52, right: 16, top: 34, bottom: 28 },
    xAxis: {
      type: "time",
      axisLabel: { color: AXIS_COLOR },
      axisLine: { lineStyle: { color: "#ffffff22" } },
    },
    yAxis: {
      type: "value",
      name: "PLN/kWh",
      nameTextStyle: { color: AXIS_COLOR },
      axisLabel: { color: AXIS_COLOR },
      splitLine: { lineStyle: { color: SPLIT_COLOR } },
    },
    series: [
      {
        name: "Buy",
        type: "line",
        step: "end",
        showSymbol: false,
        data: buy,
        itemStyle: { color: "#61afef" },
        markLine: {
          symbol: "none",
          data: [{ xAxis: nowIso }],
          lineStyle: { color: "#e5c07b", type: "dashed" },
          label: { formatter: "now", color: "#e5c07b" },
        },
      },
      {
        name: "Sell",
        type: "line",
        step: "end",
        showSymbol: false,
        data: sell,
        itemStyle: { color: "#98c379" },
      },
    ],
  };
}

function PriceChart() {
  const { data, error } = usePolling(() => api.prices(12, 24), 60000);
  return (
    <div className="grid-single" style={{ marginBottom: 16 }}>
      <section className="panel">
        <h2>Prices — past &amp; forecast</h2>
        {error && <div className="badge badge-block">{error}</div>}
        {data && data.prices.length > 0 ? (
          <ReactECharts option={priceChartOption(data.prices, data.now)} style={{ height: 260 }} notMerge />
        ) : (
          <p className="muted">No price history yet.</p>
        )}
      </section>
    </div>
  );
}

function planChartOption(steps: PlanStep[]) {
  const x = steps.map((s) => s.interval_start.slice(5, 16).replace("T", " "));
  const soc = steps.map((s) => +s.soc_pct_end.toFixed(1));
  const gridImport = steps.map((s) => +(s.grid_to_load_kwh + s.grid_to_battery_kwh).toFixed(3));
  const gridExport = steps.map((s) => +(s.pv_to_grid_kwh + s.battery_to_grid_kwh).toFixed(3));
  const battCharge = steps.map((s) => +(s.pv_to_battery_kwh + s.grid_to_battery_kwh).toFixed(3));
  const battDischarge = steps.map((s) => +(s.battery_to_load_kwh + s.battery_to_grid_kwh).toFixed(3));

  return {
    tooltip: { trigger: "axis" },
    legend: {
      data: ["SoC %", "Grid import", "Grid export", "Charge", "Discharge"],
      textStyle: { color: TEXT_COLOR },
      inactiveColor: INACTIVE_COLOR,
    },
    grid: { left: 48, right: 48, top: 40, bottom: 60 },
    xAxis: { type: "category", data: x, axisLabel: { rotate: 45, fontSize: 9, color: AXIS_COLOR } },
    yAxis: [
      {
        type: "value",
        name: "kWh",
        nameTextStyle: { color: AXIS_COLOR },
        axisLabel: { color: AXIS_COLOR },
        splitLine: { lineStyle: { color: SPLIT_COLOR } },
      },
      {
        type: "value",
        name: "SoC %",
        min: 0,
        max: 100,
        position: "right",
        nameTextStyle: { color: AXIS_COLOR },
        axisLabel: { color: AXIS_COLOR },
        splitLine: { show: false },
      },
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

function PlanChart() {
  const { data, error } = usePolling(api.plan, 30000);
  return (
    <div className="grid-single" style={{ marginBottom: 16 }}>
      <section className="panel">
        <h2>
          48h plan{" "}
          {data?.run && (
            <span className="muted">
              ({data.steps.length} steps, run {data.run.run_id.slice(0, 8)})
            </span>
          )}
        </h2>
        {error && <div className="badge badge-block">{error}</div>}
        {data && data.run && data.steps.length > 0 ? (
          <ReactECharts option={planChartOption(data.steps)} style={{ height: 460 }} notMerge />
        ) : (
          <p className="muted">No plan yet. Waiting for an optimiser run.</p>
        )}
      </section>
    </div>
  );
}

function fmt(v: number | null | undefined, unit = "", digits = 2): string {
  if (v === null || v === undefined) return "—";
  return `${v.toFixed(digits)}${unit}`;
}

const STATUS_CLASS: Record<string, string> = {
  ok: "badge-ok",
  low_confidence: "badge-warn",
  blocked: "badge-block",
};

export default function NowView() {
  const { data, error, loading } = usePolling(api.status, 15000);
  const { data: savings } = usePolling(api.savings, 300000);

  if (loading && !data) return <div className="panel">Loading…</div>;
  if (error) return <div className="panel error">Error: {error}</div>;
  if (!data) return null;

  const t = data.telemetry;
  const price = data.current_price;
  const run = data.last_run;

  return (
    <>
      <PriceChart />
      <PlanChart />
      <div className="grid">
        <section className="panel">
          <h2>Live power</h2>
          {t?.stale && <div className="badge badge-warn" style={{ marginBottom: 10 }}>telemetry stale</div>}
          <ul className="metrics">
            <li><span>Battery SoC</span><b>{fmt(t?.soc_pct, " %", 1)}</b></li>
            <li><span>Battery charge</span><b>{fmt(t?.batt_charge_kw, " kW")}</b></li>
            <li><span>Battery discharge</span><b>{fmt(t?.batt_discharge_kw, " kW")}</b></li>
            <li><span>PV</span><b>{fmt(t?.pv_kw, " kW")}</b></li>
            <li><span>Load</span><b>{fmt(t?.load_kw, " kW")}</b></li>
            <li><span>Grid import</span><b>{fmt(t?.grid_import_kw, " kW")}</b></li>
            <li><span>Grid export</span><b>{fmt(t?.grid_export_kw, " kW")}</b></li>
            <li><span>EMS mode</span><b>{t?.ems_mode ?? "—"}</b></li>
          </ul>
        </section>

        <section className="panel">
          <h2>Price now</h2>
          <ul className="metrics">
            <li><span>Buy</span><b>{fmt(price?.buy_gross, " PLN/kWh", 3)}</b></li>
            <li><span>Sell</span><b>{fmt(price?.sell_gross, " PLN/kWh", 3)}</b></li>
            <li>
              <span>Flag</span>
              <b>
                {price?.is_expensive ? "expensive" : price?.is_cheap ? "cheap" : "normal"}
              </b>
            </li>
            <li><span>Source</span><b>{price?.source ?? "—"}</b></li>
          </ul>
        </section>

        <section className="panel">
          <h2>Recommendation</h2>
          {run ? (
            <>
              <div className={`badge ${STATUS_CLASS[run.status] ?? ""}`}>{run.status}</div>
              <p className="reason">{run.reason ?? "No reason recorded"}</p>
              <ul className="metrics">
                <li><span>Expected value</span><b>{fmt(run.objective_pln ? -run.objective_pln : null, " PLN")}</b></li>
                <li><span>Known prices</span><b>{fmt(run.known_price_hours, " h", 0)}</b></li>
                <li title="Realised: measured cost minus optimiser cost over recorded data">
                  <span>Saved today</span><b>{fmt(savings?.day.savings_pln, " PLN")}</b>
                </li>
                <li title="Realised: measured cost minus optimiser cost over recorded data">
                  <span>Saved 7 days</span><b>{fmt(savings?.week.savings_pln, " PLN")}</b>
                </li>
              </ul>
            </>
          ) : (
            <p>No optimiser run yet.</p>
          )}
        </section>
      </div>
    </>
  );
}
