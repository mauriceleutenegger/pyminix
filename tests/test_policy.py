import itertools
import random

import pytest

from minix import protocol as p
from minix.config import SafetyConfig, UnitConfig
from minix.device import MiniX
from minix.policy.averaging import RunningAverage
from minix.policy.banding import PowerBand, PowerBandTracker, power_band
from minix.policy.limits import AdjustmentKind, DacSetpoints, commit_setpoints
from minix.policy.ranging import RangeChecker, in_range
from minix.policy.sequencing import (
    ConfigureTemperature, PlanError, RequireMonx, SetEnable, Wait, WaitForSettle, WriteDac,
    peak_power_mw, plan_commit, plan_deenergize, plan_energize, plan_startup,
)
from minix.sim import SimTransport

UNIT_10W = UnitConfig("01300036", 10.0)       # 50 kV, NSI
UNIT_4W = UnitConfig("01300036", 4.0)
UNIT_40KV = UnitConfig("01000000", 4.0)       # 40 kV, NSI
UNIT_OLD = UnitConfig("00001234", 4.0)        # 40 kV, non-NSI
UNITS = [UNIT_10W, UNIT_4W, UNIT_40KV, UNIT_OLD]


def kinds(commitment):
    return [a.kind for a in commitment.adjustments]


# --- limits ------------------------------------------------------------------

def test_plain_request():
    c = commit_setpoints(15.0, 10.0, UNIT_10W)
    assert (c.setpoints.hv_counts, c.setpoints.current_counts) == (1200, 200)
    assert (c.kv, c.ua) == (pytest.approx(15.0), pytest.approx(10.0))
    assert c.adjustments == ()
    assert c.power_mw == pytest.approx(150.0)


@pytest.mark.parametrize("kv, ua, expected", [
    (5.0, 10.0, [AdjustmentKind.HV_RAISED_TO_MIN]),
    (-3.0, 10.0, [AdjustmentKind.HV_RAISED_TO_MIN]),
    (55.0, 10.0, [AdjustmentKind.HV_LOWERED_TO_MAX]),
    (20.0, 1.0, [AdjustmentKind.CURRENT_RAISED_TO_MIN]),
    (20.0, 300.0, [AdjustmentKind.CURRENT_LOWERED_TO_MAX]),
    (5.0, 300.0, [AdjustmentKind.HV_RAISED_TO_MIN, AdjustmentKind.CURRENT_LOWERED_TO_MAX]),
    (10.0, 5.0, []),
    (50.0, 5.0, []),
])
def test_clamping(kv, ua, expected):
    c = commit_setpoints(kv, ua, UNIT_10W)
    assert kinds(c) == expected
    assert 10.0 <= c.kv <= 50.0 and 5.0 <= c.ua <= 200.0
    assert (c.requested_kv, c.requested_ua) == (kv, ua)


def test_40kv_board_clamps_at_40():
    c = commit_setpoints(45.0, 10.0, UNIT_40KV)
    assert c.kv == pytest.approx(40.0)
    assert c.setpoints.hv_counts == 4000


def test_full_corner_on_10w_unit():
    c = commit_setpoints(50.0, 200.0, UNIT_10W)
    assert kinds(c) == [AdjustmentKind.CURRENT_REDUCED_FOR_POWER]
    assert c.kv == pytest.approx(50.0)
    assert c.setpoints.current_counts == 3979          # 198.95 µA
    assert c.power_mw == pytest.approx(9947.5)


def test_rounding_hole_is_closed():
    # The reference could commit 21 kV x 193 µA = 4053 mW here (§9.1).
    c = commit_setpoints(20.51, 200.0, UNIT_4W)
    assert c.power_mw < UNIT_4W.safe_mw
    assert c.kv <= 20.51


def test_power_reduction_message():
    c = commit_setpoints(40.0, 200.0, UNIT_4W)
    (adj,) = c.adjustments
    assert adj.requested == 200.0
    assert adj.committed == pytest.approx(98.70)     # 98.75 µA would be exactly 3950 mW
    assert adj.message == "current reduced to stay under the power limit: 200 → 98.7 µA"


@pytest.mark.parametrize("unit", UNITS, ids=lambda u: u.describe())
def test_commit_properties(unit):
    rng = random.Random(1)
    requests = [(rng.uniform(-5, 60), rng.uniform(-5, 250)) for _ in range(3000)]
    requests += [(unit.hv_max_kv, unit.current_max_ua), (unit.hv_min_kv, unit.current_min_ua)]
    requests += [(kv / 100, 200.0) for kv in range(1000, 5001, 7)]
    for kv, ua in requests:
        c = commit_setpoints(kv, ua, unit)
        sp = c.setpoints
        assert c.power_mw < unit.safe_mw
        assert unit.hv_min_kv <= c.kv <= unit.hv_max_kv
        assert unit.current_min_ua <= c.ua <= unit.current_max_ua
        # never above the (clamped) request
        assert c.kv <= max(kv, unit.hv_min_kv) + 1e-9
        assert c.ua <= max(ua, unit.current_min_ua) + 1e-9
        if AdjustmentKind.CURRENT_REDUCED_FOR_POWER in kinds(c):
            one_more = DacSetpoints(sp.hv_counts, sp.current_counts + 1, sp.hv_factor)
            assert one_more.power_mw >= unit.safe_mw
        else:
            # truncation costs less than one count
            assert c.ua > min(max(ua, unit.current_min_ua), unit.current_max_ua) - 0.05


@pytest.mark.parametrize("kv, ua", [(float("nan"), 10), (15, float("inf")), (float("-inf"), 5)])
def test_non_finite_requests_are_refused(kv, ua):
    with pytest.raises(ValueError, match="finite"):
        commit_setpoints(kv, ua, UNIT_10W)


def test_limit_follows_the_rating():
    assert commit_setpoints(40, 200, UNIT_4W).power_mw < 3950
    assert commit_setpoints(40, 200, UNIT_10W).power_mw == pytest.approx(8000)


# --- sequencing --------------------------------------------------------------

def setpoints(kv, ua, unit=UNIT_10W):
    return commit_setpoints(kv, ua, unit).setpoints


def dac_writes(steps):
    return [(s.channel, s.counts) for s in steps if isinstance(s, WriteDac)]


def test_nsi_raise_that_would_overshoot_goes_current_first():
    prev, new = setpoints(20, 150, UNIT_4W), setpoints(35, 100, UNIT_4W)   # 35 x 150 > limit
    steps = plan_commit(prev, new, UNIT_4W)
    assert dac_writes(steps) == [(p.DAC_CURRENT, new.current_counts), (p.DAC_HV, new.hv_counts)]
    assert steps[0] == Wait(0.25)
    assert steps[2] == WaitForSettle(p.ADC_CURRENT, new.ua)
    assert steps[-1] == Wait(0.5)


def test_nsi_safe_raise_goes_voltage_first():
    prev, new = setpoints(20, 50), setpoints(30, 60)
    steps = plan_commit(prev, new, UNIT_10W)
    assert steps == [
        WriteDac(p.DAC_HV, new.hv_counts), WaitForSettle(p.ADC_HV, new.kv),
        Wait(0.25), WriteDac(p.DAC_CURRENT, new.current_counts),
        WaitForSettle(p.ADC_CURRENT, new.ua), Wait(0.5),
    ]


def test_nsi_lowering_goes_voltage_first():
    prev, new = setpoints(40, 100), setpoints(20, 190)
    assert dac_writes(plan_commit(prev, new, UNIT_10W))[0] == (p.DAC_HV, new.hv_counts)


def test_settle_waits_use_reference_timing():
    step = plan_commit(setpoints(20, 50), setpoints(30, 60), UNIT_10W)[1]
    assert (step.tolerance, step.interval_s, step.attempts) == (0.10, 0.5, 15)


@pytest.mark.parametrize("prev_kv, new_kv, first", [(20, 30, p.DAC_CURRENT), (30, 20, p.DAC_HV)])
def test_non_nsi_order_and_delays(prev_kv, new_kv, first):
    prev, new = setpoints(prev_kv, 50, UNIT_OLD), setpoints(new_kv, 50, UNIT_OLD)
    steps = plan_commit(prev, new, UNIT_OLD)
    assert dac_writes(steps)[0][0] == first
    assert [type(s) for s in steps] == [WriteDac, Wait, WriteDac, Wait]
    assert all(s.seconds == 1.0 for s in steps if isinstance(s, Wait))


@pytest.mark.parametrize("unit", UNITS, ids=lambda u: u.describe())
def test_no_plan_passes_through_the_limit(unit):
    rng = random.Random(2)
    states = [DacSetpoints.zero(unit)]
    states += [commit_setpoints(rng.uniform(10, 50), rng.uniform(5, 200), unit).setpoints
               for _ in range(150)]
    states += [commit_setpoints(kv, 200, unit).setpoints for kv in (10, 20, 30, 40, 50)]
    for prev, new in itertools.product(states, states[1:]):
        steps = plan_commit(prev, new, unit)          # raises PlanError if unsafe
        assert peak_power_mw(prev, steps) < unit.safe_mw
        assert set(dac_writes(steps)) == {
            (p.DAC_HV, new.hv_counts), (p.DAC_CURRENT, new.current_counts)}


def test_unsafe_target_is_refused():
    unsafe = DacSetpoints(4000, 4000, UNIT_4W.hv_factor)     # 50 kV x 200 µA
    with pytest.raises(PlanError):
        plan_commit(DacSetpoints.zero(UNIT_4W), unsafe, UNIT_4W)


def test_energize_plan():
    target = setpoints(15, 10)
    steps = plan_energize(target, UNIT_10W, monx_timeout_s=1.0)
    assert steps[:3] == [WriteDac(p.DAC_HV, 0), WriteDac(p.DAC_CURRENT, 0), SetEnable(True)]
    assert steps[3:-1] == plan_commit(DacSetpoints.zero(UNIT_10W), target, UNIT_10W)
    assert steps[-1] == RequireMonx(1.0)


def test_deenergize_plan_nsi():
    assert plan_deenergize(UNIT_10W) == [
        WriteDac(p.DAC_HV, 0), Wait(0.2), WriteDac(p.DAC_CURRENT, 0), Wait(0.1),
        SetEnable(False), Wait(0.5),
    ]


def test_deenergize_plan_non_nsi():
    assert plan_deenergize(UNIT_OLD) == [
        WriteDac(p.DAC_HV, 0), WriteDac(p.DAC_CURRENT, 0), SetEnable(False)]


def test_startup_plan():
    assert plan_startup(UNIT_10W) == [
        ConfigureTemperature(), WriteDac(p.DAC_HV, 0), Wait(0.2), WriteDac(p.DAC_CURRENT, 0)]
    assert Wait(0.2) not in plan_startup(UNIT_OLD)


def run_on_sim(steps, dev, sim):
    """Apply a plan's device actions, ignoring timing. Test harness only."""
    for step in steps:
        if isinstance(step, WriteDac):
            dev.write_dac(step.channel, step.counts)
        elif isinstance(step, SetEnable):
            dev.set_hv_enable(step.on)
        elif isinstance(step, ConfigureTemperature):
            dev.configure_temperature_sensor()


def test_plans_on_the_simulator_stay_under_the_limit():
    unit = UNIT_4W
    sim = SimTransport()
    dev = MiniX(sim)
    dev.initialize()
    run_on_sim(plan_startup(unit), dev, sim)
    rng = random.Random(3)
    current = setpoints(15, 10, unit)
    run_on_sim(plan_energize(current, unit, 1.0), dev, sim)
    for _ in range(300):
        target = commit_setpoints(rng.uniform(5, 55), rng.uniform(0, 250), unit).setpoints
        run_on_sim(plan_commit(current, target, unit), dev, sim)
        assert (sim.hv_dac, sim.current_dac) == (target.hv_counts, target.current_counts)
        current = target
    run_on_sim(plan_deenergize(unit), dev, sim)
    assert 0 < sim.max_commanded_mw < unit.safe_mw
    assert not sim.hv_enabled and (sim.hv_dac, sim.current_dac) == (0, 0)
    assert sim.violations == []


def test_naive_ordering_would_fail_the_same_check():
    # Voltage first on a raise that would overshoot: the check must catch it.
    unit = UNIT_4W
    prev, new = setpoints(20, 150, unit), setpoints(35, 100, unit)
    naive = [WriteDac(p.DAC_HV, new.hv_counts), WriteDac(p.DAC_CURRENT, new.current_counts)]
    assert peak_power_mw(prev, naive) >= unit.safe_mw


# --- ranging -----------------------------------------------------------------

@pytest.mark.parametrize("expected, measured, ok", [
    (15.0, 15.0, True),
    (15.0, 17.49, True), (15.0, 17.5, False),     # 15 * 1.1 + 1 = 17.5, strict
    (15.0, 12.51, True), (15.0, 12.5, False),
    (10.0, 10.4, True),                           # §10.3: 10 µA read as 10.4
    (5.0, 6.49, True), (5.0, 6.5, False),
    (1.1, 0.0, True), (1.1, -0.02, False), (1.1, 2.2, True), (1.1, 2.22, False),
])
def test_in_range(expected, measured, ok):
    assert in_range(expected, measured) is ok


def test_in_range_fine_tolerance():
    assert in_range(100, 105.9, 0.05) and not in_range(100, 106.1, 0.05)


def checker(unit=UNIT_10W, **safety):
    return RangeChecker(unit, SafetyConfig(**safety))


def test_hv_on_compares_with_setpoints():
    rc = checker()
    target = setpoints(15, 10)
    status = rc.check(0.0, hv_on=True, expected=target, kv=15.1, ua=10.4)
    assert status.testing and status.ok
    status = rc.check(1.0, hv_on=True, expected=target, kv=15.1, ua=13.0)
    assert status.testing and status.hv_ok and not status.current_ok and not status.ok


def test_hv_on_requires_setpoints():
    with pytest.raises(ValueError):
        checker().check(0.0, hv_on=True, expected=None, kv=0, ua=0)


def test_hv_off_waits_then_expects_near_zero():
    rc = checker()
    rc.hv_switched(100.0)
    assert not rc.check(106.9, hv_on=False, expected=None, kv=30, ua=50).testing
    status = rc.check(107.0, hv_on=False, expected=None, kv=0.2, ua=0.4)
    assert status.testing and status.ok
    status = rc.check(108.0, hv_on=False, expected=None, kv=3.0, ua=0.4)
    assert not status.hv_ok


def test_delay_also_applies_after_startup():
    rc = checker()
    assert not rc.check(50.0, hv_on=False, expected=None, kv=30, ua=0).testing
    assert rc.check(57.0, hv_on=False, expected=None, kv=30, ua=0).testing


def test_delay_is_configurable():
    rc = checker(hv_off_test_delay_s=2.0)
    rc.hv_switched(0.0)
    assert rc.check(2.0, hv_on=False, expected=None, kv=0, ua=0).testing


@pytest.mark.parametrize("unit, kv, ua, ok", [
    (UNIT_4W, 40, 101.2, True),       # 4048 mW < 4050
    (UNIT_4W, 40, 101.3, False),
    (UNIT_10W, 50, 200.9, True),      # 10045 mW < 10050
    (UNIT_10W, 50, 201.0, False),
])
def test_power_limit(unit, kv, ua, ok):
    target = commit_setpoints(kv, ua, unit).setpoints
    status = checker(unit).check(0.0, hv_on=True, expected=target, kv=kv, ua=ua)
    assert status.power_ok is ok


# --- averaging ---------------------------------------------------------------

def test_first_value_fills_the_window():
    avg = RunningAverage()
    assert avg.value is None
    assert avg.add(15.0) == 15.0
    assert avg.add(22.0) == pytest.approx(16.0)       # (6 * 15 + 22) / 7


def test_window_rolls():
    avg = RunningAverage(3)
    for v in (1.0, 2.0, 3.0, 4.0):
        avg.add(v)
    assert avg.value == pytest.approx(3.0)


def test_reset_primes_again():
    avg = RunningAverage()
    avg.add(0.0)
    avg.reset()
    assert avg.value is None
    assert avg.add(30.0) == 30.0


def test_window_size_must_be_positive():
    with pytest.raises(ValueError):
        RunningAverage(0)


# --- banding -----------------------------------------------------------------

@pytest.mark.parametrize("unit, mw, band", [
    (UNIT_4W, 0.0, PowerBand.IDLE),
    (UNIT_4W, 9.99, PowerBand.IDLE),
    (UNIT_4W, 10.0, PowerBand.NORMAL),
    (UNIT_4W, 3899.9, PowerBand.NORMAL),
    (UNIT_4W, 3900.0, PowerBand.CAUTION),
    (UNIT_4W, 3999.9, PowerBand.CAUTION),
    (UNIT_4W, 4000.0, PowerBand.DANGER),
    (UNIT_10W, 9899.9, PowerBand.NORMAL),
    (UNIT_10W, 9900.0, PowerBand.CAUTION),
    (UNIT_10W, 10000.0, PowerBand.DANGER),
    (UNIT_10W, float("inf"), PowerBand.DANGER),
    (UNIT_10W, -5.0, PowerBand.IDLE),
])
def test_power_band(unit, mw, band):
    assert power_band(mw, unit) is band


def test_maximal_setpoint_shows_caution():
    for unit in (UNIT_4W, UNIT_10W):
        c = commit_setpoints(unit.hv_max_kv, unit.current_max_ua, unit)
        assert power_band(c.power_mw, unit) is PowerBand.CAUTION


def test_tracker_goes_up_at_once():
    tracker = PowerBandTracker(UNIT_10W)
    assert tracker.band is None
    assert tracker.update(5000) is PowerBand.NORMAL
    assert tracker.update(9900) is PowerBand.CAUTION
    assert tracker.update(10000) is PowerBand.DANGER


@pytest.mark.parametrize("power, band", [
    (9990, PowerBand.DANGER), (9901, PowerBand.DANGER),     # within 100 mW of 10 W
    (9899, PowerBand.CAUTION), (9850, PowerBand.CAUTION),   # 9900 - 100 < power
    (9799, PowerBand.NORMAL), (3000, PowerBand.NORMAL),
    (4, PowerBand.IDLE), (0, PowerBand.IDLE),
])
def test_tracker_comes_down_with_hysteresis(power, band):
    tracker = PowerBandTracker(UNIT_10W)
    tracker.update(10050)
    assert tracker.update(power) is band


def test_tracker_holds_caution_near_the_threshold():
    tracker = PowerBandTracker(UNIT_10W)
    rng = random.Random(4)
    tracker.update(9900)
    bands = {tracker.update(rng.gauss(9892, 19)) for _ in range(5000)}
    assert bands == {PowerBand.CAUTION}


def test_tracker_idle_hysteresis_is_small():
    tracker = PowerBandTracker(UNIT_4W)
    tracker.update(12)
    assert tracker.update(6) is PowerBand.NORMAL
    assert tracker.update(4) is PowerBand.IDLE


def test_tracker_reset():
    tracker = PowerBandTracker(UNIT_4W)
    tracker.update(4000)
    tracker.reset()
    assert tracker.band is None
    assert tracker.update(100) is PowerBand.NORMAL


def test_nan_power_is_an_error():
    with pytest.raises(ValueError):
        power_band(float("nan"), UNIT_4W)
