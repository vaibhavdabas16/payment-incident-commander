"""Payment event simulator.

Two properties make this more than a data faker:

1. **A control plane the agent actually mutates.** Route weights, disabled methods and retry policy
   live here. When the Action Agent shifts traffic, the generator samples differently from that
   moment on, and success rate genuinely moves. Verification is therefore measuring a real effect,
   not a scripted one.

2. **Exact ground truth.** Because degradation is applied to a known nominal success probability,
   the expected revenue loss is computed from the generative process itself
   (`amount x (p_nominal - p_effective)`), giving the evaluation harness a true value to score the
   agent's estimate against.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from ..config import SimulationConfig, settings
from ..schemas import ConfigChange, PaymentEvent
from ..store import EventStore
from .scenarios import Scenario

# --------------------------------------------------------------------------
# Static traffic profile
# --------------------------------------------------------------------------

ROUTES = {
    "route_A": {"gateway": "gw_primary", "psp": "psp_axis"},
    "route_B": {"gateway": "gw_primary", "psp": "psp_yes"},
    "route_C": {"gateway": "gw_secondary", "psp": "psp_hdfc"},
}

DEFAULT_ROUTE_WEIGHTS = {"route_A": 0.55, "route_B": 0.25, "route_C": 0.20}

METHOD_MIX = {"upi": 0.55, "card": 0.25, "netbanking": 0.10, "wallet": 0.07, "emi": 0.03}

BASE_SUCCESS = {"upi": 0.94, "card": 0.89, "netbanking": 0.86, "wallet": 0.95, "emi": 0.84}

ISSUER_MIX = {"HDFC": 0.28, "ICICI": 0.22, "SBI": 0.20, "AXIS": 0.16, "KOTAK": 0.14}
ISSUER_QUALITY = {"HDFC": 1.005, "ICICI": 1.0, "SBI": 0.985, "AXIS": 1.0, "KOTAK": 0.995}

GEO_MIX = {"MH": 0.22, "KA": 0.16, "DL": 0.14, "TN": 0.12, "UP": 0.11, "GJ": 0.10, "WB": 0.09, "OR": 0.06}

DEVICE_MIX = {"mobile": 0.78, "desktop": 0.17, "tablet": 0.05}
OS_BY_DEVICE = {
    "mobile": {"android": 0.72, "ios": 0.28},
    "tablet": {"android": 0.55, "ios": 0.45},
    "desktop": {"windows": 0.68, "macos": 0.32},
}
APP_VERSIONS = {"8.4.1": 0.45, "8.4.2": 0.33, "8.5.0": 0.22}
NETWORKS = {"visa": 0.42, "mastercard": 0.34, "rupay": 0.24}

# Mean/sigma of the lognormal amount distribution in rupees, per method.
AMOUNT_LOGNORM = {
    "upi": (6.4, 1.05),
    "card": (7.5, 1.15),
    "netbanking": (7.9, 1.0),
    "wallet": (5.9, 0.85),
    "emi": (9.6, 0.6),
}

# Failure codes that occur at baseline, independent of any injected scenario.
BASELINE_ERRORS = {
    "INSUFFICIENT_FUNDS": 0.32,
    "USER_DROPPED": 0.24,
    "BANK_DECLINE": 0.18,
    "AUTH_TIMEOUT": 0.12,
    "INVALID_VPA": 0.08,
    "CARD_EXPIRED": 0.06,
}

LATENCY_LOGNORM = {"upi": (7.1, 0.45), "card": (7.5, 0.5), "netbanking": (7.9, 0.55), "wallet": (6.7, 0.4), "emi": (7.6, 0.5)}

# Errors a retry has a realistic chance of clearing.
TRANSIENT_ERRORS = {"AUTH_TIMEOUT", "GATEWAY_TIMEOUT", "PSP_UNAVAILABLE", "BANK_UNAVAILABLE", "CHECKOUT_CALLBACK_TIMEOUT"}

HIGH_VALUE_PAISE = 1_000_000  # INR 10,000 - about 7% of card traffic, enough volume to reason about
LOW_VALUE_PAISE = 100_000  # INR 1,000


def _weighted(rng: random.Random, mapping: dict[str, float]) -> str:
    keys = list(mapping)
    weights = [mapping[k] for k in keys]
    return rng.choices(keys, weights=weights, k=1)[0]


def _blend(base: dict[str, float], target: dict[str, float], t: float) -> dict[str, float]:
    keys = set(base) | set(target)
    return {k: (1 - t) * base.get(k, 0.0) + t * target.get(k, 0.0) for k in keys}


# --------------------------------------------------------------------------
# Control plane — mutated by the Action Agent
# --------------------------------------------------------------------------


@dataclass
class Intervention:
    action: str
    parameters: dict
    applied_at: datetime
    active: bool = True


def _change_id_for(scenario: Scenario) -> str:
    """The id of the configuration change a scenario records, if it records one."""
    return f"chg_{scenario.scenario_id.lower()}"


@dataclass
class ControlPlane:
    """Live routing/retry configuration. The Action Agent's write tools mutate this object."""

    route_weights: dict[str, dict[str, float]] = field(default_factory=dict)
    disabled_methods: set[str] = field(default_factory=set)
    max_retries: int = 1
    retry_enabled: bool = True
    monitoring_interval_s: int = 120
    interventions: list[Intervention] = field(default_factory=list)
    # Configuration changes the agent has reverted. A scenario whose degradation was caused by a
    # recorded change stops applying once that change is rolled back, so reverting a bad deploy
    # actually fixes the thing it broke.
    rolled_back_changes: set[str] = field(default_factory=set)
    # Order-recovery campaigns, keyed by id. Kept here rather than in the event store because a
    # recovered order is not a new payment attempt in the merchant's stream, and injecting it as
    # one would corrupt the very success-rate series verification is measuring.
    recovery_campaigns: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.route_weights:
            self.route_weights = {m: dict(DEFAULT_ROUTE_WEIGHTS) for m in METHOD_MIX}

    def weights_for(self, method: str) -> dict[str, float]:
        return self.route_weights.get(method, dict(DEFAULT_ROUTE_WEIGHTS))

    def shift_traffic(
        self, from_route: str, to_route: str, percentage: float, payment_method: str | None = None
    ) -> dict:
        """Move `percentage` points of absolute traffic share from one route to another.

        Returns the before/after weights so the action can be audited and inverted exactly.
        """
        methods = [payment_method] if payment_method else list(self.route_weights)
        before, after = {}, {}
        moved_total = 0.0
        for m in methods:
            w = self.weights_for(m)
            before[m] = dict(w)
            movable = min(w.get(from_route, 0.0), percentage / 100.0)
            w[from_route] = w.get(from_route, 0.0) - movable
            w[to_route] = w.get(to_route, 0.0) + movable
            self.route_weights[m] = w
            after[m] = dict(w)
            moved_total += movable
        return {
            "from_route": from_route,
            "to_route": to_route,
            "percentage": percentage,
            "payment_method": payment_method,
            "weights_before": before,
            "weights_after": after,
            "effective_share_moved": round(moved_total / max(1, len(methods)), 4),
        }

    def disable_method(self, method: str) -> dict:
        self.disabled_methods.add(method)
        return {"payment_method": method, "disabled": True}

    def enable_method(self, method: str) -> dict:
        self.disabled_methods.discard(method)
        return {"payment_method": method, "disabled": False}

    def configure_retry(self, max_retries: int, enabled: bool = True) -> dict:
        before = {"max_retries": self.max_retries, "retry_enabled": self.retry_enabled}
        self.max_retries = max_retries
        self.retry_enabled = enabled
        return {"before": before, "after": {"max_retries": max_retries, "retry_enabled": enabled}}

    def rollback_config_change(self, change_id: str) -> dict:
        """Revert a merchant configuration change.

        Previously this was recorded as intent only: the simulator kept degrading traffic no
        matter what the agent did, so the one action that could actually fix a config regression
        was guaranteed to look useless. Verification then read no improvement and the agent
        reverted its own correct fix - which is why every failed rollback in the benchmark was a
        `rollback_change`.
        """
        before = change_id in self.rolled_back_changes
        self.rolled_back_changes.add(change_id)
        return {"change_id": change_id, "already_rolled_back": before}

    def restore_config_change(self, change_id: str) -> dict:
        """Re-apply a change, so reverting a rollback is itself reversible."""
        self.rolled_back_changes.discard(change_id)
        return {"change_id": change_id, "restored": True}

    # ------------------------------------------------- order recovery layer

    def recover_payments(
        self, campaign_id: str, orders: list[dict], methods: list[str] | None = None
    ) -> dict:
        """Re-present failed payments and report which of them actually completed.

        Outcomes are drawn per order from a generator seeded on `(campaign_id, order_id)`, so the
        result is reproducible for a given seed and independent of the payment stream's own RNG —
        running a recovery campaign does not perturb the traffic the Verification Agent is
        measuring, which it would if these were injected as new payment events.

        That separation is deliberate and is the boundary of the simulation: recovered payments are
        counted as recovered orders, not as new rows in the payment stream. See
        `docs/ARCHITECTURE.md` on what this layer does and does not model.
        """
        allowed = set(methods or [])
        attempted = 0
        recovered = 0
        recovered_value = 0
        attempted_value = 0
        by_method: dict[str, int] = {}
        skipped: dict[str, int] = {}

        for order in orders:
            method = str(order.get("recovery_method", "retry"))
            if allowed and method not in allowed:
                skipped[method] = skipped.get(method, 0) + 1
                continue
            amount = int(order.get("amount_paise", 0))
            probability = float(order.get("recovery_probability", 0.0))
            attempted += 1
            attempted_value += amount
            rng = random.Random(f"{campaign_id}:{order.get('order_id')}")
            if rng.random() < probability:
                recovered += 1
                recovered_value += amount
                by_method[method] = by_method.get(method, 0) + 1

        campaign = {
            "campaign_id": campaign_id,
            "attempted": attempted,
            "recovered": recovered,
            "attempted_value_paise": attempted_value,
            "recovered_value_paise": recovered_value,
            "by_method": by_method,
            "skipped_by_method": skipped,
            "cancelled": False,
        }
        self.recovery_campaigns[campaign_id] = campaign
        return dict(campaign)

    def cancel_recovery(self, campaign_id: str) -> dict:
        """Stop a recovery campaign.

        The honest inverse of `recover_payments`, and its limits are stated rather than glossed:
        it halts any further attempts and marks the campaign cancelled. It cannot un-charge a
        payment a customer has already completed, because nothing can — that money is in the
        merchant's account and reversing it would be a refund, not a rollback. What it guarantees
        is that the campaign stops and that the audit trail says when.
        """
        campaign = self.recovery_campaigns.get(campaign_id)
        if campaign is None:
            return {"campaign_id": campaign_id, "cancelled": True, "known": False}
        campaign["cancelled"] = True
        return {
            "campaign_id": campaign_id,
            "cancelled": True,
            "known": True,
            "already_recovered": campaign["recovered"],
            "note": (
                "Further attempts stopped. Payments already completed by customers are not "
                "reversed by a rollback."
            ),
        }

    def snapshot(self) -> dict:
        return {
            "rolled_back_changes": sorted(self.rolled_back_changes),
            "route_weights": {m: dict(w) for m, w in self.route_weights.items()},
            "disabled_methods": sorted(self.disabled_methods),
            "max_retries": self.max_retries,
            "retry_enabled": self.retry_enabled,
            "monitoring_interval_s": self.monitoring_interval_s,
            "recovery_campaigns": {
                cid: {
                    "attempted": c["attempted"],
                    "recovered": c["recovered"],
                    "recovered_value_paise": c["recovered_value_paise"],
                    "cancelled": c["cancelled"],
                }
                for cid, c in self.recovery_campaigns.items()
            },
        }


# --------------------------------------------------------------------------
# Ground truth accounting
# --------------------------------------------------------------------------


@dataclass
class GroundTruthAccumulator:
    """Exact expected loss, accumulated from the generative process (never estimated).

    Loss is bucketed per minute rather than kept as a running total. Degradations ramp in and out,
    so a single total divided by elapsed time would understate the loss rate at full severity — and
    the evaluation harness needs to score the agent's estimate against the *same* window the agent
    observed, not against a ramp-diluted average.
    """

    expected_lost_paise: float = 0.0
    expected_lost_txns: float = 0.0
    attempts: int = 0
    window_start: datetime | None = None
    window_end: datetime | None = None
    # Minute bucket (floor of the timestamp) -> expected loss in that minute.
    buckets: dict[datetime, float] = field(default_factory=dict)
    txn_buckets: dict[datetime, float] = field(default_factory=dict)

    def record(self, ts: datetime, amount_paise: int, p_nominal: float, p_effective: float) -> None:
        delta = max(0.0, p_nominal - p_effective)
        self.expected_lost_paise += amount_paise * delta
        self.expected_lost_txns += delta
        self.attempts += 1
        minute = ts.replace(second=0, microsecond=0)
        self.buckets[minute] = self.buckets.get(minute, 0.0) + amount_paise * delta
        self.txn_buckets[minute] = self.txn_buckets.get(minute, 0.0) + delta
        if self.window_start is None or ts < self.window_start:
            self.window_start = ts
        if self.window_end is None or ts > self.window_end:
            self.window_end = ts

    def per_hour_paise(self) -> int:
        """Loss rate over the whole scenario, ramps included."""
        if not self.window_start or not self.window_end:
            return 0
        seconds = max(1.0, (self.window_end - self.window_start).total_seconds())
        return int(self.expected_lost_paise * 3600.0 / seconds)

    def per_hour_paise_between(self, start: datetime, end: datetime) -> int:
        """Loss rate over a specific window — the fair comparison for an agent estimate."""
        seconds = (end - start).total_seconds()
        if seconds <= 0:
            return 0
        total = sum(v for minute, v in self.buckets.items() if start <= minute < end)
        return int(total * 3600.0 / seconds)

    def lost_txns_between(self, start: datetime, end: datetime) -> float:
        return sum(v for minute, v in self.txn_buckets.items() if start <= minute < end)


# --------------------------------------------------------------------------
# Simulator
# --------------------------------------------------------------------------


@dataclass
class ActiveScenario:
    scenario: Scenario
    started_at: datetime

    def ends_at(self) -> datetime:
        return self.started_at + timedelta(seconds=self.scenario.duration_s)


class PaymentSimulator:
    def __init__(
        self,
        store: EventStore,
        config: SimulationConfig | None = None,
        seed: int | None = None,
        start_time: datetime | None = None,
    ) -> None:
        self.store = store
        self.config = config or settings.simulation
        self.rng = random.Random(seed if seed is not None else self.config.seed)
        self.control = ControlPlane()
        self.now = start_time or datetime.now(timezone.utc).replace(microsecond=0)
        self.start_time = self.now
        self.active: list[ActiveScenario] = []
        self.ground_truth: dict[str, GroundTruthAccumulator] = {}
        self._seq = 0
        self._pending_retries: list[tuple[datetime, PaymentEvent, str]] = []

    # ------------------------------------------------------------- scenarios

    def activate(self, scenario: Scenario, at: datetime | None = None) -> ActiveScenario:
        at = at or self.now
        active = ActiveScenario(scenario=scenario, started_at=at)
        self.active.append(active)
        self.ground_truth.setdefault(scenario.scenario_id, GroundTruthAccumulator())
        if scenario.config_change:
            change = ConfigChange(
                change_id=_change_id_for(scenario),
                timestamp=at - timedelta(seconds=scenario.config_change_lead_s),
                merchant_id=self.config.merchant_id,
                component=scenario.config_change["component"],
                description=scenario.config_change["description"],
                changed_by=scenario.config_change.get("changed_by", "unknown"),
                reversible=True,
            )
            self.store.add_config_change(change)
        return active

    def deactivate_all(self) -> None:
        self.active = []

    def _live_scenarios(self, ts: datetime) -> list[tuple[Scenario, float]]:
        out = []
        for a in self.active:
            # A degradation caused by a configuration change ends when that change is reverted.
            # Without this the simulator punished the correct fix.
            if a.scenario.config_change and _change_id_for(a.scenario) in self.control.rolled_back_changes:
                continue
            elapsed = (ts - a.started_at).total_seconds()
            intensity = a.scenario.intensity(elapsed)
            if intensity > 0:
                out.append((a.scenario, intensity))
        return out

    # ------------------------------------------------------------ generation

    def _arrival_rate(self, ts: datetime) -> float:
        """Payments per minute, with a diurnal shape so baselines are not flat."""
        hour = ts.hour + ts.minute / 60.0
        diurnal = 1.0 + self.config.diurnal_amplitude * math.sin((hour - 6) / 24.0 * 2 * math.pi)
        return self.config.base_rate_per_min * diurnal

    def _sample_attrs(self, _ts: datetime, live: list[tuple[Scenario, float]]) -> dict:
        rng = self.rng

        method_mix = dict(METHOD_MIX)
        for scenario, intensity in live:
            shift = scenario.traffic_mix_shift.get("payment_method")
            if shift:
                method_mix = _blend(method_mix, shift, intensity)

        allowed = {m: w for m, w in method_mix.items() if m not in self.control.disabled_methods}
        if not allowed:
            allowed = dict(method_mix)
        total = sum(allowed.values()) or 1.0
        method = _weighted(rng, {k: v / total for k, v in allowed.items()})

        weights = self.control.weights_for(method)
        positive = {k: v for k, v in weights.items() if v > 0}
        route = _weighted(rng, positive) if positive else "route_A"
        routing = ROUTES[route]

        device = _weighted(rng, DEVICE_MIX)
        os_name = _weighted(rng, OS_BY_DEVICE[device])
        mu, sigma = AMOUNT_LOGNORM[method]
        amount_paise = max(1000, int(rng.lognormvariate(mu, sigma) * 100))
        band = "high" if amount_paise >= HIGH_VALUE_PAISE else ("low" if amount_paise < LOW_VALUE_PAISE else "mid")

        return {
            "payment_method": method,
            "route_id": route,
            "gateway": routing["gateway"],
            "psp": routing["psp"],
            "issuer": _weighted(rng, ISSUER_MIX),
            "geography": _weighted(rng, GEO_MIX),
            "device": device,
            "os": os_name,
            "app_version": _weighted(rng, APP_VERSIONS),
            "network": _weighted(rng, NETWORKS) if method in ("card", "emi") else None,
            "amount_paise": amount_paise,
            "amount_band": band,
        }

    def _nominal_success(self, attrs: dict) -> float:
        p = BASE_SUCCESS[attrs["payment_method"]]
        p *= ISSUER_QUALITY.get(attrs["issuer"], 1.0)
        if attrs["gateway"] == "gw_secondary":
            p *= 0.995
        if attrs["amount_band"] == "high":
            p *= 0.97
        return min(0.995, p)

    def _apply_scenarios(
        self, attrs: dict, p_nominal: float, live: list[tuple[Scenario, float]]
    ) -> tuple[float, str | None, float, int, str | None]:
        """Returns (p_effective, forced_error_code, latency_mult, latency_add, scenario_id)."""
        p = p_nominal
        error_code = None
        latency_mult = 1.0
        latency_add = 0
        owner: str | None = None
        for scenario, intensity in live:
            for effect in scenario.effects:
                if not effect.matches(attrs):
                    continue
                # Interpolate the multiplier by ramp intensity: 1.0 -> configured multiplier.
                mult = 1.0 + (effect.success_multiplier - 1.0) * intensity
                if mult < 1.0:
                    p *= mult
                    if effect.error_code:
                        error_code = effect.error_code
                    owner = scenario.scenario_id
                latency_mult *= 1.0 + (effect.latency_multiplier - 1.0) * intensity
                latency_add += int(effect.latency_add_ms * intensity)
        return max(0.001, p), error_code, latency_mult, latency_add, owner

    def _make_event(
        self, ts: datetime, live: list[tuple[Scenario, float]], retry_of: PaymentEvent | None = None
    ) -> PaymentEvent:
        rng = self.rng
        self._seq += 1
        if retry_of is not None:
            attrs = {
                "payment_method": retry_of.payment_method,
                "route_id": retry_of.route_id,
                "gateway": retry_of.gateway,
                "psp": retry_of.psp,
                "issuer": retry_of.issuer,
                "geography": retry_of.geography,
                "device": retry_of.device,
                "os": retry_of.os,
                "app_version": retry_of.app_version,
                "network": retry_of.network,
                "amount_paise": retry_of.amount_paise,
                "amount_band": (
                    "high"
                    if retry_of.amount_paise >= HIGH_VALUE_PAISE
                    else ("low" if retry_of.amount_paise < LOW_VALUE_PAISE else "mid")
                ),
            }
        else:
            attrs = self._sample_attrs(ts, live)

        p_nominal = self._nominal_success(attrs)
        p_eff, forced_error, lat_mult, lat_add, owner = self._apply_scenarios(attrs, p_nominal, live)

        if owner:
            self.ground_truth.setdefault(owner, GroundTruthAccumulator()).record(
                ts, attrs["amount_paise"], p_nominal, p_eff
            )

        success = rng.random() < p_eff
        error_code = None
        if not success:
            # Of all failures, the fraction attributable to the scenario is
            # (p_nominal - p_eff) / (1 - p_eff); the rest are ordinary baseline failures.
            # Sampling this way keeps the error distribution realistic: a degraded segment still
            # shows background INSUFFICIENT_FUNDS alongside the scenario's signature code.
            attributable = max(0.0, (p_nominal - p_eff) / max(1e-9, 1.0 - p_eff))
            if forced_error and rng.random() < min(0.95, attributable):
                error_code = forced_error
            else:
                error_code = _weighted(rng, BASELINE_ERRORS)

        mu, sigma = LATENCY_LOGNORM[attrs["payment_method"]]
        latency = int(rng.lognormvariate(mu, sigma) * lat_mult) + lat_add
        latency = max(80, min(45_000, latency))

        order_id = retry_of.order_id if retry_of else f"order_{self._seq:08d}"
        event = PaymentEvent(
            payment_id=f"pay_{self._seq:08d}",
            order_id=order_id,
            timestamp=ts,
            merchant_id=self.config.merchant_id,
            amount_paise=attrs["amount_paise"],
            payment_method=attrs["payment_method"],
            gateway=attrs["gateway"],
            psp=attrs["psp"],
            issuer=attrs["issuer"],
            network=attrs["network"],
            geography=attrs["geography"],
            device=attrs["device"],
            os=attrs["os"],
            app_version=attrs["app_version"],
            status="success" if success else "failed",
            error_code=error_code,
            latency_ms=latency,
            retry_count=(retry_of.retry_count + 1) if retry_of else 0,
            is_retry=retry_of is not None,
            route_id=attrs["route_id"],
            amount_band=attrs["amount_band"],
        )

        if (
            not success
            and self.control.retry_enabled
            and event.retry_count < self.control.max_retries
            and error_code in TRANSIENT_ERRORS
            and rng.random() < 0.45
        ):
            self._pending_retries.append((ts + timedelta(seconds=rng.randint(15, 75)), event, error_code))

        return event

    def advance_to(self, target: datetime) -> list[PaymentEvent]:
        """Generate every event between `self.now` and `target`, one second at a time."""
        produced: list[PaymentEvent] = []
        while self.now < target:
            second = self.now
            live = self._live_scenarios(second)
            rate_per_s = self._arrival_rate(second) / 60.0
            count = self._poisson(rate_per_s)

            due = [p for p in self._pending_retries if p[0] <= second]
            if due:
                self._pending_retries = [p for p in self._pending_retries if p[0] > second]
                for _, original, _err in due:
                    produced.append(self._make_event(second, live, retry_of=original))

            for _ in range(count):
                offset = timedelta(milliseconds=self.rng.randint(0, 999))
                produced.append(self._make_event(second + offset, live))

            self.now = second + timedelta(seconds=1)

        produced.sort(key=lambda e: e.timestamp)
        self.store.extend(produced)
        return produced

    def advance_seconds(self, seconds: float) -> list[PaymentEvent]:
        return self.advance_to(self.now + timedelta(seconds=seconds))

    def _poisson(self, lam: float) -> int:
        """Knuth's algorithm — fine at our rate (~3/s) and keeps the RNG stream reproducible."""
        if lam <= 0:
            return 0
        limit = math.exp(-lam)
        k, p = 0, 1.0
        while True:
            p *= self.rng.random()
            if p <= limit:
                return k
            k += 1
            if k > 1000:
                return k

    # ------------------------------------------------------------------ misc

    def warmup(self, minutes: int = 45) -> list[PaymentEvent]:
        """Generate clean history so the detector has a real baseline to work from."""
        return self.advance_seconds(minutes * 60)

    def true_revenue_at_risk_per_hour(
        self, scenario_id: str, start: datetime | None = None, end: datetime | None = None
    ) -> int:
        acc = self.ground_truth.get(scenario_id)
        if not acc:
            return 0
        if start and end:
            return acc.per_hour_paise_between(start, end)
        return acc.per_hour_paise()


def iter_events(events: Iterable[PaymentEvent]) -> Iterable[PaymentEvent]:
    return events
