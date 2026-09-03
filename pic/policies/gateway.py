"""Policy / guardrail gateway.

The only path from a proposed action to an executed one. Deterministic Python with no model
anywhere in it (ADR-002) — if an LLM could talk its way past this, every other guardrail in the
system would be decoration.

Design rules:

* **Most restrictive outcome wins.** Every rule is evaluated, not short-circuited, so the decision
  records every constraint that applied rather than just the first one hit. An operator reading the
  audit log needs to know all of it.
* **Clamp before refusing.** A proposal that overshoots a numeric limit is reduced to the limit
  rather than rejected. Refusing outright would leave a live incident unmitigated because the agent
  was slightly too ambitious.
* **Unknown means no.** An action absent from the merchant's policy is denied. New capabilities
  have to be granted explicitly; they are never inherited by default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from ..config import settings
from ..schemas import (
    ActionProposal,
    ActionType,
    AnomalySignal,
    PolicyDecision,
    PolicyOutcome,
    RootCauseAssessment,
)

# Ordered most-restrictive first, so `min` over this ranking picks the binding outcome.
_SEVERITY_ORDER = {
    PolicyOutcome.DENY: 0,
    PolicyOutcome.REQUIRE_APPROVAL: 1,
    PolicyOutcome.APPROVE_WITH_CLAMP: 2,
    PolicyOutcome.APPROVE: 3,
}


@dataclass
class RuleResult:
    rule: str
    outcome: PolicyOutcome
    reason: str
    clamped: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "clamped": self.clamped or None,
        }


@dataclass
class InterventionHistory:
    """What the agent has already done, so rate limits and conflicts can be enforced."""

    executed_at: list[datetime] = field(default_factory=list)
    failed_at: dict[str, list[datetime]] = field(default_factory=dict)
    cumulative_shift_pct: dict[str, dict[str, float]] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)
    active_routes: dict[str, set[str]] = field(default_factory=dict)

    def record_execution(
        self, incident_id: str, at: datetime, action: ActionType, params: dict[str, Any]
    ) -> None:
        self.executed_at.append(at)
        self.attempts[incident_id] = self.attempts.get(incident_id, 0) + 1
        if action is ActionType.SHIFT_TRAFFIC:
            src = str(params.get("from_route"))
            shifted = self.cumulative_shift_pct.setdefault(incident_id, {})
            shifted[src] = shifted.get(src, 0.0) + float(
                params.get("percentage", 0)
            )
            routes = self.active_routes.setdefault(incident_id, set())
            routes.add(src)
            routes.add(str(params.get("to_route")))

    def record_failure(self, incident_id: str, at: datetime) -> None:
        self.failed_at.setdefault(incident_id, []).append(at)

    def actions_in_last_hour(self, now: datetime) -> int:
        # Drop what has aged out while counting. Only the last hour is ever consulted, so a
        # process that stays up for days was keeping — and rescanning — every timestamp it had
        # ever recorded to answer a question about the last sixty minutes.
        cutoff = now - timedelta(hours=1)
        self.executed_at = [t for t in self.executed_at if t >= cutoff]
        return len(self.executed_at)

    def seconds_since_failure(self, incident_id: str, now: datetime) -> float | None:
        failures = self.failed_at.get(incident_id, [])
        if not failures:
            return None
        return (now - max(failures)).total_seconds()

    def attempts_for(self, incident_id: str) -> int:
        return self.attempts.get(incident_id, 0)

    def cumulative_shift_from(self, incident_id: str, route: str) -> float:
        return self.cumulative_shift_pct.get(incident_id, {}).get(route, 0.0)

    def clear(self) -> None:
        """Forget every intervention. Used when a demo world is reset to a clean baseline.

        The per-incident maps are not pruned when an incident closes, and must not be: `retry`
        and `override` re-enter the pipeline under the same incident id, and the attempt cap and
        cooling-off period have to still be counting when they do.
        """
        self.executed_at.clear()
        self.failed_at.clear()
        self.cumulative_shift_pct.clear()
        self.attempts.clear()
        self.active_routes.clear()


class PolicyGateway:
    def __init__(self, policy_path: Path | None = None, policy: dict[str, Any] | None = None) -> None:
        if policy is not None:
            self.policy = policy
        else:
            path = policy_path or settings.policy_file
            self.policy = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        self.history = InterventionHistory()

    # ------------------------------------------------------------------ api

    @property
    def bounds(self) -> dict[str, Any]:
        return self.policy.get("bounds", {})

    @property
    def thresholds(self) -> dict[str, Any]:
        return self.policy.get("thresholds", {})

    @property
    def rate_limits(self) -> dict[str, Any]:
        return self.policy.get("rate_limits", {})

    def evaluate(
        self,
        proposal: ActionProposal,
        *,
        now: datetime,
        anomaly: AnomalySignal | None = None,
        root_cause: RootCauseAssessment | None = None,
        route_health: dict[str, float] | None = None,
    ) -> PolicyDecision:
        granted = dict(proposal.parameters)
        results: list[RuleResult] = []

        results.append(self._rule_capability(proposal))
        results.append(self._rule_autonomy(proposal))
        results.append(self._rule_reversibility(proposal))
        results.extend(self._rule_bounds(proposal, granted))
        results.append(self._rule_blast_radius(anomaly))
        results.append(self._rule_confidence(anomaly, root_cause))
        results.append(self._rule_expected_value(proposal))
        results.append(self._rule_risk(proposal))
        results.extend(self._rule_rate_limits(proposal.incident_id, now))
        results.append(self._rule_routing(proposal, granted, route_health))

        binding = min(results, key=lambda r: _SEVERITY_ORDER[r.outcome])
        outcome = binding.outcome
        # A clamp applied by one rule still counts even when another rule is more restrictive.
        if outcome is PolicyOutcome.APPROVE and any(r.clamped for r in results):
            outcome = PolicyOutcome.APPROVE_WITH_CLAMP

        approved = outcome in (PolicyOutcome.APPROVE, PolicyOutcome.APPROVE_WITH_CLAMP)
        requires_human = outcome is PolicyOutcome.REQUIRE_APPROVAL
        bound_by = [r.rule for r in results if r.outcome is not PolicyOutcome.APPROVE]

        reasons = [r.reason for r in results if r.outcome is not PolicyOutcome.APPROVE]
        return PolicyDecision(
            incident_id=proposal.incident_id,
            action=proposal.action,
            requested_parameters=dict(proposal.parameters),
            granted_parameters=granted,
            outcome=outcome,
            approved=approved,
            bound_by=bound_by,
            reason="; ".join(reasons) if reasons else "within all merchant policy limits",
            evaluated_rules=[r.as_dict() for r in results],
            requires_human=requires_human,
            decided_at=now,
        )

    # ---------------------------------------------------------------- rules

    def _rule_capability(self, proposal: ActionProposal) -> RuleResult:
        allowed = set(self.policy.get("allowed_actions", []))
        if proposal.action.value not in allowed:
            return RuleResult(
                "capability:allowed_actions",
                PolicyOutcome.DENY,
                f"action {proposal.action.value!r} is not permitted by merchant policy",
            )
        return RuleResult("capability:allowed_actions", PolicyOutcome.APPROVE, "action permitted")

    def _rule_autonomy(self, proposal: ActionProposal) -> RuleResult:
        autonomous = set(self.policy.get("autonomous_actions", []))
        if proposal.action.value not in autonomous:
            return RuleResult(
                "autonomy:autonomous_actions",
                PolicyOutcome.REQUIRE_APPROVAL,
                f"action {proposal.action.value!r} requires human approval by merchant policy",
            )
        return RuleResult("autonomy:autonomous_actions", PolicyOutcome.APPROVE, "autonomous action")

    def _rule_reversibility(self, proposal: ActionProposal) -> RuleResult:
        irreversible = set(self.policy.get("irreversible_actions", []))
        if proposal.action.value in irreversible or not proposal.reversible:
            return RuleResult(
                "reversibility:irreversible_requires_approval",
                PolicyOutcome.REQUIRE_APPROVAL,
                "action cannot be automatically undone, so a human must approve it",
            )
        return RuleResult(
            "reversibility:irreversible_requires_approval", PolicyOutcome.APPROVE, "reversible"
        )

    def _rule_bounds(self, proposal: ActionProposal, granted: dict[str, Any]) -> list[RuleResult]:
        out: list[RuleResult] = []
        bounds = self.bounds

        if proposal.action is ActionType.SHIFT_TRAFFIC:
            requested = float(granted.get("percentage", 0) or 0)
            limit = float(bounds.get("max_traffic_shift_pct", 100))
            if requested > limit:
                granted["percentage"] = limit
                out.append(
                    RuleResult(
                        "bound:max_traffic_shift_pct",
                        PolicyOutcome.APPROVE_WITH_CLAMP,
                        f"requested {requested:g}% exceeds merchant limit of {limit:g}%",
                        {"percentage": limit},
                    )
                )
            else:
                out.append(
                    RuleResult("bound:max_traffic_shift_pct", PolicyOutcome.APPROVE, "within limit")
                )

            source = str(granted.get("from_route"))
            already = self.history.cumulative_shift_from(proposal.incident_id, source)
            cumulative_limit = float(bounds.get("max_cumulative_traffic_shift_pct", 100))
            projected = already + float(granted.get("percentage", 0) or 0)
            if projected > cumulative_limit:
                headroom = max(0.0, cumulative_limit - already)
                if headroom <= 0:
                    out.append(
                        RuleResult(
                            "bound:max_cumulative_traffic_shift_pct",
                            PolicyOutcome.DENY,
                            f"route {source} has already been shifted by {already:g}%, "
                            f"at the cumulative limit of {cumulative_limit:g}%",
                        )
                    )
                else:
                    granted["percentage"] = headroom
                    out.append(
                        RuleResult(
                            "bound:max_cumulative_traffic_shift_pct",
                            PolicyOutcome.APPROVE_WITH_CLAMP,
                            f"only {headroom:g}% of cumulative shift headroom remains on {source}",
                            {"percentage": headroom},
                        )
                    )

        elif proposal.action is ActionType.CONFIGURE_RETRY:
            requested = int(granted.get("max_retries", 0) or 0)
            limit = int(bounds.get("max_retries", 3))
            if requested > limit:
                granted["max_retries"] = limit
                out.append(
                    RuleResult(
                        "bound:max_retries",
                        PolicyOutcome.APPROVE_WITH_CLAMP,
                        f"requested {requested} retries exceeds merchant limit of {limit}",
                        {"max_retries": limit},
                    )
                )

        elif proposal.action is ActionType.SET_MONITORING_FREQUENCY:
            requested = int(granted.get("interval_seconds", 120) or 120)
            lo = int(bounds.get("min_monitoring_interval_seconds", 30))
            hi = int(bounds.get("max_monitoring_interval_seconds", 600))
            clamped = max(lo, min(hi, requested))
            if clamped != requested:
                granted["interval_seconds"] = clamped
                out.append(
                    RuleResult(
                        "bound:monitoring_interval",
                        PolicyOutcome.APPROVE_WITH_CLAMP,
                        f"monitoring interval clamped to the permitted range {lo}-{hi}s",
                        {"interval_seconds": clamped},
                    )
                )

        if not out:
            out.append(RuleResult("bound:none_applicable", PolicyOutcome.APPROVE, "no bounds apply"))
        return out

    def _rule_blast_radius(self, anomaly: AnomalySignal | None) -> RuleResult:
        limit = self.thresholds.get("human_approval_revenue_at_risk_paise")
        if anomaly and limit and anomaly.estimated_revenue_at_risk_paise >= float(limit):
            return RuleResult(
                "blast_radius:human_approval_revenue_at_risk",
                PolicyOutcome.REQUIRE_APPROVAL,
                (
                    f"revenue at risk of INR {anomaly.estimated_revenue_at_risk_paise / 100:,.0f}/hr "
                    f"exceeds the autonomous ceiling of INR {float(limit) / 100:,.0f}/hr"
                ),
            )
        return RuleResult(
            "blast_radius:human_approval_revenue_at_risk", PolicyOutcome.APPROVE, "within ceiling"
        )

    def _rule_confidence(
        self, anomaly: AnomalySignal | None, root_cause: RootCauseAssessment | None
    ) -> RuleResult:
        min_detection = float(self.thresholds.get("min_detection_confidence", 0.0))
        min_cause = float(self.thresholds.get("min_confidence_for_autonomous_action", 0.0))

        if anomaly and anomaly.confidence < min_detection:
            return RuleResult(
                "confidence:min_detection_confidence",
                PolicyOutcome.DENY,
                f"detection confidence {anomaly.confidence:.2f} is below the floor {min_detection:.2f}",
            )
        if root_cause and root_cause.confidence < min_cause:
            return RuleResult(
                "confidence:min_confidence_for_autonomous_action",
                PolicyOutcome.REQUIRE_APPROVAL,
                (
                    f"root-cause confidence {root_cause.confidence:.2f} is below the autonomous "
                    f"floor {min_cause:.2f}"
                ),
            )
        if root_cause and root_cause.ambiguous:
            return RuleResult(
                "confidence:ambiguous_diagnosis",
                PolicyOutcome.REQUIRE_APPROVAL,
                "diagnosis is ambiguous: competing hypotheses are too close to separate",
            )
        return RuleResult("confidence:thresholds", PolicyOutcome.APPROVE, "confidence sufficient")

    def _rule_expected_value(self, proposal: ActionProposal) -> RuleResult:
        if proposal.action is ActionType.NO_ACTION:
            return RuleResult("expected_value:min", PolicyOutcome.APPROVE, "no action proposed")
        floor = float(self.thresholds.get("min_expected_value_paise", 0))
        if proposal.expected_value_paise <= floor:
            return RuleResult(
                "expected_value:min",
                PolicyOutcome.DENY,
                (
                    f"expected value of INR {proposal.expected_value_paise / 100:,.0f} does not "
                    "justify the intervention"
                ),
            )
        return RuleResult("expected_value:min", PolicyOutcome.APPROVE, "positive expected value")

    def _rule_risk(self, proposal: ActionProposal) -> RuleResult:
        limit = float(self.thresholds.get("max_autonomous_risk_score", 1.0))
        if proposal.risk_score > limit:
            return RuleResult(
                "risk:max_autonomous_risk_score",
                PolicyOutcome.REQUIRE_APPROVAL,
                f"risk score {proposal.risk_score:.2f} exceeds the autonomous limit {limit:.2f}",
            )
        return RuleResult("risk:max_autonomous_risk_score", PolicyOutcome.APPROVE, "acceptable risk")

    def _rule_rate_limits(self, incident_id: str, now: datetime) -> list[RuleResult]:
        out: list[RuleResult] = []
        limits = self.rate_limits

        per_hour = int(limits.get("max_autonomous_actions_per_hour", 999))
        used = self.history.actions_in_last_hour(now)
        if used >= per_hour:
            out.append(
                RuleResult(
                    "rate_limit:max_autonomous_actions_per_hour",
                    PolicyOutcome.REQUIRE_APPROVAL,
                    f"{used} autonomous actions already executed in the last hour (limit {per_hour})",
                )
            )

        cooloff = float(limits.get("cooloff_after_failed_intervention_seconds", 0))
        since = self.history.seconds_since_failure(incident_id, now)
        if cooloff and since is not None and since < cooloff:
            out.append(
                RuleResult(
                    "rate_limit:cooloff_after_failed_intervention",
                    PolicyOutcome.REQUIRE_APPROVAL,
                    (
                        f"only {since:.0f}s since the last failed intervention; "
                        f"cooloff is {cooloff:.0f}s"
                    ),
                )
            )

        max_attempts = int(limits.get("max_intervention_attempts", 2))
        attempts = self.history.attempts_for(incident_id)
        if attempts >= max_attempts:
            out.append(
                RuleResult(
                    "rate_limit:max_intervention_attempts",
                    PolicyOutcome.DENY,
                    f"{attempts} interventions already attempted for this incident "
                    f"(limit {max_attempts})",
                )
            )

        if not out:
            out.append(
                RuleResult("rate_limit:none_binding", PolicyOutcome.APPROVE, "within rate limits")
            )
        return out

    def _rule_routing(
        self,
        proposal: ActionProposal,
        granted: dict[str, Any],
        route_health: dict[str, float] | None,
    ) -> RuleResult:
        if proposal.action is not ActionType.SHIFT_TRAFFIC:
            return RuleResult("routing:eligible_routes", PolicyOutcome.APPROVE, "not a routing action")

        routing = self.policy.get("routing", {})
        eligible = set(routing.get("eligible_routes", []))
        destination = str(granted.get("to_route"))
        if eligible and destination not in eligible:
            return RuleResult(
                "routing:eligible_routes",
                PolicyOutcome.DENY,
                f"route {destination!r} is not an approved destination",
            )

        # Never move traffic onto a route that is itself unhealthy. This is the rule that stops the
        # failed-intervention scenario from being made worse by a second shift onto a bad route.
        floor = float(routing.get("min_destination_success_rate", 0.0))
        if route_health and destination in route_health and route_health[destination] < floor:
            return RuleResult(
                "routing:min_destination_success_rate",
                PolicyOutcome.DENY,
                (
                    f"destination route {destination} is at "
                    f"{route_health[destination]:.1%} success rate, below the required {floor:.0%}"
                ),
            )
        return RuleResult("routing:eligible_routes", PolicyOutcome.APPROVE, "destination permitted")
