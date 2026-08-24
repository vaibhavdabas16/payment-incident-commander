"""Typed contracts for the entire system.

Every agent boundary, tool result and persisted artefact is one of these models.
If it is not in this file, it is not part of an agent contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


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
    DECIDING = "DECIDING"
    POLICY_REVIEW = "POLICY_REVIEW"
    AWAITING_HUMAN_APPROVAL = "AWAITING_HUMAN_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    ROLLING_BACK = "ROLLING_BACK"
    RESOLVED = "RESOLVED"
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
    SET_MONITORING_FREQUENCY = "set_monitoring_frequency"
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


class PaymentEvent(BaseModel):
    """One payment attempt. Money is always integer paise (ADR-008)."""

    payment_id: str
    order_id: str
    timestamp: datetime
    merchant_id: str
    amount_paise: int
    payment_method: str
    gateway: str
    psp: str
    issuer: str
    network: str | None = None
    geography: str
    device: str
    os: str
    app_version: str
    status: Literal["success", "failed"]
    error_code: str | None = None
    latency_ms: int
    retry_count: int = 0
    is_retry: bool = False
    route_id: str
    # Derived at generation time so order-value-specific failures are sliceable like any other
    # dimension. The spec calls these out explicitly and they are invisible without it.
    amount_band: str = "mid"

    @field_validator("amount_paise")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("amount_paise must be positive")
        return v


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
    estimated_revenue_protected_per_hour_paise: int = 0
    side_effects: list[str] = Field(default_factory=list)
    rollback_recommended: bool = False
    explanation: str = ""


class Escalation(BaseModel):
    incident_id: str
    reason_code: str
    reason: str
    urgency: Severity
    recommended_human_action: str
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
