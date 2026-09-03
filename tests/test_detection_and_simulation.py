"""Detection quality, statistics, and the simulator contract the whole evaluation rests on."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pic.detection.detector import Detector
from pic.detection.statistics import (
    confidence_from_evidence,
    cusum,
    ewma,
    mad,
    median,
    robust_z,
    two_proportion_ztest,
)
from pic.schemas import Segment
from pic.simulation.generator import PaymentSimulator
from pic.simulation.scenarios import SCENARIOS, get_scenario
from pic.store import EventStore, wilson_lower_bound

START = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def warm():
    """45 minutes of healthy traffic, shared across tests (generation is the slow part)."""
    store = EventStore()
    sim = PaymentSimulator(store, start_time=START, seed=7)
    sim.warmup(45)
    return store, sim


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------


def test_robust_z_is_not_fooled_by_an_outlier_in_history():
    """MAD-based scoring is the reason a previous incident in the baseline cannot mask a new one."""
    history = [0.92, 0.91, 0.92, 0.93, 0.92, 0.40, 0.92, 0.91]
    assert robust_z(0.70, history) < -3.0


def test_robust_z_handles_a_perfectly_flat_baseline():
    """A flat series must not divide by zero and report infinite significance."""
    z = robust_z(0.92, [0.92] * 8)
    assert z == 0.0
    assert robust_z(0.80, [0.92] * 8) > -1000


def test_wilson_bound_discounts_thin_volume():
    """3-of-4 failures must not outrank 400-of-1000 — the guard against small-segment noise."""
    assert wilson_lower_bound(3, 4) < wilson_lower_bound(400, 1000)


def test_two_proportion_test_rejects_noise_and_detects_real_change():
    noise = two_proportion_ztest(74, 100, 78, 100)
    assert not noise.significant
    real = two_proportion_ztest(740, 1000, 870, 1000)
    assert real.significant and real.p_value < 0.01


def test_cusum_finds_a_sustained_shift_but_not_a_stable_series():
    stable = [0.92, 0.91, 0.93, 0.92, 0.91, 0.92, 0.93, 0.92]
    assert not cusum(stable).change_point_detected
    shifted = stable + [0.74, 0.73, 0.72, 0.74]
    assert cusum(shifted).change_point_detected


def test_confidence_rises_with_evidence():
    weak = confidence_from_evidence(z_score=-3.1, sample_size=45, absolute_drop=0.05, change_point=False)
    strong = confidence_from_evidence(z_score=-8.0, sample_size=900, absolute_drop=0.22, change_point=True)
    assert 0.0 < weak < strong <= 0.99


def test_ewma_tracks_recent_values_more_than_old_ones():
    assert ewma([0.9] * 10 + [0.5], 0.5) < median([0.9] * 10 + [0.5])
    assert mad([1, 1, 1, 1]) == 0.0


# --------------------------------------------------------------------------
# Simulator contract
# --------------------------------------------------------------------------


def test_baseline_traffic_matches_the_configured_success_rate(warm):
    store, sim = warm
    window = store.metric_window(START + timedelta(minutes=5), sim.now)
    assert 0.90 <= window.success_rate <= 0.94, window.success_rate
    assert window.total > 5000


def test_money_is_always_integer_paise(warm):
    store, _ = warm
    sample = store.events[:400]
    assert all(isinstance(e.amount_paise, int) and e.amount_paise > 0 for e in sample)


def test_control_plane_shift_is_exactly_invertible():
    """Rollback replays a recorded inverse, so the inverse has to restore the prior state exactly."""
    store = EventStore()
    sim = PaymentSimulator(store, start_time=START, seed=1)
    before = dict(sim.control.weights_for("upi"))

    detail = sim.control.shift_traffic("route_A", "route_B", 15, payment_method="upi")
    assert sim.control.weights_for("upi") != before

    sim.control.shift_traffic("route_B", "route_A", detail["effective_share_moved"] * 100, payment_method="upi")
    after = sim.control.weights_for("upi")
    assert all(abs(after[k] - before[k]) < 1e-9 for k in before), (before, after)


def test_shift_cannot_move_more_than_a_route_carries():
    store = EventStore()
    sim = PaymentSimulator(store, start_time=START, seed=1)
    detail = sim.control.shift_traffic("route_C", "route_A", 90, payment_method="upi")
    weights = sim.control.weights_for("upi")
    assert weights["route_C"] >= -1e-9
    assert detail["effective_share_moved"] <= 0.21


def test_agent_actions_actually_change_the_generated_traffic():
    """The claim that verification measures a real effect depends on this."""
    store = EventStore()
    sim = PaymentSimulator(store, start_time=START, seed=3)
    sim.warmup(30)
    sim.activate(get_scenario("SCN-UPI-PSP"))
    sim.advance_seconds(600)

    before_split = store.metric_window(sim.now - timedelta(seconds=300), sim.now, {"route_id": "route_B", "payment_method": "upi"})
    sim.control.shift_traffic("route_A", "route_B", 20, payment_method="upi")
    sim.advance_seconds(600)
    after_split = store.metric_window(sim.now - timedelta(seconds=300), sim.now, {"route_id": "route_B", "payment_method": "upi"})

    assert after_split.total > before_split.total, "shifting traffic must move real volume"


def test_every_scenario_has_an_attributable_root_cause():
    """A scenario whose cause is not in the catalogue could never be diagnosed correctly."""
    from pic.agents.root_cause import HYPOTHESIS_CATALOGUE

    for scenario in SCENARIOS.values():
        assert scenario.root_cause_id in HYPOTHESIS_CATALOGUE, scenario.scenario_id


def test_scenario_intensity_ramps_in_and_out():
    scenario = get_scenario("SCN-UPI-PSP")
    assert scenario.intensity(-10) == 0.0
    assert 0 < scenario.intensity(scenario.ramp_s / 2) < 1.0
    assert scenario.intensity(scenario.duration_s / 2) == 1.0
    assert scenario.intensity(scenario.duration_s + 1) == 0.0


def test_ground_truth_is_windowed_not_ramp_diluted():
    """Scoring an estimate against a ramp-diluted average would understate the agent's accuracy."""
    store = EventStore()
    sim = PaymentSimulator(store, start_time=START, seed=5)
    sim.warmup(30)
    sim.activate(get_scenario("SCN-UPI-PSP"))
    sim.advance_seconds(1200)

    full = sim.true_revenue_at_risk_per_hour("SCN-UPI-PSP")
    late = sim.true_revenue_at_risk_per_hour(
        "SCN-UPI-PSP", sim.now - timedelta(seconds=300), sim.now
    )
    assert late > full, "loss rate at full severity must exceed the ramp-diluted average"


# --------------------------------------------------------------------------
# Detector
# --------------------------------------------------------------------------


def test_no_incident_on_healthy_traffic(warm):
    """Fail-closed: healthy traffic must never open an incident."""
    store, sim = warm
    detector = Detector(store)
    assert detector.evaluate(sim.now) is None


def test_detects_a_psp_degradation_and_attributes_it_correctly():
    store = EventStore()
    sim = PaymentSimulator(store, start_time=START, seed=7)
    sim.warmup(45)
    detector = Detector(store)
    sim.activate(get_scenario("SCN-UPI-PSP"))

    signal = None
    for _ in range(20):
        sim.advance_seconds(30)
        signal = detector.evaluate(sim.now)
        if signal:
            break

    assert signal is not None, "a severe UPI PSP outage must be detected"
    assert signal.current_value < signal.baseline
    assert signal.confidence >= 0.6
    assert signal.estimated_revenue_at_risk_paise > 0

    implicated = {
        value
        for segment in signal.affected_segments
        for value in segment.segment.dimensions.values()
    }
    assert "psp_axis" in implicated or "route_A" in implicated, implicated


def test_traffic_mix_change_does_not_open_an_incident():
    """The false-positive control: success rate falls, nothing is broken, so nothing should fire."""
    store = EventStore()
    sim = PaymentSimulator(store, start_time=START, seed=7)
    sim.warmup(45)
    detector = Detector(store)
    sim.activate(get_scenario("SCN-TRAFFIC-MIX"))

    for _ in range(20):
        sim.advance_seconds(30)
        assert detector.evaluate(sim.now) is None


def test_baseline_freeze_stops_an_outage_becoming_the_new_normal():
    """Without this a long incident is absorbed into 'expected' and stops being reported."""
    store = EventStore()
    sim = PaymentSimulator(store, start_time=START, seed=7)
    sim.warmup(45)
    detector = Detector(store)
    sim.activate(get_scenario("SCN-UPI-PSP"))
    sim.advance_seconds(120)

    detector.mark_degraded_from(sim.now - timedelta(seconds=120))
    frozen = detector.baseline(sim.now).ewma_baseline

    sim.advance_seconds(2400)
    still_frozen = detector.baseline(sim.now).ewma_baseline
    assert abs(still_frozen - frozen) < 0.03, "baseline drifted despite the incident being open"
    assert still_frozen > 0.88, "baseline should still reflect healthy behaviour"


# --------------------------------------------------------------------------
# Aggregation
#
# `segment_stats`, `cross_segment_stats`, `union_metric_window` and `success_rate_series` are the
# query surface every tool and the detector read through, and they accumulate counters in a single
# pass rather than collecting each bucket's events and walking them again per statistic. That is
# worth roughly half the cost of a detection sweep and is invisible until it is wrong, so the
# arithmetic is pinned here against a deliberately naive count over the same events.
# --------------------------------------------------------------------------


def _naive(rows, predicate):
    """(total, successes, failures, failed amount) over the rows a predicate accepts."""
    matched = [e for e in rows if predicate(e)]
    successes = sum(1 for e in matched if e.status == "success")
    return (
        len(matched),
        successes,
        len(matched) - successes,
        sum(e.amount_paise for e in matched if e.status == "failed"),
    )


def test_segment_stats_counts_match_a_naive_count(warm):
    store, sim = warm
    end = sim.now
    start = end - timedelta(minutes=10)
    rows = store.slice(start, end)

    seen = 0
    for stat in store.segment_stats(start, end, "psp", min_volume=1):
        value = stat.segment.dimensions["psp"]
        total, successes, failures, failed_amount = _naive(rows, lambda e: e.psp == value)
        assert (stat.total, stat.successes, stat.failures) == (total, successes, failures)
        assert stat.amount_at_risk_paise == failed_amount
        assert stat.success_rate == pytest.approx(successes / total)
        seen += total
    assert seen == len(rows), "every event belongs to exactly one psp bucket"


def test_segment_stats_baseline_deviation_uses_the_segments_own_baseline(warm):
    store, sim = warm
    end = sim.now
    start = end - timedelta(minutes=5)
    b_end, b_start = start, start - timedelta(minutes=20)
    base_rows = store.slice(b_start, b_end)

    for stat in store.segment_stats(start, end, "issuer", b_start, b_end, min_volume=1):
        value = stat.segment.dimensions["issuer"]
        b_total, b_ok, _f, _a = _naive(base_rows, lambda e: e.issuer == value)
        assert (stat.baseline_total, stat.baseline_successes) == (b_total, b_ok)
        assert stat.baseline_success_rate == pytest.approx(b_ok / b_total)
        assert stat.deviation == pytest.approx(stat.success_rate - stat.baseline_success_rate)


def test_segment_stats_exclude_removes_that_segments_traffic(warm):
    """The residual test in the Investigation Agent depends on this being an exact removal."""
    store, sim = warm
    end = sim.now
    start = end - timedelta(minutes=10)
    rows = store.slice(start, end)
    excluded = Segment(dimensions={"payment_method": "upi"})

    for stat in store.segment_stats(start, end, "psp", min_volume=1, exclude=excluded):
        value = stat.segment.dimensions["psp"]
        total, successes, _f, _a = _naive(
            rows, lambda e: e.psp == value and e.payment_method != "upi"
        )
        assert (stat.total, stat.successes) == (total, successes)


def test_cross_segment_stats_counts_match_a_naive_count(warm):
    store, sim = warm
    end = sim.now
    start = end - timedelta(minutes=10)
    rows = store.slice(start, end)

    for stat in store.cross_segment_stats(start, end, ("payment_method", "psp"), min_volume=1):
        method = stat.segment.dimensions["payment_method"]
        psp = stat.segment.dimensions["psp"]
        total, successes, failures, failed_amount = _naive(
            rows, lambda e: e.payment_method == method and e.psp == psp
        )
        assert (stat.total, stat.successes, stat.failures) == (total, successes, failures)
        assert stat.amount_at_risk_paise == failed_amount


def test_union_counts_overlapping_segments_once(warm):
    """Summing per-segment windows double-counts wherever two segments describe one payment."""
    store, sim = warm
    end = sim.now
    start = end - timedelta(minutes=10)
    rows = store.slice(start, end)
    method = Segment(dimensions={"payment_method": "upi"})
    psp = Segment(dimensions={"psp": store.slice(start, end)[0].psp})

    window = store.union_metric_window(start, end, [method, psp])
    total, successes, _f, failed_amount = _naive(
        rows,
        lambda e: e.payment_method == method.dimensions["payment_method"]
        or e.psp == psp.dimensions["psp"],
    )
    assert (window.total, window.successes) == (total, successes)
    assert window.failed_gmv_paise == failed_amount

    separately = store.metric_window(start, end, {"payment_method": "upi"}).total
    separately += store.metric_window(start, end, {"psp": psp.dimensions["psp"]}).total
    assert window.total < separately, "the segments overlap, so the union must be smaller"


def test_success_rate_series_agrees_with_metric_window(warm):
    """The series takes a light path; it must not diverge from the full aggregate it stands in for."""
    store, sim = warm
    end = sim.now
    windows = store.success_rate_series(end, window_seconds=60, count=12)
    assert len(windows) == 12
    for window in windows:
        full = store.metric_window(window.start, window.end)
        assert (window.total, window.successes, window.failures) == (
            full.total,
            full.successes,
            full.failures,
        )
        assert window.success_rate == pytest.approx(full.success_rate)
        assert (window.gmv_paise, window.failed_gmv_paise) == (
            full.gmv_paise,
            full.failed_gmv_paise,
        )
    assert [w.start for w in windows] == sorted(w.start for w in windows), "oldest first"
