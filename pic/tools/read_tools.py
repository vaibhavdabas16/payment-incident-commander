"""Read tools — the agents' only window onto payment data.

Each returns plain JSON-able structures so an LLM can consume the output directly and the
evaluation harness can verify that a cited finding really came from a tool call.

Every tool takes an explicit time window rather than reading "now" implicitly, so an investigation
is reproducible: replaying the same tool calls against the same store yields the same evidence.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from ..store import wilson_lower_bound
from .registry import ToolContext, ToolSpec

# Default comparison: the incident window against the period preceding it.
DEFAULT_WINDOW_MINUTES = 10
DEFAULT_BASELINE_MINUTES = 40


def _windows(
    ctx: ToolContext, minutes: int | None, baseline_minutes: int | None
) -> tuple[datetime, datetime, datetime, datetime]:
    minutes = minutes or DEFAULT_WINDOW_MINUTES
    baseline_minutes = baseline_minutes or DEFAULT_BASELINE_MINUTES
    end = ctx.now
    start = end - timedelta(minutes=minutes)
    base_end = start
    base_start = base_end - timedelta(minutes=baseline_minutes)
    return start, end, base_start, base_end


def _pct(x: float) -> float:
    return round(x, 4)


def _segment_rows(
    ctx: ToolContext,
    dimensions: tuple[str, ...],
    minutes: int | None,
    baseline_minutes: int | None,
    min_volume: int,
    limit: int = 12,
) -> list[dict[str, Any]]:
    start, end, base_start, base_end = _windows(ctx, minutes, baseline_minutes)
    if len(dimensions) == 1:
        stats = ctx.store.segment_stats(
            start, end, dimensions[0], base_start, base_end, min_volume=min_volume
        )
    else:
        stats = ctx.store.cross_segment_stats(
            start, end, dimensions, base_start, base_end, min_volume=min_volume
        )
    rows = []
    for s in stats[:limit]:
        rows.append(
            {
                "segment": s.segment.dimensions,
                "transactions": s.total,
                "success_rate": _pct(s.success_rate),
                "baseline_success_rate": _pct(s.baseline_success_rate)
                if s.baseline_success_rate is not None
                else None,
                "deviation": _pct(s.deviation) if s.deviation is not None else None,
                "failure_share": _pct(s.failure_share),
                "traffic_share": _pct(s.traffic_share),
                "failed_value_paise": s.amount_at_risk_paise,
            }
        )
    return rows


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------


def get_success_rate_series(
    ctx: ToolContext, window_seconds: int = 120, count: int = 20, payment_method: str | None = None
) -> dict[str, Any]:
    filters = {"payment_method": payment_method} if payment_method else None
    series = ctx.store.success_rate_series(ctx.now, window_seconds, count, filters)
    return {
        "window_seconds": window_seconds,
        "filter": filters or {},
        "points": [
            {
                "start": w.start.isoformat(),
                "transactions": w.total,
                "success_rate": _pct(w.success_rate),
            }
            for w in series
        ],
    }


def get_transactions(
    ctx: ToolContext, minutes: int = 5, status: str | None = None, limit: int = 20
) -> dict[str, Any]:
    start, end, _, _ = _windows(ctx, minutes, None)
    rows = ctx.store.slice(start, end)
    if status:
        rows = [e for e in rows if e.status == status]
    sample = rows[-limit:]
    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "matched": len(rows),
        "sample": [
            {
                "payment_id": e.payment_id,
                "amount_paise": e.amount_paise,
                "payment_method": e.payment_method,
                "psp": e.psp,
                "issuer": e.issuer,
                "route_id": e.route_id,
                "os": e.os,
                "app_version": e.app_version,
                "geography": e.geography,
                "amount_band": e.amount_band,
                "status": e.status,
                "error_code": e.error_code,
                "latency_ms": e.latency_ms,
            }
            for e in sample
        ],
    }


def get_payment_failures(ctx: ToolContext, minutes: int = 10, limit: int = 25) -> dict[str, Any]:
    return get_transactions(ctx, minutes=minutes, status="failed", limit=limit)


def get_error_distribution(
    ctx: ToolContext, minutes: int | None = None, baseline_minutes: int | None = None
) -> dict[str, Any]:
    """Current vs baseline error mix. A code that barely existed before is the strongest signal."""
    start, end, base_start, base_end = _windows(ctx, minutes, baseline_minutes)
    current = ctx.store.metric_window(start, end)
    baseline = ctx.store.metric_window(base_start, base_end)

    cur_total = max(1, sum(current.error_distribution.values()))
    base_total = max(1, sum(baseline.error_distribution.values()))

    rows = []
    for code, count in sorted(current.error_distribution.items(), key=lambda kv: -kv[1]):
        cur_share = count / cur_total
        base_share = baseline.error_distribution.get(code, 0) / base_total
        rows.append(
            {
                "error_code": code,
                "count": count,
                "share": _pct(cur_share),
                "baseline_share": _pct(base_share),
                "share_delta": _pct(cur_share - base_share),
                # Ratio of shares. A code absent at baseline reports `null` rather than infinity.
                "lift": _pct(cur_share / base_share) if base_share > 0 else None,
                "novel": base_share == 0,
            }
        )
    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "total_failures": current.failures,
        "baseline_total_failures": baseline.failures,
        "errors": rows,
    }


def get_payment_method_metrics(
    ctx: ToolContext, minutes: int | None = None, baseline_minutes: int | None = None
) -> dict[str, Any]:
    return {"dimension": "payment_method", "rows": _segment_rows(ctx, ("payment_method",), minutes, baseline_minutes, 20)}


def get_gateway_metrics(
    ctx: ToolContext, minutes: int | None = None, baseline_minutes: int | None = None
) -> dict[str, Any]:
    return {
        "dimension": "gateway",
        "rows": _segment_rows(ctx, ("gateway",), minutes, baseline_minutes, 20),
        "by_route": _segment_rows(ctx, ("route_id",), minutes, baseline_minutes, 20),
        "by_psp": _segment_rows(ctx, ("psp",), minutes, baseline_minutes, 20),
    }


def get_bank_metrics(
    ctx: ToolContext, minutes: int | None = None, baseline_minutes: int | None = None
) -> dict[str, Any]:
    return {
        "dimension": "issuer",
        "rows": _segment_rows(ctx, ("issuer",), minutes, baseline_minutes, 20),
        "by_method_and_issuer": _segment_rows(
            ctx, ("payment_method", "issuer"), minutes, baseline_minutes, 25
        ),
    }


def get_geographic_metrics(
    ctx: ToolContext, minutes: int | None = None, baseline_minutes: int | None = None
) -> dict[str, Any]:
    return {"dimension": "geography", "rows": _segment_rows(ctx, ("geography",), minutes, baseline_minutes, 20)}


def get_device_metrics(
    ctx: ToolContext, minutes: int | None = None, baseline_minutes: int | None = None
) -> dict[str, Any]:
    return {
        "dimension": "device",
        "by_device": _segment_rows(ctx, ("device",), minutes, baseline_minutes, 20),
        "by_os": _segment_rows(ctx, ("os",), minutes, baseline_minutes, 20),
        "by_os_and_app_version": _segment_rows(
            ctx, ("os", "app_version"), minutes, baseline_minutes, 25
        ),
    }


def get_order_value_distribution(
    ctx: ToolContext, minutes: int | None = None, baseline_minutes: int | None = None
) -> dict[str, Any]:
    start, end, base_start, base_end = _windows(ctx, minutes, baseline_minutes)
    rows = ctx.store.slice(start, end)
    amounts = sorted(e.amount_paise for e in rows)

    def pctile(p: float) -> int:
        return amounts[int(p * (len(amounts) - 1))] if amounts else 0

    return {
        "by_amount_band": _segment_rows(ctx, ("amount_band",), minutes, baseline_minutes, 20),
        "by_method_and_band": _segment_rows(
            ctx, ("payment_method", "amount_band"), minutes, baseline_minutes, 20
        ),
        "percentiles_paise": {
            "p50": pctile(0.50),
            "p90": pctile(0.90),
            "p99": pctile(0.99),
        },
        "attempted_value_paise": sum(amounts),
    }


def get_latency_metrics(
    ctx: ToolContext, minutes: int | None = None, baseline_minutes: int | None = None
) -> dict[str, Any]:
    """Latency shift, split by route. A timeout cascade looks different from a decline spike."""
    start, end, base_start, base_end = _windows(ctx, minutes, baseline_minutes)
    current = ctx.store.metric_window(start, end)
    baseline = ctx.store.metric_window(base_start, base_end)

    by_route = []
    for route in sorted({e.route_id for e in ctx.store.slice(start, end)}):
        cur = ctx.store.metric_window(start, end, {"route_id": route})
        base = ctx.store.metric_window(base_start, base_end, {"route_id": route})
        by_route.append(
            {
                "route_id": route,
                "p95_latency_ms": round(cur.p95_latency_ms),
                "baseline_p95_latency_ms": round(base.p95_latency_ms),
                "delta_ms": round(cur.p95_latency_ms - base.p95_latency_ms),
                "transactions": cur.total,
            }
        )
    return {
        "p95_latency_ms": round(current.p95_latency_ms),
        "baseline_p95_latency_ms": round(baseline.p95_latency_ms),
        "delta_ms": round(current.p95_latency_ms - baseline.p95_latency_ms),
        "by_route": by_route,
    }


def get_traffic_composition(
    ctx: ToolContext,
    dimension: str = "payment_method",
    minutes: int | None = None,
    baseline_minutes: int | None = None,
) -> dict[str, Any]:
    """Mix shift, which distinguishes 'something broke' from 'different customers arrived'."""
    start, end, base_start, base_end = _windows(ctx, minutes, baseline_minutes)
    current = ctx.store.traffic_composition(start, end, dimension)
    baseline = ctx.store.traffic_composition(base_start, base_end, dimension)
    rows = []
    for key in sorted(set(current) | set(baseline)):
        cur, base = current.get(key, 0.0), baseline.get(key, 0.0)
        rows.append(
            {
                "value": key,
                "share": _pct(cur),
                "baseline_share": _pct(base),
                "share_delta": _pct(cur - base),
            }
        )
    total_shift = round(sum(abs(r["share_delta"]) for r in rows) / 2, 4)
    return {
        "dimension": dimension,
        "rows": rows,
        # Total variation distance: 0 means an identical mix, 1 means completely disjoint.
        "total_variation_distance": total_shift,
    }


def get_recent_configuration_changes(ctx: ToolContext, minutes: int = 120) -> dict[str, Any]:
    start = ctx.now - timedelta(minutes=minutes)
    changes = ctx.store.config_changes(start, ctx.now)
    return {
        "window_minutes": minutes,
        "changes": [
            {
                "change_id": c.change_id,
                "timestamp": c.timestamp.isoformat(),
                "component": c.component,
                "description": c.description,
                "changed_by": c.changed_by,
                "reversible": c.reversible,
                "minutes_ago": round((ctx.now - c.timestamp).total_seconds() / 60, 1),
            }
            for c in changes
        ],
    }


def get_historical_incidents(ctx: ToolContext, limit: int = 5, **features: Any) -> dict[str, Any]:
    """Similar past incidents from memory, for priors on cause and on what actually worked."""
    if ctx.memory is None:
        return {"matches": [], "note": "incident memory unavailable"}
    matches = ctx.memory.similar(features, limit=limit)
    return {"matches": matches}


def get_retry_effectiveness(ctx: ToolContext, minutes: int | None = None) -> dict[str, Any]:
    """Whether retries are recovering payments — decides if a retry change is worth proposing."""
    start, end, _, _ = _windows(ctx, minutes, None)
    rows = ctx.store.slice(start, end)
    retries = [e for e in rows if e.is_retry]
    recovered = sum(1 for e in retries if e.status == "success")
    by_error = Counter(e.error_code for e in retries if e.status == "failed" and e.error_code)
    return {
        "retry_attempts": len(retries),
        "retry_successes": recovered,
        "retry_success_rate": _pct(recovered / len(retries)) if retries else None,
        "retry_success_rate_lower_bound": _pct(wilson_lower_bound(recovered, len(retries)))
        if retries
        else None,
        "still_failing_after_retry": dict(by_error.most_common(5)),
    }


# --------------------------------------------------------------------------
# Declarations
# --------------------------------------------------------------------------

_WINDOW_PARAMS = {
    "minutes": {"type": "integer", "description": "Length of the analysis window in minutes."},
    "baseline_minutes": {
        "type": "integer",
        "description": "Length of the comparison window immediately before the analysis window.",
    },
}

SPECS = [
    ToolSpec(
        name="get_success_rate_series",
        description="Time series of payment success rate, optionally filtered to one payment method.",
        parameters={
            "window_seconds": {"type": "integer", "description": "Bucket size in seconds."},
            "count": {"type": "integer", "description": "Number of buckets, most recent last."},
            "payment_method": {"type": "string", "description": "Optional method filter."},
        },
        func=get_success_rate_series,
    ),
    ToolSpec(
        name="get_transactions",
        description="Sample of recent payment attempts with their full attributes.",
        parameters={
            "minutes": {"type": "integer"},
            "status": {"type": "string", "description": "'success' or 'failed'."},
            "limit": {"type": "integer"},
        },
        func=get_transactions,
    ),
    ToolSpec(
        name="get_payment_failures",
        description="Sample of recent failed payments with error codes and attributes.",
        parameters={"minutes": {"type": "integer"}, "limit": {"type": "integer"}},
        func=get_payment_failures,
    ),
    ToolSpec(
        name="get_error_distribution",
        description=(
            "Failure codes in the window versus baseline, with share delta, lift and whether the "
            "code is novel. The strongest single discriminator between causes."
        ),
        parameters=dict(_WINDOW_PARAMS),
        func=get_error_distribution,
    ),
    ToolSpec(
        name="get_payment_method_metrics",
        description="Success rate and deviation per payment method.",
        parameters=dict(_WINDOW_PARAMS),
        func=get_payment_method_metrics,
    ),
    ToolSpec(
        name="get_gateway_metrics",
        description="Success rate per gateway, route and PSP — isolates routing-side faults.",
        parameters=dict(_WINDOW_PARAMS),
        func=get_gateway_metrics,
    ),
    ToolSpec(
        name="get_bank_metrics",
        description="Success rate per issuing bank, and per payment method x issuer.",
        parameters=dict(_WINDOW_PARAMS),
        func=get_bank_metrics,
    ),
    ToolSpec(
        name="get_geographic_metrics",
        description="Success rate per geography.",
        parameters=dict(_WINDOW_PARAMS),
        func=get_geographic_metrics,
    ),
    ToolSpec(
        name="get_device_metrics",
        description="Success rate by device, OS, and OS x app version — isolates client regressions.",
        parameters=dict(_WINDOW_PARAMS),
        func=get_device_metrics,
    ),
    ToolSpec(
        name="get_order_value_distribution",
        description="Success rate by order-value band and percentiles of attempted value.",
        parameters=dict(_WINDOW_PARAMS),
        func=get_order_value_distribution,
    ),
    ToolSpec(
        name="get_latency_metrics",
        description="p95 latency now versus baseline, overall and per route.",
        parameters=dict(_WINDOW_PARAMS),
        func=get_latency_metrics,
    ),
    ToolSpec(
        name="get_traffic_composition",
        description=(
            "Traffic mix now versus baseline on one dimension, with total variation distance. "
            "Distinguishes an infrastructure fault from a change in who is paying."
        ),
        parameters={
            "dimension": {"type": "string", "description": "e.g. payment_method, device, geography"},
            **_WINDOW_PARAMS,
        },
        func=get_traffic_composition,
    ),
    ToolSpec(
        name="get_recent_configuration_changes",
        description="Merchant-side configuration changes in the recent past, newest first.",
        parameters={"minutes": {"type": "integer"}},
        func=get_recent_configuration_changes,
    ),
    ToolSpec(
        name="get_historical_incidents",
        description="Past incidents resembling the current one, with their causes and outcomes.",
        parameters={"limit": {"type": "integer"}},
        func=get_historical_incidents,
    ),
    ToolSpec(
        name="get_retry_effectiveness",
        description="How well retries are currently recovering failed payments.",
        parameters={"minutes": {"type": "integer"}},
        func=get_retry_effectiveness,
    ),
]
