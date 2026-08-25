"""Injectable degradation scenarios with ground truth.

A scenario perturbs the generative process for a bounded time over a matched slice of traffic.
Because the perturbation is applied at generation time, the *true* expected revenue loss is known
exactly — the harness compares the agent's estimate against it rather than against another guess.

`root_cause_id` values must exist in `pic.agents.root_cause.HYPOTHESIS_CATALOGUE`; a test asserts
this so a scenario can never be unattributable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


@dataclass
class Effect:
    """A perturbation applied to events matching `match`."""

    match: dict[str, Sequence[str] | str] = field(default_factory=dict)
    # Multiplies the nominal success probability (0.35 => a severe degradation).
    success_multiplier: float = 1.0
    # Error code emitted when this effect causes the failure.
    error_code: str | None = None
    latency_multiplier: float = 1.0
    latency_add_ms: int = 0

    def matches(self, attrs: dict[str, str]) -> bool:
        for key, want in self.match.items():
            got = attrs.get(key)
            if got is None:
                return False
            if isinstance(want, str):
                if got != want:
                    return False
            elif got not in want:
                return False
        return True


@dataclass
class Scenario:
    scenario_id: str
    name: str
    description: str
    root_cause_id: str
    start_offset_s: int
    duration_s: int
    effects: list[Effect] = field(default_factory=list)
    # Seconds over which the effect ramps in and out. Real degradations are not step functions,
    # and a ramp is what makes detection latency a meaningful measurement.
    ramp_s: int = 90
    # Shifts the sampling mix, e.g. a marketing push changing the device/method composition.
    traffic_mix_shift: dict[str, dict[str, float]] = field(default_factory=dict)
    # Emits a discoverable ConfigChange record this many seconds before onset.
    config_change: dict[str, str] | None = None
    config_change_lead_s: int = 240
    recommended_action: str = "shift_traffic"
    # If False, the obvious fallback is also degraded — the failed-intervention demo.
    fallback_healthy: bool = True
    # Route the recommended shift should move traffic away from / toward.
    from_route: str | None = None
    to_route: str | None = None

    def intensity(self, elapsed_s: float) -> float:
        """Ramp factor in [0, 1] for a given time since scenario start."""
        if elapsed_s < 0 or elapsed_s > self.duration_s:
            return 0.0
        if self.ramp_s <= 0:
            return 1.0
        ramp_in = min(1.0, elapsed_s / self.ramp_s)
        remaining = self.duration_s - elapsed_s
        ramp_out = min(1.0, remaining / self.ramp_s)
        return max(0.0, min(ramp_in, ramp_out))


# --------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------

UPI_PSP_DEGRADATION = Scenario(
    scenario_id="SCN-UPI-PSP",
    name="UPI PSP route degradation",
    description=(
        "A single UPI PSP on route_A starts declining collect requests. UPI is the dominant "
        "method, so headline success rate drops hard while cards stay healthy."
    ),
    root_cause_id="psp_degradation",
    start_offset_s=0,
    duration_s=2400,
    effects=[
        Effect(
            match={"payment_method": "upi", "psp": "psp_axis"},
            success_multiplier=0.34,
            error_code="PSP_UNAVAILABLE",
            latency_add_ms=1800,
        )
    ],
    recommended_action="shift_traffic",
    fallback_healthy=True,
    from_route="route_A",
    to_route="route_B",
)

ISSUER_DEGRADATION = Scenario(
    scenario_id="SCN-ISSUER",
    name="Issuer-specific card declines",
    description=(
        "One issuing bank starts declining card authorisations across every gateway. No routing "
        "change can fix this, which is what makes it a good test of action selection."
    ),
    root_cause_id="issuer_degradation",
    start_offset_s=0,
    duration_s=2100,
    effects=[
        Effect(
            match={"payment_method": ["card", "emi"], "issuer": "HDFC"},
            success_multiplier=0.42,
            error_code="ISSUER_DECLINE",
        )
    ],
    recommended_action="notify_merchant",
    fallback_healthy=True,
)

ANDROID_CHECKOUT_REGRESSION = Scenario(
    scenario_id="SCN-ANDROID",
    name="Android checkout regression",
    description=(
        "A checkout SDK release breaks callback handling on one Android app version. A config "
        "change is recorded shortly before onset, so rollback is the correct action."
    ),
    root_cause_id="config_regression",
    start_offset_s=0,
    duration_s=2100,
    effects=[
        Effect(
            match={"os": "android", "app_version": "8.5.0"},
            success_multiplier=0.30,
            error_code="CHECKOUT_CALLBACK_TIMEOUT",
            latency_add_ms=2600,
        )
    ],
    config_change={
        "component": "checkout_sdk",
        "description": "Rolled out checkout SDK 8.5.0 to 100% of Android traffic",
        "changed_by": "deploy_bot",
    },
    recommended_action="rollback_change",
    fallback_healthy=True,
)

GATEWAY_LATENCY_SPIKE = Scenario(
    scenario_id="SCN-GW-LATENCY",
    name="Gateway latency spike causing timeouts",
    description=(
        "Gateway latency climbs until requests exceed the timeout budget. Failures present as "
        "timeouts across every method on that gateway, not as declines."
    ),
    root_cause_id="gateway_degradation",
    start_offset_s=0,
    duration_s=1800,
    effects=[
        Effect(
            match={"gateway": "gw_secondary"},
            success_multiplier=0.48,
            error_code="GATEWAY_TIMEOUT",
            latency_multiplier=3.4,
            latency_add_ms=4200,
        )
    ],
    recommended_action="shift_traffic",
    fallback_healthy=True,
    from_route="route_C",
    to_route="route_A",
)

TRAFFIC_MIX_SHIFT = Scenario(
    scenario_id="SCN-TRAFFIC-MIX",
    name="Traffic composition change (no infrastructure fault)",
    description=(
        "A campaign drives a surge of low-value wallet and netbanking traffic that converts worse "
        "at baseline. Headline success rate falls but nothing is broken — the correct response is "
        "to take no corrective routing action. This is the system's false-positive test."
    ),
    root_cause_id="traffic_mix_shift",
    start_offset_s=0,
    duration_s=1800,
    effects=[],
    traffic_mix_shift={
        "payment_method": {"upi": 0.30, "card": 0.14, "netbanking": 0.26, "wallet": 0.27, "emi": 0.03}
    },
    recommended_action="no_action",
    fallback_healthy=True,
)

MULTI_FACTOR = Scenario(
    scenario_id="SCN-MULTI",
    name="Multi-factor degradation",
    description=(
        "A moderate PSP degradation and an unrelated issuer wobble overlap. Neither alone explains "
        "the headline drop; the diagnosis should be explicitly ambiguous."
    ),
    root_cause_id="multi_factor",
    start_offset_s=0,
    duration_s=2100,
    effects=[
        Effect(
            match={"payment_method": "upi", "psp": "psp_yes"},
            success_multiplier=0.62,
            error_code="PSP_UNAVAILABLE",
        ),
        Effect(
            match={"payment_method": "card", "issuer": "ICICI"},
            success_multiplier=0.66,
            error_code="ISSUER_DECLINE",
        ),
    ],
    recommended_action="escalate",
    fallback_healthy=True,
)

# The failed-intervention demo: shifting away from route_A cannot help because route_B is
# degraded too. Verification must catch this and force a rollback rather than declare success.
UPI_PSP_DEGRADATION_BAD_FALLBACK = Scenario(
    scenario_id="SCN-UPI-PSP-BADFALLBACK",
    name="UPI rails degraded network-wide, presenting as a single PSP",
    description=(
        "Every UPI PSP is degrading together - an upstream rails problem, not a provider problem. "
        "Random variation still makes one PSP look worst, so the evidence points at a reroute that "
        "cannot possibly help. This is the failed-intervention case: the system must act, measure "
        "no improvement against a concurrent control, roll back and escalate rather than declaring "
        "victory or thrashing between routes."
    ),
    root_cause_id="payment_method_degradation",
    start_offset_s=0,
    duration_s=2700,
    effects=[
        Effect(
            match={"payment_method": "upi", "psp": "psp_axis"},
            success_multiplier=0.42,
            error_code="PSP_UNAVAILABLE",
            latency_add_ms=1500,
        ),
        Effect(
            match={"payment_method": "upi", "psp": "psp_yes"},
            success_multiplier=0.46,
            error_code="PSP_UNAVAILABLE",
            latency_add_ms=1400,
        ),
        Effect(
            match={"payment_method": "upi", "psp": "psp_hdfc"},
            success_multiplier=0.44,
            error_code="PSP_UNAVAILABLE",
            latency_add_ms=1400,
        ),
    ],
    recommended_action="escalate",
    fallback_healthy=False,
    from_route="route_A",
    to_route="route_B",
)

GEO_DEGRADATION = Scenario(
    scenario_id="SCN-GEO",
    name="Region-specific netbanking failures",
    description="Netbanking failures concentrated in two states, pointing at a regional bank link.",
    root_cause_id="issuer_degradation",
    start_offset_s=0,
    duration_s=1500,
    effects=[
        Effect(
            match={"payment_method": "netbanking", "geography": ["WB", "OR"]},
            success_multiplier=0.38,
            error_code="BANK_UNAVAILABLE",
        )
    ],
    recommended_action="disable_payment_method",
    fallback_healthy=True,
)

HIGH_VALUE_DEGRADATION = Scenario(
    scenario_id="SCN-HIGHVALUE",
    name="High-value transaction failures",
    description=(
        "Large-ticket card payments fail risk checks after a rule change. Transaction count barely "
        "moves but revenue at risk is severe — impact estimation, not detection, is the test here."
    ),
    root_cause_id="config_regression",
    start_offset_s=0,
    duration_s=1800,
    effects=[
        Effect(
            match={"payment_method": "card", "amount_band": "high"},
            success_multiplier=0.35,
            error_code="RISK_RULE_DECLINE",
        )
    ],
    config_change={
        "component": "risk_rules",
        "description": "Tightened velocity rule threshold for card payments above INR 10,000",
        "changed_by": "risk_ops",
    },
    recommended_action="rollback_change",
    fallback_healthy=True,
)

SCENARIOS: dict[str, Scenario] = {
    s.scenario_id: s
    for s in [
        UPI_PSP_DEGRADATION,
        ISSUER_DEGRADATION,
        ANDROID_CHECKOUT_REGRESSION,
        GATEWAY_LATENCY_SPIKE,
        TRAFFIC_MIX_SHIFT,
        MULTI_FACTOR,
        UPI_PSP_DEGRADATION_BAD_FALLBACK,
        GEO_DEGRADATION,
        HIGH_VALUE_DEGRADATION,
    ]
}


def get_scenario(scenario_id: str) -> Scenario:
    if scenario_id not in SCENARIOS:
        raise KeyError(f"unknown scenario {scenario_id!r}; known: {sorted(SCENARIOS)}")
    return SCENARIOS[scenario_id]
