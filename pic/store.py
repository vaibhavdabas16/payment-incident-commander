"""In-memory event index.

The durable record is SQLite (`pic/database.py`); this is the query surface the tools use.
Events are held in a timestamp-sorted list with a parallel epoch array, so a window slice is a
bisect rather than a scan. At demo scale (~10^5 events) this keeps every tool call sub-millisecond,
which matters because a single incident triggers a dozen of them and the UI streams live.
"""

from __future__ import annotations

import bisect
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Iterable, Sequence

from .config import settings
from .schemas import ConfigChange, MetricWindow, PaymentEvent, Segment, SegmentStat


def _epoch(ts: datetime) -> float:
    return ts.timestamp()


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


class EventStore:
    def __init__(self) -> None:
        self._events: list[PaymentEvent] = []
        self._epochs: list[float] = []
        self._config_changes: list[ConfigChange] = []
        self._dirty: list[PaymentEvent] = []

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

    def extend(self, events: Iterable[PaymentEvent]) -> None:
        for e in events:
            self.add(e)

    def add_config_change(self, change: ConfigChange) -> None:
        self._config_changes.append(change)

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
        """The `count` most recent consecutive windows ending at `end`, oldest first."""
        out: list[MetricWindow] = []
        step = timedelta(seconds=window_seconds)
        cursor_end = end
        for _ in range(count):
            cursor_start = cursor_end - step
            out.append(self.metric_window(cursor_start, cursor_end, filters))
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
    ) -> list[SegmentStat]:
        """Per-value performance on one dimension, with baseline deviation and failure share."""
        min_volume = min_volume if min_volume is not None else settings.detection.min_segment_volume
        rows = self.slice(start, end)
        total_all = len(rows)
        total_failures = sum(1 for e in rows if e.status == "failed")

        buckets: dict[str, list[PaymentEvent]] = defaultdict(list)
        for e in rows:
            value = getattr(e, dimension, None)
            if value is None:
                continue
            buckets[str(value)].append(e)

        baseline_rate: dict[str, float] = {}
        if baseline_start and baseline_end:
            base_rows = self.slice(baseline_start, baseline_end)
            base_buckets: dict[str, list[PaymentEvent]] = defaultdict(list)
            for e in base_rows:
                value = getattr(e, dimension, None)
                if value is None:
                    continue
                base_buckets[str(value)].append(e)
            for value, items in base_buckets.items():
                if items:
                    baseline_rate[value] = sum(1 for i in items if i.status == "success") / len(items)

        stats: list[SegmentStat] = []
        for value, items in buckets.items():
            total = len(items)
            if total < min_volume:
                continue
            successes = sum(1 for i in items if i.status == "success")
            failures = total - successes
            rate = successes / total
            base = baseline_rate.get(value)
            stats.append(
                SegmentStat(
                    segment=Segment(dimensions={dimension: value}),
                    total=total,
                    successes=successes,
                    failures=failures,
                    success_rate=rate,
                    baseline_success_rate=base,
                    deviation=(rate - base) if base is not None else None,
                    failure_share=(failures / total_failures) if total_failures else 0.0,
                    traffic_share=(total / total_all) if total_all else 0.0,
                    failure_rate_lower_bound=wilson_lower_bound(failures, total),
                    amount_at_risk_paise=sum(i.amount_paise for i in items if i.status == "failed"),
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
        total_failures = sum(1 for e in rows if e.status == "failed")

        def key_of(e: PaymentEvent) -> tuple[str, ...] | None:
            vals = []
            for d in dimensions:
                v = getattr(e, d, None)
                if v is None:
                    return None
                vals.append(str(v))
            return tuple(vals)

        buckets: dict[tuple[str, ...], list[PaymentEvent]] = defaultdict(list)
        for e in rows:
            k = key_of(e)
            if k is not None:
                buckets[k].append(e)

        base_buckets: dict[tuple[str, ...], list[PaymentEvent]] = defaultdict(list)
        if baseline_start and baseline_end:
            for e in self.slice(baseline_start, baseline_end):
                k = key_of(e)
                if k is not None:
                    base_buckets[k].append(e)

        stats: list[SegmentStat] = []
        for k, items in buckets.items():
            total = len(items)
            if total < min_volume:
                continue
            successes = sum(1 for i in items if i.status == "success")
            failures = total - successes
            rate = successes / total
            base_items = base_buckets.get(k, [])
            base = (
                sum(1 for i in base_items if i.status == "success") / len(base_items)
                if base_items
                else None
            )
            stats.append(
                SegmentStat(
                    segment=Segment(dimensions=dict(zip(dimensions, k))),
                    total=total,
                    successes=successes,
                    failures=failures,
                    success_rate=rate,
                    baseline_success_rate=base,
                    deviation=(rate - base) if base is not None else None,
                    failure_share=(failures / total_failures) if total_failures else 0.0,
                    traffic_share=(total / total_all) if total_all else 0.0,
                    failure_rate_lower_bound=wilson_lower_bound(failures, total),
                    amount_at_risk_paise=sum(i.amount_paise for i in items if i.status == "failed"),
                )
            )
        stats.sort(key=lambda s: (s.failure_rate_lower_bound, s.failure_share), reverse=True)
        return stats

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
