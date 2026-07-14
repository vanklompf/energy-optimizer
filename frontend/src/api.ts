export interface Telemetry {
  ts: string;
  soc_pct: number | null;
  batt_charge_kw: number | null;
  batt_discharge_kw: number | null;
  pv_kw: number | null;
  load_kw: number | null;
  grid_import_kw: number | null;
  grid_export_kw: number | null;
  ems_mode: string | null;
  stale: boolean;
}

export interface PriceNow {
  interval_start: string;
  buy_gross: number | null;
  sell_gross: number | null;
  full_price: number | null;
  is_cheap: boolean | null;
  is_expensive: boolean | null;
  source: string;
}

export interface RunSummary {
  run_id: string;
  ts: string;
  mode: string;
  status: string;
  reason: string | null;
  objective_pln: number | null;
  horizon_hours: number;
  known_price_hours: number;
  solver_input_sha256: string | null;
  solve_ms: number | null;
}

export interface StatusResponse {
  mode: string;
  control_enabled: boolean;
  now: string;
  telemetry: Telemetry | null;
  current_price: PriceNow | null;
  last_run: RunSummary | null;
}

export interface PricePoint {
  interval_start: string;
  buy_gross: number | null;
  sell_gross: number | null;
  full_price: number | null;
  is_cheap: boolean | null;
  is_expensive: boolean | null;
  source: string;
}

export interface PricesResponse {
  now: string;
  current_hour: string | null;
  prices: PricePoint[];
}

export interface PlanStep {
  interval_start: string;
  dt_hours: number;
  pv_to_load_kwh: number;
  pv_to_battery_kwh: number;
  pv_to_grid_kwh: number;
  grid_to_load_kwh: number;
  grid_to_battery_kwh: number;
  battery_to_load_kwh: number;
  battery_to_grid_kwh: number;
  curtail_kwh: number;
  soc_pct_end: number;
  marginal_value: number | null;
}

export interface PlanResponse {
  run: RunSummary | null;
  steps: PlanStep[];
}

export interface PolicyResult {
  policy: string;
  net_cost_pln: number;
  import_kwh: number;
  export_kwh: number;
  battery_throughput_kwh: number;
}

export interface BacktestResponse {
  start: string;
  end: string;
  intervals: number;
  results: PolicyResult[];
}

export interface SavingsWindow {
  actual_cost_pln: number | null;
  optimiser_cost_pln: number | null;
  savings_pln: number | null;
  intervals: number;
}

export interface SavingsResponse {
  now: string;
  day: SavingsWindow;
  week: SavingsWindow;
}

export interface HourlyComparisonPoint {
  interval_start: string;
  buy_price: number;
  sell_price: number;
  actual_charge_kwh: number;
  actual_discharge_kwh: number;
  optimiser_charge_kwh: number | null;
  optimiser_discharge_kwh: number | null;
  actual_soc_pct: number | null;
  optimiser_soc_pct: number | null;
}

export interface HourlyComparisonResponse {
  now: string;
  tz: string;
  optimiser_status?: string;
  points: HourlyComparisonPoint[];
}

async function getJSON<T>(url: string): Promise<T> {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${url} -> ${resp.status}`);
  return resp.json() as Promise<T>;
}

export const api = {
  status: () => getJSON<StatusResponse>("/api/status"),
  plan: () => getJSON<PlanResponse>("/api/plan"),
  prices: (pastHours = 12, futureHours = 24) =>
    getJSON<PricesResponse>(`/api/prices?past_hours=${pastHours}&future_hours=${futureHours}`),
  savings: () => getJSON<SavingsResponse>("/api/savings"),
  hourlyComparison: (hours = 48) =>
    getJSON<HourlyComparisonResponse>(`/api/comparison/hourly?hours=${hours}`),
  dailyReports: () => getJSON<{ reports: Record<string, unknown>[] }>("/api/reports/daily"),
  backtest: async (body: {
    start: string;
    end: string;
    policies: string[];
  }): Promise<BacktestResponse> => {
    const resp = await fetch("/api/backtest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error(`backtest -> ${resp.status}`);
    return resp.json() as Promise<BacktestResponse>;
  },
};
