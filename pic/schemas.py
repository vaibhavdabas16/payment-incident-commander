"""Typed contracts for the entire system.

Every agent boundary, tool result and persisted artefact is one of these models.
If it is not in this file, it is not part of an agent contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class IncidentState(str, Enum):
    """Canonical FSM states. See docs/ARCHITECTURE.md section 3."""

    OBSERVING = "OBSERVING"
    DETECTED = "DETECTED"
    INVESTIGATING = "INVESTIGATING"
    IMPACT_ASSESSED = "IMPACT_ASSESSED"
    DIAGNOSING = "DIAGNOSING"
    RECOVERY_PLANNING = "RECOVERY_PLANNING"
    DECIDING = "DECIDING"
    POLICY_REVIEW = "POLICY_REVIEW"
    AWAITING_HUMAN_APPROVAL = "AWAITING_HUMAN_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    ROLLING_BACK = "ROLLING_BACK"
    RESOLVED = "RESOLVED"
    RECOVERING_ORDERS = "RECOVERING_ORDERS"
    ESCALATED = "ESCALATED"
    LEARNING = "LEARNING"
    CLOSED = "CLOSED"


TERMINAL_STATES = {IncidentState.CLOSED}


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ActionType(str, Enum):
    """Closed catalogue. The LLM may only propose a member of this enum."""

    SHIFT_TRAFFIC = "shift_traffic"
    DISABLE_PAYMENT_METHOD = "disable_payment_method"
    CONFIGURE_RETRY = "configure_retry"
    ROLLBACK_CHANGE = "rollback_change"
    # The inverse of ROLLBACK_CHANGE. Never proposed by the reasoner - it exists so that reverting
    # a configuration rollback is expressible, which is what makes that action reversible like
    # every other write.
    RESTORE_CHANGE = "restore_change"
    SET_MONITORING_FREQUENCY = "set_monitoring_frequency"
    # Second recovery layer: payments that already failed during the incident and can still be
    # rescued once the infrastructure fault is gone. A write like any other - policy-gated,
    # audited, and reversible via CANCEL_ORDER_RECOVERY.
    RECOVER_FAILED_PAYMENTS = "recover_failed_payments"
    # The inverse of RECOVER_FAILED_PAYMENTS. Never proposed by the reasoner; it exists so the
    # order-recovery campaign is reversible like every other write.
    CANCEL_ORDER_RECOVERY = "cancel_order_recovery"
    NOTIFY_MERCHANT = "notify_merchant"
    CREATE_INCIDENT_TICKET = "create_incident_ticket"
    NO_ACTION = "no_action"


class PolicyOutcome(str, Enum):
    APPROVE = "APPROVE"
    APPROVE_WITH_CLAMP = "APPROVE_WITH_CLAMP"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


class VerificationStatus(str, Enum):
    RECOVERED = "RECOVERED"
    PARTIALLY_RECOVERED = "PARTIALLY_RECOVERED"
    FAILED = "FAILED"
    REGRESSED = "REGRESSED"
    INCONCLUSIVE = "INCONCLUSIVE"


# --------------------------------------------------------------------------
# Payment events
# --------------------------------------------------------------------------


@dataclass(slots=True)
class PaymentEvent:
    """One payment attempt. Money is always integer paise (ADR-008).

    A plain slotted dataclass rather than a pydantic model, because there are a great many of
    these and they never cross a trust boundary: the simulator is the only thing that constructs
    one, nothing serialises them, and every consumer reads them by attribute. As a model each
    instance carried a `__dict__` and a `__pydantic_fields_set__` — a set of all twenty-one field
    names, 2,264 bytes on its own — for about 3.2KB an event. At the demo speed-up that is tens of
    megabytes an hour of pure overhead, and it is what made a per-visitor simulation unaffordable.

    Validation is not lost, only moved: the one invariant this type ever enforced is checked in
    `__post_init__` and still raises on construction.
    """

    payment_id: str
    order_id: str
    timestamp: datetime
    merchant_id: str
    amount_paise: int
    payment_method: str
    gateway: str
    psp: str
    issuer: str
    geography: str
    device: str
    os: str
    app_version: str
    status: Literal["success", "failed"]
    latency_ms: int
    route_id: str
    network: str | None = None
    error_code: str | None = None
    retry_count: int = 0
    is_retry: bool = False
    # Derived at generation time so order-value-specific failures are sliceable like any other
    # dimension. The spec calls these out explicitly and they are invisible without it.
    amount_band: str = "mid"

    def __post_init__(self) -> None:
        if self.amount_paise <= 0:
            raise ValueError("amount_paise must be positive")


class ConfigChange(BaseModel):
    """A merchant-side change, discoverable as investigation evidence."""

    change_id: str
    timestamp: datetime
    merchant_id: str
    component: str
    description: str
    changed_by: str
    reversible: bool = True


# --------------------------------------------------------------------------
# Segments and metric windows
# --------------------------------------------------------------------------


class Segment(BaseModel):
    """A slice of traffic, e.g. {"payment_method": "upi", "psp": "psp_axis"}."""

    dimensions: dict[str, str]

    def key(self) -> str:
        return "&".join(f"{k}={v}" for k, v in sorted(self.dimensions.items()))

    def label(self) -> str:
        return " · ".join(f"{k}={v}" for k, v in sorted(self.dimensions.items()))


class SegmentStat(BaseModel):
    """Observed performance of one segment inside a window."""

    segment: Segment
    total: int
    successes: int
    failures: int
    success_rate: float
    baseline_success_rate: float | None = None
    deviation: float | None = None
    # Baseline counts, kept so a segment drop can be significance-tested rather than eyeballed.
    baseline_total: int = 0
    baseline_successes: int = 0
    p_value: float = 1.0
    # Share of all failures in the window that fall in this segment.
    failure_share: float = 0.0
    # Share of all traffic in the window that this segment represents.
    traffic_share: float = 0.0
    # Wilson lower bound on the failure rate: guards against thin-volume noise.
    failure_rate_lower_bound: float = 0.0
    amount_at_risk_paise: int = 0


class MetricWindow(BaseModel):
    """Aggregate performance over a time window."""

    start: datetime
    end: datetime
    total: int
    successes: int
    failures: int
    success_rate: float
    gmv_paise: int
    failed_gmv_paise: int
    p95_latency_ms: float = 0.0
    error_distribution: dict[str, int] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Agent outputs
# --------------------------------------------------------------------------


class AnomalySignal(BaseModel):
    """Detection Agent output. Purely deterministic (ADR-001)."""

    incident_id: str
    detected_at: datetime
    severity: Severity
    metric: str = "payment_success_rate"
    current_value: float
    baseline: float
    deviation: float
    confidence: float
    z_score: float
    change_point_detected: bool = False
    sample_size: int
    affected_segments: list[SegmentStat] = Field(default_factory=list)
    estimated_revenue_at_risk_paise: int = 0
    detection_method: list[str] = Field(default_factory=list)
    window_start: datetime
    window_end: datetime


class Finding(BaseModel):
    """One atomic piece of evidence, always traceable to the tool that produced it."""

    finding_id: str
    source_tool: str
    dimension: str
    statement: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    # 0..1 — how strongly this discriminates between hypotheses.
    strength: float = 0.5


class EvidenceBundle(BaseModel):
    """Investigation Agent output."""

    incident_id: str
    findings: list[Finding] = Field(default_factory=list)
    correlated_signals: list[str] = Field(default_factory=list)
    top_segments: list[SegmentStat] = Field(default_factory=list)
    error_distribution: dict[str, int] = Field(default_factory=dict)
    baseline_error_distribution: dict[str, int] = Field(default_factory=dict)
    dominant_error_code: str | None = None
    dominant_error_share: float = 0.0
    # The segment judged to be the primary fault; other segments are tested against it for
    # independence so echoes are not mistaken for concurrent faults.
    primary_segment: dict[str, str] = Field(default_factory=dict)
    latency_shift_ms: float = 0.0
    recent_config_changes: list[ConfigChange] = Field(default_factory=list)
    traffic_composition_shift: dict[str, float] = Field(default_factory=dict)
    similar_past_incidents: list[dict[str, Any]] = Field(default_factory=list)
    degraded: bool = False
    tools_used: list[str] = Field(default_factory=list)

    def finding_ids(self) -> set[str]:
        return {f.finding_id for f in self.findings}


class ImpactAssessment(BaseModel):
    """Business Impact Agent output. Every number carries its derivation."""

    incident_id: str
    revenue_at_risk_per_hour_paise: int
    transactions_at_risk_per_hour: int
    affected_customers_estimate: int
    affected_gmv_paise: int
    projected_loss_if_unmitigated_paise: int
    projection_horizon_minutes: int
    # Human-readable arithmetic, shown in the UI. Never fabricated.
    calculation: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class Hypothesis(BaseModel):
    cause_id: str
    cause: str
    probability: float
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)
    deterministic_score: float = 0.0
    memory_adjustment: float = 0.0
    reasoning: str = ""


class RootCauseAssessment(BaseModel):
    """Root Cause Agent output."""

    incident_id: str
    most_likely_root_cause: str
    cause_id: str
    confidence: float
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    supporting_evidence: list[str] = Field(default_factory=list)
    contradicting_evidence: list[str] = Field(default_factory=list)
    ambiguous: bool = False
    reasoner: str = "deterministic"
    narrative: str = ""


class ActionProposal(BaseModel):
    """Decision Agent output. Not yet authorised."""

    incident_id: str
    action: ActionType
    parameters: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    expected_revenue_protected_per_hour_paise: int = 0
    expected_cost_paise: int = 0
    risk_score: float = 0.0
    confidence: float = 0.0
    reversible: bool = True
    expected_value_paise: int = 0
    alternatives_considered: list[dict[str, Any]] = Field(default_factory=list)
    proposer: str = "deterministic"


class PolicyDecision(BaseModel):
    """Policy Gateway output. The only thing that can authorise execution."""

    incident_id: str
    action: ActionType
    requested_parameters: dict[str, Any] = Field(default_factory=dict)
    granted_parameters: dict[str, Any] = Field(default_factory=dict)
    outcome: PolicyOutcome
    approved: bool
    approved_by: str = "policy_engine"
    bound_by: list[str] = Field(default_factory=list)
    reason: str = ""
    evaluated_rules: list[dict[str, Any]] = Field(default_factory=list)
    requires_human: bool = False
    decided_at: datetime = Field(default_factory=utcnow)


class ActionResult(BaseModel):
    """Action Agent output."""

    incident_id: str
    action: ActionType
    parameters: dict[str, Any] = Field(default_factory=dict)
    executed: bool
    success: bool
    adapter: str = "simulator"
    result_detail: dict[str, Any] = Field(default_factory=dict)
    inverse_action: dict[str, Any] | None = None
    error: str | None = None
    executed_at: datetime = Field(default_factory=utcnow)


class AuditRecord(BaseModel):
    """Immutable record of a side effect."""

    audit_id: str
    timestamp: datetime
    incident_id: str
    action: str
    parameters: dict[str, Any]
    # What the gateway granted for *this* execution. Recorded alongside what was executed so the
    # two can be compared per record. An incident can make several attempts under several
    # decisions, and checking an early execution against the incident's final decision reports a
    # violation whenever a later attempt was clamped - a bug in the metric being read as a bug in
    # the agent.
    granted_parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str
    approved_by: str
    policy_outcome: str
    execution_result: str
    adapter: str
    reversible: bool
    inverse_action: dict[str, Any] | None = None


class VerificationResult(BaseModel):
    """Verification Agent output. Statistical, not a two-number comparison (ADR-009)."""

    incident_id: str
    status: VerificationStatus
    before_success_rate: float
    after_success_rate: float
    improvement: float
    baseline_success_rate: float
    recovery_ratio: float = 0.0
    p_value: float = 1.0
    statistically_significant: bool = False
    before_sample: int = 0
    after_sample: int = 0
    # Concurrent control comparison, when the action creates a natural control group (traffic left
    # on the old route). Immune to the incident worsening on its own, unlike before/after.
    control_used: bool = False
    treated_success_rate: float | None = None
    control_success_rate: float | None = None
    treated_sample: int = 0
    control_sample: int = 0
    caused_harm: bool = False
    estimated_revenue_protected_per_hour_paise: int = 0
    side_effects: list[str] = Field(default_factory=list)
    rollback_recommended: bool = False
    explanation: str = ""


# Actions that inform or observe but never change payment behaviour, so there is nothing for the
# Verification Agent to measure and nothing for an operator to gain by choosing one over another.
# Lives here rather than in the supervisor so the escalation agent can use it without importing
# the thing that imports it.
NON_REMEDIAL_ACTIONS = {
    ActionType.NOTIFY_MERCHANT,
    ActionType.CREATE_INCIDENT_TICKET,
    ActionType.SET_MONITORING_FREQUENCY,
    ActionType.NO_ACTION,
}


# --------------------------------------------------------------------------
# Closed-loop learning contracts
#
# Everything below exists so the system can answer, deterministically, "what happened last time
# something like this broke, and what did we do about it?". These are typed records rather than
# prose: the reasoner may explain them, but it can neither produce nor edit them, which is what
# makes historical evidence checkable instead of recalled (ADR-010).
# --------------------------------------------------------------------------


class FailureSignature(BaseModel):
    """Stable, structured identity of *what kind of failure* this is.

    Retrieval matches on these dimensions rather than on free text. Two incidents that agree on
    provider and error code are the same kind of problem even when their prose differs; two that
    share a paragraph of narrative but disagree on both are not.
    """

    merchant_id: str = ""
    payment_method: str | None = None
    psp: str | None = None
    issuer: str | None = None
    gateway: str | None = None
    route_id: str | None = None
    geography: str | None = None
    dominant_error_code: str | None = None
    root_cause_id: str = ""
    severity: Severity = Severity.LOW
    # Absolute success-rate deviation from baseline: how badly it broke, not merely that it did.
    degradation_magnitude: float = 0.0
    latency_shift_ms: float = 0.0
    # Share of total traffic sitting in the affected segments - the blast radius.
    traffic_share: float = 0.0
    affected_segment_keys: list[str] = Field(default_factory=list)
    # Route success rates measured at decision time, so "was a healthy destination available?" is
    # part of what makes two incidents comparable.
    route_health: dict[str, float] = Field(default_factory=dict)

    def key(self) -> str:
        parts = [
            self.merchant_id,
            self.root_cause_id,
            self.payment_method or "-",
            self.psp or "-",
            self.dominant_error_code or "-",
        ]
        return "|".join(parts)

    def label(self) -> str:
        bits = [
            b
            for b in (
                self.payment_method,
                self.psp or self.issuer or self.gateway,
                self.dominant_error_code,
            )
            if b
        ]
        return " · ".join(bits) or self.root_cause_id or "unclassified"


class ActionOutcomeStats(BaseModel):
    """How one action, at one magnitude band, has actually performed historically."""

    action: str
    magnitude_band: str = "any"
    attempts: int = 0
    successes: int = 0
    partials: int = 0
    failures: int = 0
    rollbacks: int = 0
    # Full recoveries only. This is the number reported to a human, because "it worked" should
    # mean the incident was over.
    success_rate: float = 0.0
    # Recoveries and partial recoveries together: the times the action moved the number in the
    # right direction without having to be undone. This is the quantity that belongs in an
    # efficacy prior — "will this fix help?" is a different question from "will it finish the
    # job?", and scoring a partial recovery as a failure teaches the system that a working
    # intervention does not work.
    helped: int = 0
    helped_rate: float = 0.0
    median_recovery_time_s: float | None = None
    median_revenue_protected_paise: int = 0
    # Incident ids behind the counts, so every claim can be opened and checked.
    incident_ids: list[str] = Field(default_factory=list)

    def summary(self) -> str:
        return f"{self.action} at {self.magnitude_band}: {self.successes}/{self.attempts} succeeded"


class HistoricalSupport(BaseModel):
    """The evidence from memory attached to one candidate strategy.

    `advisory` is not decoration. It is the guarantee that nothing here authorises anything: this
    object adjusts an efficacy prior inside a bounded range and is rendered to a human, and that
    is the whole of its power.
    """

    matched_incidents: int = 0
    stats: ActionOutcomeStats | None = None
    # Whether the statistics are about this magnitude specifically, or about the action across
    # every size it has been tried at. The difference matters when the evidence is being quoted:
    # "20% worked nine times" and "rerouting worked nine times" are different claims.
    magnitude_matched: bool = False
    # Signed, bounded adjustment to the efficacy prior, within +/- MAX_HISTORY_ADJUSTMENT.
    efficacy_adjustment: float = 0.0
    recommendation: str = ""
    evidence: list[str] = Field(default_factory=list)
    advisory: bool = True


class RecoveryStrategy(BaseModel):
    """One priced candidate way out of the incident. Not authorised, not chosen."""

    strategy_id: str
    action: ActionType
    target: str = ""
    # Points of traffic this option would move, and nothing else. `None` for every action that
    # does not move traffic - a retry count and a monitoring interval are not magnitudes of the
    # same thing, and letting them share this field renders "60%" for an interval in seconds and
    # buckets them into traffic-shift bands they have no business being in. Their own values live
    # in `parameters`, where they are labelled.
    magnitude: float | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    expected_revenue_protected_per_hour_paise: int = 0
    expected_cost_paise: int = 0
    expected_value_paise: int = 0
    confidence: float = 0.0
    risk_score: float = 0.0
    reversible: bool = True
    p_success: float = 0.0
    reasoning: str = ""
    supporting_findings: list[str] = Field(default_factory=list)
    historical_support: HistoricalSupport | None = None

    def as_catalogue_entry(self) -> dict[str, Any]:
        """The shape the reasoner and the Decision Agent consume."""
        return {
            "strategy_id": self.strategy_id,
            "action": self.action.value,
            "target": self.target,
            "magnitude": self.magnitude,
            "parameters": dict(self.parameters),
            "rationale": self.reasoning,
            "expected_revenue_protected_per_hour_paise": (
                self.expected_revenue_protected_per_hour_paise
            ),
            "expected_cost_paise": self.expected_cost_paise,
            "expected_value_paise": self.expected_value_paise,
            "risk_score": self.risk_score,
            "reversible": self.reversible,
            "p_success": self.p_success,
            "historical_support": (
                self.historical_support.model_dump(mode="json")
                if self.historical_support
                else None
            ),
        }


class RecoveryPlan(BaseModel):
    """Recovery Strategy Agent output: the closed set of options, with history attached."""

    incident_id: str
    signature: FailureSignature
    strategies: list[RecoveryStrategy] = Field(default_factory=list)
    similar_incidents: list[dict[str, Any]] = Field(default_factory=list)
    historical_recommendation: str = ""
    memory_size: int = 0
    # Set when the merchant profile shaped the option set (preferred magnitude, risk appetite).
    profile_applied: dict[str, Any] = Field(default_factory=dict)


class RecoverableOrder(BaseModel):
    """A payment that failed during the incident and might still be completable."""

    order_id: str
    payment_id: str
    amount_paise: int
    payment_method: str
    error_code: str | None = None
    failed_at: datetime
    # retry | alternate_route | payment_link - chosen from the failure, never invented.
    recovery_method: str = "retry"
    # Probability this order completes if attempted, from the observed recovery rate for this
    # error class. Used for sizing the campaign, never reported as revenue.
    recovery_probability: float = 0.0


class OrderRecoveryResult(BaseModel):
    """Outcome of the second recovery layer: what actually happened to failed payments."""

    incident_id: str
    failed_payments: int = 0
    recoverable_payments: int = 0
    attempted: int = 0
    recovered: int = 0
    recoverable_value_paise: int = 0
    recovered_value_paise: int = 0
    by_method: dict[str, int] = Field(default_factory=dict)
    campaign_id: str = ""
    executed: bool = False
    note: str = ""


class RevenueOutcome(BaseModel):
    """The financial story of one incident, in integer paise (ADR-008).

    The three money figures are deliberately disjoint so nothing is counted twice:

    * `revenue_at_risk_paise` - what the degradation threatened over the incident's own duration.
    * `revenue_protected_paise` - future loss the intervention prevented, measured against the
      concurrent control, for the time the intervention was actually in force.
    * `revenue_recovered_paise` - value of payments that had *already* failed and were then
      completed by the order-recovery layer.

    `revenue_lost_paise` is the remainder, and `recovery_rate` is (protected + recovered) / at
    risk. Every one of them is arithmetic over recorded measurements; none comes from a model.
    """

    incident_id: str
    revenue_at_risk_paise: int = 0
    revenue_at_risk_per_hour_paise: int = 0
    revenue_protected_paise: int = 0
    revenue_recovered_paise: int = 0
    revenue_lost_paise: int = 0
    recovery_rate: float = 0.0
    exposure_seconds: float = 0.0
    protected_seconds: float = 0.0
    # Human-checkable arithmetic, in the same spirit as ImpactAssessment.calculation.
    calculation: list[str] = Field(default_factory=list)
    measurable: bool = True


class PreventionRecommendation(BaseModel):
    """A recurring pattern the system has noticed, and what it would do about it.

    Advisory by construction. It carries no authority to change the merchant's policy; approving
    one records a request that a human applies to `merchant_policies.yaml` themselves.
    """

    recommendation_id: str
    merchant_id: str
    pattern: str
    signature_key: str = ""
    occurrences: int = 0
    incident_ids: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    proposed_action: ActionType = ActionType.SHIFT_TRAFFIC
    proposed_parameters: dict[str, Any] = Field(default_factory=dict)
    historical_revenue_lost_paise: int = 0
    estimated_benefit_paise: int = 0
    evidence: list[str] = Field(default_factory=list)
    requires_merchant_approval: bool = True
    status: str = "PROPOSED"  # PROPOSED | ACKNOWLEDGED | DISMISSED
    acknowledged_by: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class IncidentOutcomeRecord(BaseModel):
    """The structured record of one completed incident. The unit of the system's memory.

    Typed rather than free-form on purpose: every retrieval, success rate and prevention pattern
    is a deterministic query over these fields, so historical evidence can be recomputed and
    checked rather than recalled.
    """

    incident_id: str
    timestamp: datetime
    merchant_id: str
    failure_signature: FailureSignature
    affected_segments: list[dict[str, str]] = Field(default_factory=list)
    detected_metrics: dict[str, Any] = Field(default_factory=dict)

    root_cause_hypotheses: list[dict[str, Any]] = Field(default_factory=list)
    selected_root_cause: str = ""
    selected_root_cause_id: str = ""
    root_cause_confidence: float = 0.0

    revenue_at_risk_paise: int = 0
    revenue_at_risk_per_hour_paise: int = 0

    candidate_actions: list[dict[str, Any]] = Field(default_factory=list)
    selected_action: str = "none"
    selected_parameters: dict[str, Any] = Field(default_factory=dict)
    actual_action_executed: str = "none"
    executed_parameters: dict[str, Any] = Field(default_factory=dict)
    intervention_magnitude: float | None = None
    policy_result: str = ""
    policy_bound_by: list[str] = Field(default_factory=list)

    control_group_metrics: dict[str, Any] = Field(default_factory=dict)
    treatment_group_metrics: dict[str, Any] = Field(default_factory=dict)
    verification_result: str = "NOT_VERIFIED"
    verification_significant: bool = False

    revenue_protected_paise: int = 0
    revenue_recovered_paise: int = 0
    revenue_lost_paise: int = 0
    recovery_rate: float = 0.0
    order_recovery: OrderRecoveryResult | None = None

    rollback_required: bool = False
    rollback_result: str = "NOT_REQUIRED"  # NOT_REQUIRED | SUCCEEDED | FAILED
    time_to_recovery_s: float | None = None
    time_to_mitigate_s: float | None = None
    human_intervention_required: bool = False
    final_resolution: str = "UNKNOWN"
    false_positive: bool = False

    def succeeded(self) -> bool:
        """Whether the intervention on this incident actually worked.

        Deliberately strict: a partial recovery is not a success, and an incident that had to be
        rolled back is a failure however it was later resolved.
        """
        if self.rollback_required:
            return False
        return self.verification_result == "RECOVERED"

    def helped(self) -> bool:
        """Whether the intervention moved payments in the right direction at all.

        A partial recovery counts. It is a measured, control-verified improvement that did not
        finish the job — evidence that the action works, not evidence that it does not. Anything
        that had to be rolled back never counts, whatever it looked like beforehand.
        """
        if self.rollback_required:
            return False
        return self.verification_result in ("RECOVERED", "PARTIALLY_RECOVERED")

    def magnitude_band(self) -> str:
        return magnitude_band(self.intervention_magnitude)


def magnitude_band(magnitude: float | None) -> str:
    """Bucket an intervention size so outcomes at comparable magnitudes pool together.

    Bands rather than exact values: a 19% and a 20% shift are the same decision, and keeping them
    apart would leave every band with a sample size of one.
    """
    if magnitude is None:
        return "any"
    if magnitude <= 0:
        return "none"
    if magnitude <= 12:
        return "small (<=12%)"
    if magnitude <= 25:
        return "moderate (13-25%)"
    if magnitude <= 40:
        return "large (26-40%)"
    return "very large (>40%)"


class NextStep(BaseModel):
    """One thing a human can do about this incident, from inside the product.

    `action` is the operation to call; everything else is for the person deciding whether to.
    `consequence` is not decoration - an operator authorising a change to a live payment system is
    entitled to know what happens next before they click, particularly that an override is still
    measured and still reverted if it does not help.
    """

    action: str
    label: str
    detail: str
    consequence: str = ""
    destructive: bool = False


class Escalation(BaseModel):
    incident_id: str
    reason_code: str
    # The category, and the case. `reason` groups handovers; `because` is what actually happened
    # to this incident, in the numbers and names the operator will act on.
    reason: str
    because: str = ""
    urgency: Severity
    recommended_human_action: str
    # What can be done about it, here, now. An escalation with no next step is a dead end.
    next_steps: list[NextStep] = Field(default_factory=list)
    context_pack: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------
# Observability
# --------------------------------------------------------------------------


class ToolCallRecord(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    latency_ms: float = 0.0
    error: str | None = None
    result_summary: str = ""


class AgentStep(BaseModel):
    """One agent execution. The observability spine of the system."""

    step_id: str
    incident_id: str
    agent: str
    state: IncidentState
    started_at: datetime
    ended_at: datetime
    latency_ms: float
    ok: bool = True
    summary: str = ""
    output: dict[str, Any] = Field(default_factory=dict)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    error: str | None = None
    reasoner: str | None = None


class AgentResult(BaseModel):
    """Uniform envelope returned by every agent."""

    ok: bool
    summary: str = ""
    output: Any = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    error: str | None = None
    reasoner: str | None = None


class IncidentRecord(BaseModel):
    """The full lifecycle object the UI renders."""

    incident_id: str
    merchant_id: str
    state: IncidentState
    severity: Severity
    opened_at: datetime
    closed_at: datetime | None = None
    title: str = ""
    # Stable identity for the thing that is broken, used to correlate repeat detections into one
    # incident instead of opening a new one every monitoring cycle.
    signature: str = ""
    # Every segment key this incident covers, so a later detection that lands on any of them is
    # recognised as the same fault even when the worst-hit segment has shifted.
    segment_keys: list[str] = Field(default_factory=list)
    correlated_detections: int = 0
    anomaly: AnomalySignal | None = None
    evidence: EvidenceBundle | None = None
    impact: ImpactAssessment | None = None
    root_cause: RootCauseAssessment | None = None
    proposal: ActionProposal | None = None
    policy_decision: PolicyDecision | None = None
    action_result: ActionResult | None = None
    verification: VerificationResult | None = None
    escalation: Escalation | None = None
    steps: list[AgentStep] = Field(default_factory=list)
    audit: list[AuditRecord] = Field(default_factory=list)
    attempts: int = 0
    outcome: str | None = None
    revenue_protected_per_hour_paise: int = 0
    time_to_detect_s: float | None = None
    time_to_mitigate_s: float | None = None
    # --- closed loop -------------------------------------------------------
    # The priced option set the decision was made from, with the historical evidence attached to
    # each option. Present from RECOVERY_PLANNING onward.
    recovery_plan: RecoveryPlan | None = None
    # The second recovery layer's result: payments that had already failed and were rescued.
    order_recovery: OrderRecoveryResult | None = None
    # The incident's financial story, computed at LEARNING from recorded measurements only.
    revenue: RevenueOutcome | None = None
    # What was written to memory when this incident closed, so the trace can show it.
    learning: IncidentOutcomeRecord | None = None
