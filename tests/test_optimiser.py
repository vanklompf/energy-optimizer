from __future__ import annotations

import math

import pytest

from energy_optimizer.optimiser import IntervalInput, OptimiserParams, optimise


def make_params(**overrides) -> OptimiserParams:
    base = dict(
        battery_capacity_kwh=10.0,
        soc_min_kwh=2.0,
        battery_hard_min_kwh=0.0,
        soc_max_kwh=9.8,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        eta_charge=math.sqrt(0.9),
        eta_discharge=math.sqrt(0.9),
        site_import_limit_kw=14.0,
        site_export_limit_kw=14.0,
        inverter_limit_kw=None,
        degradation_cost_pln_per_kwh=0.05,
    )
    base.update(overrides)
    return OptimiserParams(**base)


def test_empty_intervals_returns_error() -> None:
    result = optimise([], 2.0, make_params())
    assert result.status == "error"


def test_arbitrage_charges_cheap_discharges_expensive() -> None:
    intervals = []
    for h in range(6):
        buy = 0.2 if h < 3 else 2.0
        intervals.append(
            IntervalInput(
                interval_start=f"2026-07-12T{h:02d}:00:00+00:00",
                dt_hours=1.0,
                pv_energy_kwh=0.0,
                load_energy_kwh=1.0,
                buy_price=buy,
                sell_price=buy * 0.5,
            )
        )
    result = optimise(intervals, 2.0, make_params())
    assert result.status == "optimal"
    # It should grid-charge during the cheap window and discharge during the expensive one.
    early_charge = sum(s.grid_to_battery_kwh for s in result.steps[:3])
    late_discharge = sum(s.battery_to_load_kwh for s in result.steps[3:])
    assert early_charge > 0.5
    assert late_discharge > 0.5


def test_no_simultaneous_charge_and_discharge() -> None:
    intervals = [
        IntervalInput(
            interval_start=f"2026-07-12T{h:02d}:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=2.0,
            load_energy_kwh=1.0,
            buy_price=1.0,
            sell_price=-0.1,  # negative sell price: must not burn energy
        )
        for h in range(4)
    ]
    result = optimise(intervals, 5.0, make_params())
    assert result.status == "optimal"
    for s in result.steps:
        charge = s.pv_to_battery_kwh + s.grid_to_battery_kwh
        discharge = s.battery_to_load_kwh + s.battery_to_grid_kwh
        assert not (charge > 1e-6 and discharge > 1e-6), "simultaneous charge/discharge"


def test_battery_export_flag_disables_export() -> None:
    intervals = [
        IntervalInput(
            interval_start=f"2026-07-12T{h:02d}:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=0.0,
            load_energy_kwh=0.0,
            buy_price=0.1,
            sell_price=5.0,  # very lucrative export
        )
        for h in range(3)
    ]
    result = optimise(intervals, 9.0, make_params(allow_battery_export=False))
    assert result.status == "optimal"
    assert all(s.battery_to_grid_kwh == 0.0 for s in result.steps)


def test_soc_respects_bounds() -> None:
    intervals = [
        IntervalInput(
            interval_start=f"2026-07-12T{h:02d}:00:00+00:00",
            dt_hours=0.25,
            pv_energy_kwh=0.0,
            load_energy_kwh=2.0,
            buy_price=1.0,
            sell_price=0.5,
        )
        for h in range(8)
    ]
    params = make_params()
    result = optimise(intervals, 9.0, params)
    assert result.status == "optimal"
    for s in result.steps:
        assert s.soc_kwh_end >= params.soc_min_kwh - 1e-6
        assert s.soc_kwh_end <= params.soc_max_kwh + 1e-6


def test_ev_charging_is_shifted_to_solar_surplus() -> None:
    intervals = [
        IntervalInput(
            interval_start=f"2026-07-12T{12 + h:02d}:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=0.0 if h == 0 else 4.0,
            load_energy_kwh=1.0,
            buy_price=1.0,
            sell_price=0.0,
            ev_available=True,
        )
        for h in range(2)
    ]
    params = make_params(
        ev_charge_power_kw=2.0,
        ev_target_slots=1,
    )

    result = optimise(intervals, 2.0, params)

    assert result.status == "optimal"
    assert result.steps[0].ev_charge_kwh == 0.0
    assert result.steps[1].ev_charge_kwh == 2.0
    assert result.steps[1].pv_to_load_kwh == 1.0
    assert result.steps[1].pv_to_ev_kwh == 2.0


def test_ev_minimum_charge_is_met_before_departure_deadline() -> None:
    intervals = [
        IntervalInput(
            interval_start=f"2026-07-13T0{h}:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=0.0,
            load_energy_kwh=0.0,
            buy_price=2.0 if h == 0 else 0.2,
            sell_price=0.0,
            ev_available=True,
            ev_required_soon=(h == 0),
        )
        for h in range(2)
    ]
    params = make_params(
        ev_charge_power_kw=2.0,
        ev_target_slots=1,
        ev_minimum_slots=1,
    )

    result = optimise(intervals, 5.0, params)

    assert result.status == "optimal"
    assert result.steps[0].ev_charge_kwh == 2.0
    assert result.steps[1].ev_charge_kwh == 0.0


def test_opportunistic_ev_starts_early_from_battery_without_incremental_grid() -> None:
    intervals = [
        IntervalInput(
            interval_start=f"2026-08-03T{10 + h:02d}:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=0.0 if h == 0 else 4.0,
            load_energy_kwh=0.5,
            buy_price=0.0,
            sell_price=0.0,
            ev_available=True,
        )
        for h in range(2)
    ]
    params = make_params(
        ev_charge_power_kw=2.0,
        ev_target_slots=1,
        ev_minimum_slots=0,
    )

    result = optimise(intervals, 5.0, params)

    assert result.status == "optimal"
    assert result.steps[0].ev_charge_kwh == 2.0
    assert result.steps[0].grid_import_kwh <= 1e-6
    assert result.steps[0].battery_to_load_kwh > 0


def test_guaranteed_ev_minimum_may_use_grid() -> None:
    interval = IntervalInput(
        interval_start="2026-08-03T06:00:00+00:00",
        dt_hours=1.0,
        pv_energy_kwh=0.0,
        load_energy_kwh=0.5,
        buy_price=1.0,
        sell_price=0.0,
        ev_available=True,
        ev_required_soon=True,
    )
    params = make_params(
        soc_min_kwh=2.0,
        battery_hard_min_kwh=2.0,
        ev_charge_power_kw=2.0,
        ev_target_slots=1,
        ev_minimum_slots=1,
    )

    result = optimise([interval], 2.0, params)

    assert result.status == "optimal"
    assert result.steps[0].ev_charge_kwh == 2.0
    assert result.steps[0].grid_to_load_kwh == 0.5
    assert result.steps[0].grid_to_ev_kwh == 2.0


def test_guaranteed_ev_may_use_energy_below_the_stationary_battery_reserve() -> None:
    interval = IntervalInput(
        interval_start="2026-08-03T06:00:00+00:00",
        dt_hours=1.0,
        pv_energy_kwh=0.0,
        load_energy_kwh=0.0,
        buy_price=1.0,
        sell_price=0.0,
        ev_available=True,
        ev_required_soon=True,
    )
    params = make_params(
        soc_min_kwh=2.0,
        battery_hard_min_kwh=0.0,
        site_import_limit_kw=0.0,
        ev_charge_power_kw=1.0,
        ev_target_slots=1,
        ev_minimum_slots=1,
    )

    result = optimise([interval], 1.5, params)

    assert result.status == "optimal"
    assert result.steps[0].battery_to_ev_kwh > 0.0
    assert result.steps[0].soc_kwh_end < params.soc_min_kwh
    assert result.steps[0].soc_kwh_end >= params.battery_hard_min_kwh


def test_starting_below_reserve_does_not_require_immediate_recovery() -> None:
    interval = IntervalInput(
        interval_start="2026-08-03T06:00:00+00:00",
        dt_hours=0.25,
        pv_energy_kwh=0.0,
        load_energy_kwh=0.1,
        buy_price=1.0,
        sell_price=10.0,
    )
    params = make_params(
        soc_min_kwh=2.0,
        battery_hard_min_kwh=0.0,
        site_import_limit_kw=1.0,
        inverter_limit_kw=1.0,
    )

    result = optimise([interval], 1.5, params)

    assert result.status == "optimal"
    assert result.steps[0].soc_kwh_end == pytest.approx(1.5)
    assert result.steps[0].grid_to_load_kwh == pytest.approx(0.1)
    assert result.steps[0].battery_to_load_kwh == pytest.approx(0.0)
    assert result.steps[0].battery_to_grid_kwh == pytest.approx(0.0)


def test_economic_export_cannot_spend_reserve_credited_to_the_ev() -> None:
    intervals = [
        IntervalInput(
            interval_start="2026-08-03T06:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=0.0,
            load_energy_kwh=0.0,
            buy_price=1.0,
            sell_price=0.0,
            ev_available=True,
            ev_required_soon=True,
        ),
        IntervalInput(
            interval_start="2026-08-03T07:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=0.0,
            load_energy_kwh=0.0,
            buy_price=1.0,
            sell_price=10.0,
        ),
    ]
    params = make_params(
        soc_min_kwh=2.0,
        battery_hard_min_kwh=0.0,
        site_import_limit_kw=0.0,
        ev_charge_power_kw=0.5,
        ev_target_slots=1,
        ev_minimum_slots=1,
    )

    result = optimise(intervals, 2.0, params)

    assert result.status == "optimal"
    assert result.steps[0].battery_to_ev_kwh > 0.0
    assert result.steps[0].soc_kwh_end < params.soc_min_kwh
    assert result.steps[1].battery_to_grid_kwh == pytest.approx(0.0)


def test_recharging_after_guaranteed_ev_use_does_not_reopen_reserve_for_export() -> None:
    intervals = [
        IntervalInput(
            interval_start="2026-08-03T06:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=0.0,
            load_energy_kwh=0.0,
            buy_price=1.0,
            sell_price=0.0,
            ev_available=True,
            ev_required_soon=True,
        ),
        IntervalInput(
            interval_start="2026-08-03T07:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=1.0,
            load_energy_kwh=0.0,
            buy_price=1.0,
            sell_price=0.0,
        ),
        IntervalInput(
            interval_start="2026-08-03T08:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=0.0,
            load_energy_kwh=0.0,
            buy_price=1.0,
            sell_price=10.0,
        ),
    ]
    params = make_params(
        soc_min_kwh=2.0,
        battery_hard_min_kwh=0.0,
        site_import_limit_kw=0.0,
        ev_charge_power_kw=math.sqrt(0.9),
        ev_target_slots=1,
        ev_minimum_slots=1,
        terminal_soc_salvage_pln_kwh=1.0,
    )

    result = optimise(intervals, 2.0, params)

    assert result.status == "optimal"
    assert result.steps[0].soc_kwh_end == pytest.approx(1.0)
    assert result.steps[1].soc_kwh_end == pytest.approx(1.0 + math.sqrt(0.9))
    assert result.steps[2].battery_to_grid_kwh == pytest.approx(0.0)


def test_opportunistic_ev_cannot_consume_the_departure_reserve() -> None:
    interval = IntervalInput(
        interval_start="2026-08-03T10:00:00+00:00",
        dt_hours=1.0,
        pv_energy_kwh=0.0,
        load_energy_kwh=0.0,
        buy_price=1.0,
        sell_price=0.0,
        ev_available=True,
    )
    params = make_params(
        soc_min_kwh=2.0,
        battery_hard_min_kwh=0.0,
        site_import_limit_kw=0.0,
        ev_charge_power_kw=0.5,
        ev_target_slots=1,
        ev_minimum_slots=0,
    )

    result = optimise([interval], 2.0, params)

    assert result.status == "infeasible"


def test_negative_buy_may_charge_within_limits() -> None:
    intervals = [
        IntervalInput(
            interval_start="2026-08-08T00:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=0.0,
            load_energy_kwh=0.0,
            buy_price=-0.2,
            sell_price=0.0,
            price_is_real=True,
        ),
        IntervalInput(
            interval_start="2026-08-08T01:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=0.0,
            load_energy_kwh=3.0,
            buy_price=2.0,
            sell_price=0.5,
            price_is_real=True,
        ),
    ]
    params = make_params(
        max_charge_kw=2.0,
        site_import_limit_kw=2.0,
        soc_max_kwh=5.0,
        allow_grid_charging=True,
    )
    result = optimise(intervals, 2.0, params)
    assert result.status == "optimal"
    assert result.steps[0].grid_to_battery_kwh > 0.1
    assert result.steps[0].grid_import_kwh <= 2.0 + 1e-6
    assert result.steps[0].soc_kwh_end <= 5.0 + 1e-6
    assert "grid_charge_arbitrage" in result.steps[0].reason_codes


def test_export_requires_permission_and_preserves_reserve() -> None:
    intervals = [
        IntervalInput(
            interval_start="2026-08-08T10:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=0.0,
            load_energy_kwh=0.0,
            buy_price=0.1,
            sell_price=5.0,
            price_is_real=True,
        )
    ]
    denied = optimise(intervals, 9.0, make_params(allow_battery_export=False, soc_min_kwh=2.0))
    assert denied.status == "optimal"
    assert denied.steps[0].battery_to_grid_kwh == 0.0

    allowed = optimise(intervals, 9.0, make_params(allow_battery_export=True, soc_min_kwh=2.0))
    assert allowed.status == "optimal"
    assert allowed.steps[0].battery_to_grid_kwh > 0.1
    assert allowed.steps[0].soc_kwh_end >= 2.0 - 1e-6
    assert "battery_export_arbitrage" in allowed.steps[0].reason_codes


def test_low_value_spread_below_margin_produces_no_arbitrage() -> None:
    # Spread is real but smaller than degradation + activation margin.
    intervals = [
        IntervalInput(
            interval_start="2026-08-08T00:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=0.0,
            load_energy_kwh=0.0,
            buy_price=0.50,
            sell_price=0.20,
            price_is_real=True,
        ),
        IntervalInput(
            interval_start="2026-08-08T01:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=0.0,
            load_energy_kwh=2.0,
            buy_price=0.62,  # only +0.12 vs buy; degradation 0.05 + margin 0.10 = 0.15
            sell_price=0.20,
            price_is_real=True,
        ),
    ]
    params = make_params(
        degradation_cost_pln_per_kwh=0.05,
        activation_margin_pln_kwh=0.10,
        grid_charge_margin_pln_kwh=0.0,
        allow_grid_charging=True,
        allow_battery_export=False,
    )
    result = optimise(intervals, 2.0, params)
    assert result.status == "optimal"
    assert result.steps[0].grid_to_battery_kwh == pytest.approx(0.0, abs=1e-6)


def test_no_simultaneous_import_and_export() -> None:
    intervals = [
        IntervalInput(
            interval_start=f"2026-08-08T{h:02d}:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=3.0 if h % 2 == 0 else 0.0,
            load_energy_kwh=1.0,
            buy_price=-0.5 if h % 2 else 2.0,
            sell_price=2.0 if h % 2 == 0 else -0.1,
            price_is_real=True,
        )
        for h in range(4)
    ]
    result = optimise(
        intervals,
        5.0,
        make_params(allow_grid_charging=True, allow_battery_export=True),
    )
    assert result.status == "optimal"
    for step in result.steps:
        assert not (step.grid_import_kwh > 1e-6 and step.grid_export_kwh > 1e-6)


def test_padded_future_prices_cannot_authorize_current_grid_charge() -> None:
    intervals = [
        IntervalInput(
            interval_start="2026-08-08T00:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=0.0,
            load_energy_kwh=0.0,
            buy_price=0.1,
            sell_price=0.05,
            price_is_real=True,
        ),
        IntervalInput(
            interval_start="2026-08-08T01:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=0.0,
            load_energy_kwh=5.0,
            buy_price=10.0,
            sell_price=0.05,
            price_is_real=False,  # padded — must not alone justify charging now
        ),
    ]
    result = optimise(intervals, 2.0, make_params(allow_grid_charging=True))
    assert result.status == "optimal"
    assert result.steps[0].grid_to_battery_kwh == pytest.approx(0.0, abs=1e-6)


def test_reason_codes_distinguish_self_consumption_and_ev() -> None:
    intervals = [
        IntervalInput(
            interval_start="2026-08-08T10:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=2.0,
            load_energy_kwh=1.0,
            buy_price=1.0,
            sell_price=0.2,
            price_is_real=True,
            ev_available=True,
            ev_required_soon=True,
        ),
        IntervalInput(
            interval_start="2026-08-08T11:00:00+00:00",
            dt_hours=1.0,
            pv_energy_kwh=0.0,
            load_energy_kwh=0.0,
            buy_price=1.0,
            sell_price=0.2,
            price_is_real=True,
        ),
    ]
    result = optimise(
        intervals,
        5.0,
        make_params(
            allow_battery_export=False,
            ev_charge_power_kw=1.0,
            ev_target_slots=1,
            ev_minimum_slots=1,
        ),
    )
    assert result.status == "optimal"
    assert "ev_guaranteed" in result.steps[0].reason_codes
    assert any("self_consumption" in s.reason_codes for s in result.steps)
