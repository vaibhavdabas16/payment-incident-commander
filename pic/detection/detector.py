"""Deterministic anomaly detection (ADR-001).

Detection runs at two levels, because real payment incidents arrive in two shapes.

**Headline detection** catches drops big enough to move the merchant's overall success rate.
Three independent tests must agree before an incident opens:

1. *Statistical* — robust z-score of the current window against a rolling baseline.
2. *Material*    — the drop is large enough in absolute and relative terms to matter.
3. *Sufficient*  — enough transactions in the window to trust the estimate.

**Segment detection** catches what headline monitoring structurally cannot. A single issuer
failing on cards may be only ~3 points of overall success rate — invisible against normal
variation — while being one of the most expensive incidents a merchant can have, because card
traffic carries a high average order value. Each segment is therefore tested against *its own*
baseline with a two-proportion test, and promoted on revenue at risk rather than on how much it
moves the headline number.

A CUSUM change-point test runs alongside as corroboration; it raises confidence but never opens an
incident alone. Requiring agreement is what keeps false positives low on `SCN-TRAFFIC-MIX`, where
success rate genuinely falls but nothing is broken.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import DetectionConfig, settings
from ..schemas import AnomalySignal, MetricWindow, SegmentStat, Severity
from ..store import EventStore
from .statistics import (
    confidence_from_evidence,
    cusum,
    ewma,
    median,
    robust_z,
    two_proportion_ztest,
)

# Dimensions attributed against, broadest first.
SINGLE_DIMENSIONS = (
    "payment_method",
    "psp",
    "gateway",
    "issuer",
    "route_id",
    "os",
    "geography",
    "device",
    "amount_band",
)
CROSS_DIMENSIONS = (
    ("payment_method", "psp"),
    ("payment_method", "issuer"),
    ("os", "app_version"),
    ("payment_method", "geography"),
    ("gateway", "route_id"),
    ("payment_method", "amount_band"),
)


@dataclass
class BaselineSnapshot:
    windows: list[MetricWindow]
    rates: list[float]
    ewma_baseline: float
    median_baseline: float
    start: datetime
    end: datetime

    @property
    def sufficient(self) -> bool:
        return len(self.rates) >= settings.detection.min_baseline_windows


class Detector:
    def __init__(self, store: EventStore, config: DetectionConfig | None = None) -> None:
        self.store = store
        self.config = config or settings.detection
        self._incident_seq = 0
        # Half-open intervals of known-degraded time, excluded from every baseline.
        self._degraded_periods: list[tuple[datetime, datetime | None]] = []

    # -------------------------------------------------- degraded-period marks

    def mark_degraded_from(self, start: datetime) -> None:
        """Record that a degradation is known to be in progress from `start`.

        Without this a long outage quietly becomes the new normal: the rolling baseline keeps
        absorbing degraded windows until the "expected" success rate has fallen to match the
        failure, the deviation shrinks to zero, and the detector stops reporting a problem that is
        still costing the merchant money every minute. Excluding known-bad periods keeps the
        baseline anchored to healthy behaviour for as long as the incident lasts.
        """
        if self._degraded_periods and self._degraded_periods[-1][1] is None:
            return
        self._degraded_periods.append((start, None))

    def mark_recovered(self, end: datetime) -> None:
        if self._degraded_periods and self._degraded_periods[-1][1] is None:
            self._degraded_periods[-1] = (self._degraded_periods[-1][0], end)

    def _is_degraded_window(self, window: MetricWindow) -> bool:
        for start, end in self._degraded_periods:
            if window.end > start and (end is None or window.start < end):
                return True
        return False

    # ------------------------------------------------------------- baselines

    def baseline(self, now: datetime, filters: dict[str, str] | None = None) -> BaselineSnapshot:
        """Rolling baseline from the windows immediately preceding the current one."""
        cfg = self.config
        current_start = now - timedelta(seconds=cfg.window_seconds)

        # Look further back than the baseline actually needs, so that windows excluded for being
        # known-degraded can be replaced by healthy ones from before the incident began. Without
        # the deeper search, a long outage eventually covers the entire lookback, every window is
        # excluded, and the baseline collapses to zero - which silently disables detection at the
        # exact moment it matters most.
        windows = self.store.success_rate_series(
            end=current_start,
            window_seconds=cfg.window_seconds,
            count=cfg.baseline_windows * cfg.baseline_lookback_multiplier,
            filters=filters,
        )
        healthy = [
            w
            for w in windows
            if w.total >= max(10, cfg.min_sample_size // 3) and not self._is_degraded_window(w)
        ]
        # Most recent healthy windows, still in chronological order for the EWMA.
        usable = healthy[-cfg.baseline_windows :]
        rates = [w.success_rate for w in usable]
        return BaselineSnapshot(
            windows=usable,
            rates=rates,
            ewma_baseline=ewma(rates, cfg.ewma_alpha) if rates else 0.0,
            median_baseline=median(rates) if rates else 0.0,
            start=windows[0].start if windows else current_start,
            end=current_start,
        )

    # ------------------------------------------------------------- detection

    def evaluate(self, now: datetime) -> AnomalySignal | None:
        cfg = self.config
        window_start = now - timedelta(seconds=cfg.window_seconds)
        current = self.store.metric_window(window_start, now)

        if current.total < cfg.min_sample_size:
            return None

        base = self.baseline(now)
        if not base.sufficient:
            return None
        baseline_rate = base.ewma_baseline
        if baseline_rate <= 0:
            return None

        drop = baseline_rate - current.success_rate
        relative_drop = drop / baseline_rate
        z = robust_z(current.success_rate, base.rates)
        change = cusum(base.rates + [current.success_rate], k=cfg.cusum_k, h=cfg.cusum_h)

        headline_fired = (
            z <= -cfg.z_threshold
            and drop >= cfg.min_absolute_drop
            and relative_drop >= cfg.min_relative_drop
        )

        segments = self.attribute(window_start, now, base)
        segment_hits = [s for s in segments if self._segment_is_anomalous(s)]
        revenue_at_risk = self.estimate_revenue_at_risk(window_start, now, base, segments)

        if headline_fired:
            confidence = confidence_from_evidence(
                z, current.total, drop, change.change_point_detected
            )
            methods = ["robust_z", "absolute_threshold"]
            if change.change_point_detected:
                methods.append("cusum_change_point")
            metric = "payment_success_rate"
        elif segment_hits:
            # Headline is inside normal variation but a segment is not. Promote only when the money
            # at stake justifies opening an incident, otherwise this becomes a noise machine.
            segment_risk = self.estimate_revenue_at_risk(
                window_start, now, base, segment_hits
            )
            if segment_risk < cfg.min_segment_revenue_at_risk_paise:
                return None
            worst = min(segment_hits, key=lambda s: s.deviation or 0.0)
            confidence = self._segment_confidence(worst, segment_risk)
            methods = ["segment_proportion_test", "revenue_weighted_promotion"]
            metric = f"segment_success_rate[{worst.segment.label()}]"
            revenue_at_risk = max(revenue_at_risk, segment_risk)
        else:
            # Nothing at the fast cadence. Sweep again over a longer window before giving up: a
            # narrow segment (one payment method in two states) may carry only a handful of
            # transactions per two-minute window, too few to distinguish from noise no matter how
            # badly it is failing. Trading detection latency for statistical power is the only way
            # to see these at all.
            slow = self._slow_sweep(now)
            if slow is None:
                return None
            segments, segment_hits, revenue_at_risk, window_start = slow
            worst = min(segment_hits, key=lambda s: s.deviation or 0.0)
            confidence = self._segment_confidence(worst, revenue_at_risk)
            methods = ["long_window_segment_test", "revenue_weighted_promotion"]
            metric = f"segment_success_rate[{worst.segment.label()}]"

        if confidence < cfg.min_confidence:
            return None

        # No id is minted here. A degradation produces a signal on every cycle it persists, and
        # numbering them would make incident ids race far ahead of the incidents that actually
        # exist - INC-0016 on screen when two incidents have ever opened. The supervisor assigns an
        # id only when a signal turns out to be a genuinely new incident.
        return AnomalySignal(
            incident_id="",
            detected_at=now,
            severity=self.severity(drop, revenue_at_risk),
            metric=metric,
            current_value=round(current.success_rate, 4),
            baseline=round(baseline_rate, 4),
            deviation=round(-drop, 4),
            confidence=confidence,
            z_score=round(z, 2),
            change_point_detected=change.change_point_detected,
            sample_size=current.total,
            affected_segments=segments,
            estimated_revenue_at_risk_paise=revenue_at_risk,
            detection_method=methods,
            window_start=window_start,
            window_end=now,
        )

    def _slow_sweep(
        self, now: datetime
    ) -> tuple[list[SegmentStat], list[SegmentStat], int, datetime] | None:
        """Second detection tier over a longer window, for low-volume segments."""
        cfg = self.config
        window_start = now - timedelta(seconds=cfg.slow_window_seconds)
        baseline_start = window_start - timedelta(seconds=cfg.slow_baseline_seconds)
        base = BaselineSnapshot(
            windows=self.store.success_rate_series(
                end=window_start,
                window_seconds=cfg.window_seconds,
                count=cfg.slow_baseline_seconds // cfg.window_seconds,
            ),
            rates=[],
            ewma_baseline=0.0,
            median_baseline=0.0,
            start=baseline_start,
            end=window_start,
        )
        segments = self.attribute(window_start, now, base)
        hits = [s for s in segments if self._segment_is_anomalous(s, slow=True)]
        if not hits:
            return None
        risk = self.estimate_revenue_at_risk(window_start, now, base, hits)
        if risk < cfg.min_segment_revenue_at_risk_paise:
            return None
        return segments, hits, risk, window_start

    def _segment_is_anomalous(self, s: SegmentStat, slow: bool = False) -> bool:
        """A segment drop that is both statistically real and operationally meaningful."""
        cfg = self.config
        min_volume = cfg.min_slow_segment_volume if slow else cfg.min_segment_detection_volume
        if s.deviation is None or s.baseline_total < cfg.min_segment_volume:
            return False
        if s.total < min_volume:
            return False
        if s.deviation > -cfg.min_segment_drop:
            return False
        return s.p_value < cfg.segment_significance_level

    def _segment_confidence(self, worst: SegmentStat, revenue_at_risk: int) -> float:
        """Confidence for a segment-only detection.

        Deliberately capped below a headline detection's ceiling: a segment anomaly is a narrower
        observation and should carry proportionally less certainty into diagnosis.
        """
        depth = min(1.0, abs(worst.deviation or 0.0) / 0.35)
        volume = min(1.0, worst.total / 250.0)
        money = min(1.0, revenue_at_risk / self.config.high_revenue_at_risk_paise)
        return round(min(0.92, 0.45 * depth + 0.25 * volume + 0.30 * money), 3)

    # ----------------------------------------------------------- attribution

    def attribute(self, start: datetime, end: datetime, base: BaselineSnapshot) -> list[SegmentStat]:
        """Rank the slices responsible for the drop.

        Scored on three things at once so no single artefact can dominate: how far the segment has
        fallen below its own baseline, what share of all failures it carries, and a Wilson lower
        bound that discounts thin-volume segments.

        Two filters then remove the noise that makes an attribution list useless in practice:

        * **Echo suppression.** When UPI degrades, `device=mobile` also looks degraded simply
          because UPI is mobile-heavy. Echoes carry a large failure share but a much shallower
          deviation than the true culprit, so a segment must fall at least `echo_floor_ratio` as far
          as the strongest candidate to be reported at all.
        * **Duplicate suppression.** `gateway=gw_primary & route_id=route_A` describes the same
          transactions as `route_id=route_A`. Segments covering a near-identical failing population
          collapse to the one already selected.
        """
        cfg = self.config
        candidates: list[tuple[float, SegmentStat]] = []

        def consider(stats: list[SegmentStat], min_share: float) -> None:
            for s in stats:
                if s.deviation is None or s.deviation >= -0.02:
                    continue
                if s.failure_share < min_share:
                    continue
                if s.baseline_total > 0:
                    test = two_proportion_ztest(
                        s.successes, s.total, s.baseline_successes, s.baseline_total
                    )
                    s.p_value = round(test.p_value, 6)
                score = (
                    0.45 * min(1.0, abs(s.deviation) / 0.30)
                    + 0.35 * min(1.0, s.failure_share / 0.60)
                    + 0.20 * min(1.0, s.failure_rate_lower_bound / 0.40)
                )
                candidates.append((score, s))

        for dim in SINGLE_DIMENSIONS:
            consider(
                self.store.segment_stats(
                    start, end, dim, baseline_start=base.start, baseline_end=base.end
                ),
                min_share=0.10,
            )
        for dims in CROSS_DIMENSIONS:
            consider(
                self.store.cross_segment_stats(
                    start,
                    end,
                    dims,
                    baseline_start=base.start,
                    baseline_end=base.end,
                    min_volume=cfg.min_cross_segment_volume,
                ),
                min_share=0.15,
            )

        if not candidates:
            return []

        candidates.sort(key=lambda x: x[0], reverse=True)
        deepest = max(abs(s.deviation or 0.0) for _, s in candidates)
        floor = deepest * cfg.echo_floor_ratio

        selected: list[SegmentStat] = []
        for _score, stat in candidates:
            if abs(stat.deviation or 0.0) < floor:
                continue
            if any(_duplicate(stat, chosen) for chosen in selected):
                continue
            selected.append(stat)
            if len(selected) >= cfg.max_segments_reported:
                break
        return selected

    # --------------------------------------------------------------- impact

    def estimate_revenue_at_risk(
        self,
        start: datetime,
        end: datetime,
        base: BaselineSnapshot,
        segments: list[SegmentStat] | None = None,
    ) -> int:
        """Revenue at risk per hour, summed over a disjoint stratification of traffic.

        Traffic is partitioned by `(payment_method, amount_band)` - a true partition, so every
        transaction belongs to exactly one stratum. For each stratum the excess failure *rate*
        against its own baseline is multiplied by that stratum's own average order value, and the
        contributions are summed.

        Each part of that is load-bearing, and each replaced something that measured badly against
        simulator ground truth:

        * **Rates, not realised failed GMV.** Order amounts are lognormal with a long tail, so
          summing the value of transactions that actually failed lets two large failures in a
          two-minute window triple the estimate. A failure rate is a binomial proportion and far
          more stable.

        * **Disjoint strata, not a union of affected segments.** Affected segments overlap
          (`psp=psp_yes` and `route_id=route_B` can be the same transactions), so summing them
          double-counts. But a *union* is no better: averaging order value across a union that
          mixes UPI and cards prices low-value UPI failures at a card-sized ticket. Only a
          partition gets both the count and the value right.

        * **Average value from the baseline window.** Forty minutes of history estimates a mean
          order value far more precisely than two, so the figure does not jitter between
          evaluations.

        A stratum contributes only when its drop passes a lenient significance test. Without that,
        summing `max(0, drop)` across a dozen strata accumulates one-sided noise and reports
        revenue at risk during a perfectly healthy window.
        """
        window_hours = (end - start).total_seconds() / 3600.0
        if window_hours <= 0:
            return 0

        strata = self.store.cross_segment_stats(
            start,
            end,
            ("payment_method", "amount_band"),
            baseline_start=base.start,
            baseline_end=base.end,
            min_volume=self.config.min_stratum_volume,
        )

        total = 0.0
        for stratum in strata:
            if stratum.deviation is None or stratum.deviation >= 0 or stratum.baseline_total == 0:
                continue
            test = two_proportion_ztest(
                stratum.successes, stratum.total, stratum.baseline_successes, stratum.baseline_total
            )
            if test.p_value > self.config.stratum_significance_level:
                continue
            baseline_value = self.store.union_metric_window(
                base.start, base.end, [stratum.segment]
            )
            if baseline_value.total == 0:
                continue
            avg_value = (
                baseline_value.gmv_paise + baseline_value.failed_gmv_paise
            ) / baseline_value.total
            excess_failures_per_hour = abs(stratum.deviation) * stratum.total / window_hours
            total += excess_failures_per_hour * avg_value

        _ = segments  # attribution is reported separately; valuation must stay on the partition
        return int(total)

    def severity(self, drop: float, revenue_at_risk_paise: int) -> Severity:
        low, med, high = self.config.severity_bands
        cfg = self.config
        if drop >= high or revenue_at_risk_paise >= cfg.critical_revenue_at_risk_paise:
            return Severity.CRITICAL
        if drop >= med or revenue_at_risk_paise >= cfg.high_revenue_at_risk_paise:
            return Severity.HIGH
        if drop >= low:
            return Severity.MEDIUM
        return Severity.LOW


def _duplicate(candidate: SegmentStat, chosen: SegmentStat) -> bool:
    """True when `candidate` describes essentially the same failing traffic as `chosen`.

    Either it is a broader slice containing the chosen one (same dimension values, fewer keys), or
    its failure count and volume are within 10% — which is what happens when two dimensions alias
    each other, like a gateway that only serves one route.
    """
    cand, chos = candidate.segment.dimensions, chosen.segment.dimensions
    if cand == chos:
        return True
    if set(cand).issubset(set(chos)) and all(chos[k] == v for k, v in cand.items()):
        return True

    def close(a: int, b: int) -> bool:
        return abs(a - b) <= 0.10 * max(1, max(a, b))

    return close(candidate.failures, chosen.failures) and close(candidate.total, chosen.total)
