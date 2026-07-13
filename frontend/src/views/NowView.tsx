import ReactECharts from "echarts-for-react";
import { api } from "../api";
import { usePolling } from "../hooks";

const AXIS_COLOR = "#8a909c";

function priceChartOption(prices: { interval_start: string; buy_gross: number | null; sell_gross: number | null }[], nowIso: string) {
  const buy = prices.map((p) => [p.interval_start, p.buy_gross] as [string, number | null]);
  const sell = prices.map((p) => [p.interval_start, p.sell_gross] as [string, number | null]);
  return {
    tooltip: { trigger: "axis" },
    legend: { data: ["Buy", "Sell"], textStyle: { color: AXIS_COLOR }, top: 0 },
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
      splitLine: { lineStyle: { color: "#ffffff10" } },
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

function fmt(v: number | null | undefined, unit = "", digits = 2): string {
  if (v === null || v === undefined) return "—";
  return `${v.toFixed(digits)}${unit}`;
}

function socGauge(soc: number | null) {
  return {
    series: [
      {
        type: "gauge",
        startAngle: 210,
        endAngle: -30,
        min: 0,
        max: 100,
        progress: { show: true, width: 14 },
        axisLine: { lineStyle: { width: 14 } },
        axisLabel: { distance: 18, fontSize: 10 },
        pointer: { show: false },
        detail: {
          valueAnimation: true,
          formatter: "{value}%",
          fontSize: 28,
          offsetCenter: [0, "0%"],
        },
        data: [{ value: soc ?? 0, name: "SoC" }],
      },
    ],
  };
}

const STATUS_CLASS: Record<string, string> = {
  ok: "badge-ok",
  low_confidence: "badge-warn",
  blocked: "badge-block",
};

export default function NowView() {
  const { data, error, loading } = usePolling(api.status, 15000);

  if (loading && !data) return <div className="panel">Loading…</div>;
  if (error) return <div className="panel error">Error: {error}</div>;
  if (!data) return null;

  const t = data.telemetry;
  const price = data.current_price;
  const run = data.last_run;

  return (
    <>
    <PriceChart />
    <div className="grid">
      <section className="panel">
        <h2>Battery</h2>
        <ReactECharts option={socGauge(t?.soc_pct ?? null)} style={{ height: 220 }} />
        {t?.stale && <div className="badge badge-warn">telemetry stale</div>}
      </section>

      <section className="panel">
        <h2>Live power</h2>
        <ul className="metrics">
          <li><span>PV</span><b>{fmt(t?.pv_kw, " kW")}</b></li>
          <li><span>Load</span><b>{fmt(t?.load_kw, " kW")}</b></li>
          <li><span>Battery charge</span><b>{fmt(t?.batt_charge_kw, " kW")}</b></li>
          <li><span>Battery discharge</span><b>{fmt(t?.batt_discharge_kw, " kW")}</b></li>
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
              <li><span>Solve time</span><b>{fmt(run.solve_ms, " ms", 1)}</b></li>
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
