"""In-memory event index.

The durable record is SQLite (`pic/database.py`); this is the query surface the tools use.
Events are held in a timestamp-sorted list with a parallel epoch array, so a window slice is a
bisect rather than a scan. At demo scale (~10^5 events) this keeps every tool call sub-millisecond,
which matters because a single incident triggers a dozen of them and the UI streams live.
"""

from __future__ import annotations

import bisect
import math
from collections import Counter
from operator import attrgetter
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from .config import settings
from .schemas import ConfigChange, MetricWindow, PaymentEvent, Segment, SegmentStat


def _epoch(ts: datetime) -> float:
    return ts.timestamp()


# `attrgetter` is built once per dimension tuple and reused. A segment match runs once per event
# per candidate segment inside `union_metric_window`, which is the innermost loop of the detection
# sweep, and rebuilding the getter there dominated the comparison it was doing.
_GETTER_CACHE: dict[tuple[str, ...], Any] = {}


def _getter(keys: tuple[str, ...]):
    getter = _GETTER_CACHE.get(keys)
    if getter is None:
        # attrgetter returns a bare value for one key and a tuple for several; the single-key case
        # is wrapped so every caller can treat the result as a tuple.
        raw = attrgetter(*keys)
        getter = (lambda e, _raw=raw: (_raw(e),)) if len(keys) == 1 else raw
        _GETTER_CACHE[keys] = getter
    return getter


def _segment_probe(segment: Segment) -> tuple[Any, tuple[str, ...]]:
    """A segment as (getter, expected values), so matching is one call and one tuple compare."""
    keys = tuple(segment.dimensions)
    return _getter(keys), tuple(str(v) for v in segment.dimensions.values())


def _matches(event: PaymentEvent, segment: Segment) -> bool:
    getter, expected = _segment_probe(segment)
    return tuple(map(str, getter(event))) == expected


def _without(rows: list[PaymentEvent], segment: Segment) -> list[PaymentEvent]:
    """`rows` with one segment's traffic removed, matching each event exactly once."""
    getter, expected = _segment_probe(segment)
    return [e for e in rows if tuple(map(str, getter(e))) != expected]


# (total, successes, failures, failed amount in paise) per bucket. Four integers is everything a
# `SegmentStat` needs from the events in it, so the events themselves are never retained.
_Tally = tuple[int, int, int, int]


def _tally(rows: Iterable[PaymentEvent], dimension: str) -> dict[str, _Tally]:
    """Group by one dimension, accumulating counters in a single pass.

    Bucketing on the raw attribute and stringifying once per distinct value at the end, rather
    than once per event: a dimension has a handful of values and the window has hundreds of
    thousands of events. `status` is `success | failed`, so anything not a success is a failure.

    This relies on every sliceable dimension being declared `str` on `PaymentEvent` — two raw
    values that stringify alike would be counted apart and then collide in the returned dict. The
    numeric fields (amount, latency, retry count) are not dimensions and are never passed here.
    """
    out: dict[Any, list[int]] = {}
    get = attrgetter(dimension)
    for e in rows:
        value = get(e)
        if value is None:
            continue
        cell = out.get(value)
        if cell is None:
            cell = out[value] = [0, 0, 0, 0]
        cell[0] += 1
        if e.status == "success":
            cell[1] += 1
        else:
            cell[2] += 1
            cell[3] += e.amount_paise
    return {str(k): (c[0], c[1], c[2], c[3]) for k, c in out.items()}


def _tally_cross(
    rows: Iterable[PaymentEvent], dimensions: Sequence[str]
) -> dict[tuple[str, ...], _Tally]:
    """`_tally` over a joint key, e.g. (payment_method, psp)."""
    out: dict[tuple[Any, ...], list[int]] = {}
    get = _getter(tuple(dimensions))
    for e in rows:
        key = get(e)
        if None in key:
            continue
        cell = out.get(key)
        if cell is None:
            cell = out[key] = [0, 0, 0, 0]
        cell[0] += 1
        if e.status == "success":
            cell[1] += 1
        else:
            cell[2] += 1
            cell[3] += e.amount_paise
    return {tuple(map(str, k)): (c[0], c[1], c[2], c[3]) for k, c in out.items()}


def wilson_lower_bound(successes: int, total: int, z: float = 1.96) -> float:
    """Lower bound of the Wilson score interval.

    Used so a 3-of-4 failure segment does not outrank a 400-of-1000 one.
    """
    if total == 0:
        return 0.0
    p = successes / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return max(0.0, (centre - margin) / denom)


# Dimensions the investigation agent slices on, in the order it tries them.
SEGMENT_DIMENSIONS = (
    "payment_method",
    "psp",
    "gateway",
    "issuer",
    "route_id",
    "geography",
    "device",
    "os",
    "error_code",
    "app_version",
)


# The deepest history any consumer asks for is the detector's baseline lookback: 20 windows of
# 120s with a 6x search multiplier is four hours. Six hours keeps a margin without letting a
# long-running process accumulate for ever. At the live 60x speed-up the simulator produces about
# 90,000 events an hour, so an unbounded store grows by tens of megabytes an hour and a small
# container eventually dies - which is a strange way for a monitoring system to fail.
DEFAULT_RETENTION_SECONDS = 4.5 * 3600

# `_dirty` is a hook for batch persistence and nothing drains it today, so it would otherwise pin
# every event ever generated and defeat the pruning above.
MAX_DIRTY = 5_000


class EventStore:
    def __init__(self, retention_seconds: float | None = DEFAULT_RETENTION_SECONDS) -> None:
        self._events: list[PaymentEvent] = []
        self._epochs: list[float] = []
        self._config_changes: list[ConfigChange] = []
        self._dirty: list[PaymentEvent] = []
        self._retention_seconds = retention_seconds
        self._adds_since_prune = 0

    # ---------------------------------------------------------------- writes

    def add(self, event: PaymentEvent) -> None:
        ts = _epoch(event.timestamp)
        if self._epochs and ts < self._epochs[-1]:
            idx = bisect.bisect_right(self._epochs, ts)
            self._epochs.insert(idx, ts)
            self._events.insert(idx, event)
        else:
            self._epochs.append(ts)
            self._events.append(event)
        self._dirty.append(event)
        if len(self._dirty) > MAX_DIRTY:
            del self._dirty[: len(self._dirty) - MAX_DIRTY]

        # Pruning in batches rather than on every insert: the check is cheap but the deletion is
        # not, and history older than the retention window is of no use to anyone.
        self._adds_since_prune += 1
        if self._retention_seconds and self._adds_since_prune >= 1_000:
            self._prune(ts)

    def extend(self, events: Iterable[PaymentEvent]) -> None:
        for e in events:
            self.add(e)

    def add_config_change(self, change: ConfigChange) -> None:
        self._config_changes.append(change)

    def _prune(self, newest_epoch: float) -> None:
        """Drop events older than the retention window."""
        self._adds_since_prune = 0
        assert self._retention_seconds is not None
        cutoff = newest_epoch - self._retention_seconds
        index = bisect.bisect_left(self._epochs, cutoff)
        if index > 0:
            del self._epochs[:index]
            del self._events[:index]

    def take_dirty(self) -> list[PaymentEvent]:
        out, self._dirty = self._dirty, []
        return out

    # ---------------------------------------------------------------- reads

    def __len__(self) -> int:
        return len(self._events)

    @property
    def events(self) -> Sequence[PaymentEvent]:
        return self._events

    def first_timestamp(self) -> datetime | None:
        return self._events[0].timestamp if self._events else None

    def last_timestamp(self) -> datetime | None:
        return self._events[-1].timestamp if self._events else None

    def slice(self, start: datetime, end: datetime) -> list[PaymentEvent]:
        lo = bisect.bisect_left(self._epochs, _epoch(start))
        hi = bisect.bisect_left(self._epochs, _epoch(end))
        return self._events[lo:hi]

    def filtered(
        self, start: datetime, end: datetime, filters: dict[str, str] | None = None
    ) -> list[PaymentEvent]:
        rows = self.slice(start, end)
        if not filters:
            return rows
        out = []
        for e in rows:
            if all(getattr(e, k, None) == v for k, v in filters.items()):
                out.append(e)
        return out

    def config_changes(self, start: datetime, end: datetime) -> list[ConfigChange]:
        return [c for c in self._config_changes if start <= c.timestamp < end]

    # ------------------------------------------------------------ aggregates

    def metric_window(
        self, start: datetime, end: datetime, filters: dict[str, str] | None = None
    ) -> MetricWindow:
        rows = self.filtered(start, end, filters)
        total = len(rows)
        successes = sum(1 for e in rows if e.status == "success")
        failures = total - successes
        gmv = sum(e.amount_paise for e in rows if e.status == "success")
        failed_gmv = sum(e.amount_paise for e in rows if e.status == "failed")
        errors = Counter(e.error_code for e in rows if e.error_code)
        latencies = sorted(e.latency_ms for e in rows)
        p95 = float(latencies[int(0.95 * (len(latencies) - 1))]) if latencies else 0.0
        return MetricWindow(
            start=start,
            end=end,
            total=total,
            successes=successes,
            failures=failures,
            success_rate=(successes / total) if total else 0.0,
            gmv_paise=gmv,
            failed_gmv_paise=failed_gmv,
            p95_latency_ms=p95,
            error_distribution=dict(errors),
        )

    def success_rate_series(
        self,
        end: datetime,
        window_seconds: int,
        count: int,
        filters: dict[str, str] | None = None,
    ) -> list[MetricWindow]:
        """The `count` most recent consecutive windows ending at `end`, oldest first.

        Rate, volume and GMV only. `metric_window` additionally sorts every latency in the window
        to take a p95 and counts every error code, and no caller of this method reads either — but
        the detector asks for 120 consecutive windows on every sweep to build its baseline, so
        that discarded work was the single most expensive thing detection did.
        """
        out: list[MetricWindow] = []
        step = timedelta(seconds=window_seconds)
        cursor_end = end
        for _ in range(count):
            cursor_start = cursor_end - step
            rows = self.filtered(cursor_start, cursor_end, filters)
            total = len(rows)
            successes = 0
            gmv = 0
            failed_gmv = 0
            for e in rows:
                if e.status == "success":
                    successes += 1
                    gmv += e.amount_paise
                elif e.status == "failed":
                    failed_gmv += e.amount_paise
            out.append(
                MetricWindow(
                    start=cursor_start,
                    end=cursor_end,
                    total=total,
                    successes=successes,
                    failures=total - successes,
                    success_rate=(successes / total) if total else 0.0,
                    gmv_paise=gmv,
                    failed_gmv_paise=failed_gmv,
                )
            )
            cursor_end = cursor_start
        return list(reversed(out))

    def segment_stats(
        self,
        start: datetime,
        end: datetime,
        dimension: str,
        baseline_start: datetime | None = None,
        baseline_end: datetime | None = None,
        min_volume: int | None = None,
        exclude: Segment | None = None,
    ) -> list[SegmentStat]:
        """Per-value performance on one dimension, with baseline deviation and failure share.

        `exclude` drops every event belonging to a given segment from both the current and baseline
        windows. That supports the residual test in the Investigation Agent: asking whether a
        dimension still looks degraded once the already-identified culprit's traffic is removed.
        """
        min_volume = min_volume if min_volume is not None else settings.detection.min_segment_volume
        rows = self.slice(start, end)
        if exclude is not None:
            rows = _without(rows, exclude)

        # One pass, accumulating counters rather than collecting each bucket's events and walking
        # them again per statistic. The lists were the expensive part: at four hours of retention
        # this method was appending well over a million events into per-value buckets on every
        # detection sweep, only to reduce each bucket to four integers.
        buckets = _tally(rows, dimension)
        total_all = len(rows)
        total_failures = sum(b[2] for b in buckets.values())

        baseline_counts: dict[str, tuple[int, int]] = {}
        baseline_rate: dict[str, float] = {}
        if baseline_start and baseline_end:
            base_rows = self.slice(baseline_start, baseline_end)
            if exclude is not None:
                base_rows = _without(base_rows, exclude)
            for value, (b_total, b_ok, _b_failed, _b_amount) in _tally(base_rows, dimension).items():
                if b_total:
                    baseline_counts[value] = (b_total, b_ok)
                    baseline_rate[value] = b_ok / b_total

        stats: list[SegmentStat] = []
        for value, (total, successes, failures, failed_amount) in buckets.items():
            if total < min_volume:
                continue
            rate = successes / total
            base = baseline_rate.get(value)
            b_total, b_ok = baseline_counts.get(value, (0, 0))
            stats.append(
                SegmentStat(
                    segment=Segment(dimensions={dimension: value}),
                    baseline_total=b_total,
                    baseline_successes=b_ok,
                    total=total,
                    successes=successes,
                    failures=failures,
                    success_rate=rate,
                    baseline_success_rate=base,
                    deviation=(rate - base) if base is not None else None,
                    failure_share=(failures / total_failures) if total_failures else 0.0,
                    traffic_share=(total / total_all) if total_all else 0.0,
                    failure_rate_lower_bound=wilson_lower_bound(failures, total),
                    amount_at_risk_paise=failed_amount,
                )
            )
        stats.sort(key=lambda s: (s.failure_rate_lower_bound, s.failure_share), reverse=True)
        return stats

    def cross_segment_stats(
        self,
        start: datetime,
        end: datetime,
        dimensions: Sequence[str],
        baseline_start: datetime | None = None,
        baseline_end: datetime | None = None,
        min_volume: int | None = None,
    ) -> list[SegmentStat]:
        """Joint slice over several dimensions, e.g. (payment_method, psp).

        Used to separate 'all UPI is broken' from 'one PSP within UPI is broken'.
        """
        min_volume = min_volume if min_volume is not None else settings.detection.min_segment_volume
        rows = self.slice(start, end)
        total_all = len(rows)
        buckets = _tally_cross(rows, dimensions)
        total_failures = sum(b[2] for b in buckets.values())

        base_buckets: dict[tuple[str, ...], _Tally] = {}
        if baseline_start and baseline_end:
            base_buckets = _tally_cross(self.slice(baseline_start, baseline_end), dimensions)

        stats: list[SegmentStat] = []
        for k, (total, successes, failures, failed_amount) in buckets.items():
            if total < min_volume:
                continue
            rate = successes / total
            b_total, b_ok, _b_failures, _b_amount = base_buckets.get(k, (0, 0, 0, 0))
            base = (b_ok / b_total) if b_total else None
            stats.append(
                SegmentStat(
                    segment=Segment(dimensions=dict(zip(dimensions, k))),
                    baseline_total=b_total,
                    baseline_successes=b_ok,
                    total=total,
                    successes=successes,
                    failures=failures,
                    success_rate=rate,
                    baseline_success_rate=base,
                    deviation=(rate - base) if base is not None else None,
                    failure_share=(failures / total_failures) if total_failures else 0.0,
                    traffic_share=(total / total_all) if total_all else 0.0,
                    failure_rate_lower_bound=wilson_lower_bound(failures, total),
                    amount_at_risk_paise=failed_amount,
                )
            )
        stats.sort(key=lambda s: (s.failure_rate_lower_bound, s.failure_share), reverse=True)
        return stats

    def union_metric_window(
        self, start: datetime, end: datetime, segments: Sequence[Segment]
    ) -> MetricWindow:
        """Aggregate over events matching ANY of the given segments, counting each event once.

        Summing per-segment estimates double-counts whenever segments overlap — and they usually
        do, since `psp=psp_axis` and `route_id=route_A` often describe the same transactions. The
        union is the only way to get an unbiased total.
        """
        # The probes are built once rather than per event per segment, and the counters accumulate
        # in the same pass that matches — this is the innermost loop of revenue estimation, which
        # runs over every event in the window for each candidate segment.
        probes = [_segment_probe(seg) for seg in segments]
        total = successes = gmv = failed_gmv = 0
        for e in self.slice(start, end):
            for getter, expected in probes:
                if tuple(map(str, getter(e))) == expected:
                    break
            else:
                continue
            total += 1
            if e.status == "success":
                successes += 1
                gmv += e.amount_paise
            elif e.status == "failed":
                failed_gmv += e.amount_paise
        return MetricWindow(
            start=start,
            end=end,
            total=total,
            successes=successes,
            failures=total - successes,
            success_rate=(successes / total) if total else 0.0,
            gmv_paise=gmv,
            failed_gmv_paise=failed_gmv,
        )

    def traffic_composition(
        self, start: datetime, end: datetime, dimension: str
    ) -> dict[str, float]:
        rows = self.slice(start, end)
        if not rows:
            return {}
        counts = Counter(str(getattr(e, dimension, "unknown")) for e in rows)
        return {k: v / len(rows) for k, v in counts.items()}

    def unique_customers(self, start: datetime, end: datetime, failed_only: bool = True) -> int:
        """Distinct order owners. The simulator uses one order per customer attempt chain."""
        rows = self.slice(start, end)
        if failed_only:
            rows = [e for e in rows if e.status == "failed"]
        return len({e.order_id for e in rows})


# Process-wide store used by the API and the demo runner.
_store = EventStore()


def get_store() -> EventStore:
    return _store


def reset_store() -> EventStore:
    global _store
    _store = EventStore()
    return _store
