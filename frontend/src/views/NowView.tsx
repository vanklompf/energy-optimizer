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
  const evCharge = steps.map((s) => +s.ev_charge_kwh.toFixed(3));

  return {
    tooltip: { trigger: "axis" },
    legend: {
      data: ["SoC %", "Grid import", "Grid export", "Charge", "Discharge", "Car charge"],
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
      { name: "Car charge", type: "bar", data: evCharge, itemStyle: { color: "#56b6c2" } },
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

const CONTROL_BADGE: Record<string, { label: string; className: string }> = {
  DRY_RUN: { label: "DRY RUN", className: "badge-dryrun" },
  DISARMED: { label: "DISARMED", className: "badge-dryrun" },
  PREFLIGHT: { label: "PREFLIGHT", className: "badge-warn" },
  ARMED_IDLE: { label: "ARMED", className: "badge-ok" },
  ACTIVE_CHARGE: { label: "CHARGING", className: "badge-ok" },
  ACTIVE_DISCHARGE: { label: "DISCHARGING", className: "badge-ok" },
  FALLBACK: { label: "FALLBACK", className: "badge-warn" },
  LOCKOUT: { label: "LOCKOUT", className: "badge-block" },
};

export default function NowView() {
  const { data, error, loading } = usePolling(api.status, 15000);
  const { data: savings } = usePolling(api.savings, 300000);

  if (loading && !data) return <div className="panel">Loading…</div>;
  if (error) return <div className="panel error">Error: {error}</div>;
  if (!data) return null;

  const t = data.telemetry;
  const price = data.current_price;
  const meter = data.billing_meter;
  const run = data.last_run;
  const loadDiagnostics = run?.safety?.forecast_diagnostics?.load;
  const ev = data.ev;
  const evControl = data.ev_control;
  const bc = data.battery_control;
  const controlBadge = CONTROL_BADGE[bc.effective_state] ?? {
    label: bc.effective_state,
    className: "badge-warn",
  };
  const intent = bc.current_intent;
  const requestedPower =
    intent && typeof intent.requested_power_kw === "number"
      ? intent.requested_power_kw
      : null;
  const measuredCharge = t?.batt_charge_kw ?? null;
  const measuredDischarge = t?.batt_discharge_kw ?? null;
  const measuredPower =
    measuredCharge != null || measuredDischarge != null
      ? (measuredCharge ?? 0) - (measuredDischarge ?? 0)
      : null;

  return (
    <>
      <div className="panel" style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <div className={`badge ${controlBadge.className}`} style={{ fontSize: 14, padding: "6px 14px" }}>
            {controlBadge.label}
          </div>
          <span className="muted">
            mode={bc.mode} · enabled={String(bc.battery_control_enabled)} · gates=
            {bc.gates_ok ? "ok" : "blocked"} · watchdog=
            {bc.watchdog_healthy ? "healthy" : "unproven"}
          </span>
        </div>
        <ul className="metrics" style={{ marginTop: 12 }}>
          <li><span>Controller state</span><b>{bc.controller_state}</b></li>
          <li><span>Lease</span><b>{bc.lease.held ? "held" : "free"}</b></li>
          <li><span>Requested power</span><b>{fmt(requestedPower, " kW")}</b></li>
          <li><span>Measured battery power</span><b>{fmt(measuredPower, " kW")}</b></li>
          <li><span>Last result</span><b>{bc.last_action?.result ?? "—"}</b></li>
          <li><span>Last latency</span><b>{fmt(bc.last_action?.latency_ms, " ms", 0)}</b></li>
          <li><span>Last fallback</span><b>{bc.last_fallback_at ? new Date(bc.last_fallback_at).toLocaleString() : "—"}</b></li>
          <li><span>Lockout</span><b>{bc.lockout.active ? bc.lockout.reason ?? "active" : "none"}</b></li>
        </ul>
        {bc.lockout.active && (
          <div style={{ marginTop: 12 }}>
            <button
              className="tab"
              type="button"
              onClick={() => {
                void api.clearLockout().then(() => window.location.reload());
              }}
            >
              Clear lockout
            </button>
          </div>
        )}
        {bc.last_action?.blockers && bc.last_action.blockers.length > 0 && (
          <p className="reason">Blockers: {bc.last_action.blockers.join(", ")}</p>
        )}
        {bc.last_action?.error_code && (
          <p className="muted">Last error: {bc.last_action.error_code}</p>
        )}
        <p className="muted">
          Physical verification is required for success; an HTTP 200 alone is never treated as actuated.
          Heartbeat age {bc.heartbeat_age_seconds == null ? "n/a" : `${Math.round(bc.heartbeat_age_seconds)}s`}
          {" "}/ expiry {bc.heartbeat_expiry_seconds}s.
        </p>
      </div>
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
            <li><span>Grid import (live inverter)</span><b>{fmt(t?.grid_import_kw, " kW")}</b></li>
            <li><span>Grid export (live inverter)</span><b>{fmt(t?.grid_export_kw, " kW")}</b></li>
            <li><span>EMS mode</span><b>{t?.ems_mode ?? "—"}</b></li>
          </ul>
        </section>

        <section className="panel">
          <h2>Mercedes charging</h2>
          {ev?.fault && <div className="badge badge-block" style={{ marginBottom: 10 }}>Shelly protection fault</div>}
          {ev?.stale && <div className="badge badge-warn" style={{ marginBottom: 10 }}>vehicle telemetry stale</div>}
          <ul className="metrics">
            <li><span>Car SoC</span><b>{fmt(ev?.soc_pct, " %", 0)}</b></li>
            <li><span>Connected</span><b>{ev ? (ev.plugged_in ? "yes" : "no") : "—"}</b></li>
            <li><span>Charging</span><b>{ev?.charging_active ? "active" : "idle"}</b></li>
            <li><span>Garage relay</span><b>{ev?.switch_on == null ? "unknown" : ev.switch_on ? "on" : "off"}</b></li>
            <li><span>Charge power</span><b>{fmt(ev?.power_kw, " kW")}</b></li>
            <li><span>Control</span><b>{evControl.enabled ? "automatic" : "disabled"}</b></li>
            <li><span>Targets</span><b>{evControl.minimum_target_soc_pct.toFixed(0)}% by {String(evControl.departure_hour).padStart(2, "0")}:00 / {evControl.target_soc_pct.toFixed(0)}%</b></li>
          </ul>
          <p className="reason">{evControl.reason}</p>
          <p className="muted">
            <b>Relay confirmation:</b> after each command PvOpti waits {evControl.relay_settle_seconds}s,
            then checks every {evControl.relay_verify_interval_seconds}s for up to {evControl.relay_verify_timeout_seconds}s.
            This grace period is intentional because the garage Shelly has weak coverage. An unconfirmed activation
            is forced off and retried no sooner than {evControl.relay_failure_backoff_minutes} minutes later.
          </p>
          <p className="muted">
            <b>Opportunistic policy:</b> {evControl.policy_explanation} Only {Math.round(evControl.forecast_surplus_factor * 100)}%
            of predicted same-day surplus is counted; stationary-battery reserve is {evControl.battery_reserve_pct.toFixed(0)}% and its later-fill target is {evControl.battery_full_target_pct.toFixed(0)}%.
          </p>
        </section>

        <section className="panel">
          <h2>Pstryk billing meter</h2>
          <ul className="metrics">
            <li><span>Latest settled hour</span><b>{meter ? new Date(meter.interval_start).toLocaleString() : "—"}</b></li>
            <li><span>Imported</span><b>{fmt(meter?.import_kwh, " kWh", 3)}</b></li>
            <li><span>Exported</span><b>{fmt(meter?.export_kwh, " kWh", 3)}</b></li>
            <li><span>Net balance</span><b>{fmt(meter?.balance_kwh, " kWh", 3)}</b></li>
            <li><span>Source</span><b>{meter?.source ?? "waiting for settled data"}</b></li>
          </ul>
          <p className="muted">Billing, savings, backtests, and load calibration use only settled Pstryk meter data. Inverter grid readings are retained solely for live control.</p>
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
              {run.safety?.warnings?.map((warning) => (
                <p className="reason" key={warning}>{warning}</p>
              ))}
              <ul className="metrics">
                <li><span>Expected value</span><b>{fmt(run.objective_pln ? -run.objective_pln : null, " PLN")}</b></li>
                <li><span>Known prices</span><b>{fmt(run.known_price_hours, " h", 0)}</b></li>
                {loadDiagnostics && (
                  <>
                    <li><span>Matched load hours</span><b>{loadDiagnostics.matched_hours} / {loadDiagnostics.expected_completed_hours}</b></li>
                    <li><span>Deficient load buckets</span><b>{loadDiagnostics.deficient_buckets.length}</b></li>
                  </>
                )}
                <li title="Realised: Pstryk-settled cost minus optimiser cost over the same settled intervals">
                  <span>Saved today</span><b>{fmt(savings?.day.savings_pln, " PLN")}</b>
                </li>
                <li title="Realised: Pstryk-settled cost minus optimiser cost over the same settled intervals">
                  <span>Saved 7 days</span><b>{fmt(savings?.week.savings_pln, " PLN")}</b>
                </li>
              </ul>
              {loadDiagnostics && loadDiagnostics.deficient_buckets.length > 0 && (
                <p className="muted">
                  Load history gaps: {loadDiagnostics.deficient_buckets.map((bucket) =>
                    `${bucket.weekend ? "weekend" : "weekday"} ${String(bucket.local_hour).padStart(2, "0")}:00 (${bucket.distinct_dates}/${bucket.required_distinct_dates} dates)`
                  ).join(", ")}
                </p>
              )}
            </>
          ) : (
            <p>No optimiser run yet.</p>
          )}
        </section>
      </div>
    </>
  );
}
