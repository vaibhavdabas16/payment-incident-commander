"""Root Cause Agent.

Hypotheses are scored deterministically from evidence features, then handed to the reasoner to
re-rank and narrate (ADR-001, ADR-003). Splitting it this way means the model can add judgement
without being able to hallucinate a cause into existence: it may only reorder candidates that the
evidence already supports, and any evidence it cites is checked against the bundle.

Each hypothesis declares which observations support it and which argue against it. Contradicting
evidence is scored explicitly rather than ignored, because the failure mode that matters here is
confirmation: an agent that only looks for support will confidently diagnose a PSP outage during a
traffic-mix change, and then "fix" it by rerouting healthy traffic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..schemas import (
    AgentResult,
    EvidenceBundle,
    Hypothesis,
    IncidentState,
    RootCauseAssessment,
)
from ..llm.base import ReasonerUnavailable
from .base import Agent, IncidentContext
from .investigation import CUSTOMER_ERRORS

# Two hypotheses within this margin are reported as ambiguous rather than force-ranked.
AMBIGUITY_MARGIN = 0.12
# Memory can nudge a prior but never dominate live evidence.
MAX_MEMORY_ADJUSTMENT = 0.15
# Minimum total evidence mass used when converting scores to probabilities, so that an unopposed
# but weakly supported hypothesis cannot reach certainty. See `_to_hypotheses`.
MIN_EVIDENCE_MASS = 0.9

# How far above its explanatory coverage a segment-level diagnosis may be stated. One fault often
# surfaces across several segments, so the single best segment understates it - but only by so
# much. See `_explanatory_coverage`.
COVERAGE_ALLOWANCE = 0.15


@dataclass
class CauseFeatures:
    """Numeric summary of the evidence bundle, computed once and shared by every scorer."""

    dominant_error: str | None = None
    dominant_share: float = 0.0
    error_novel: bool = False
    latency_shift_ms: float = 0.0
    traffic_tvd: float = 0.0
    config_change_minutes: float | None = None
    config_component: str | None = None
    config_change_id: str | None = None
    # Strongest concentration (failure share / traffic share) seen on each dimension.
    concentration: dict[str, float] = field(default_factory=dict)
    # Worst deviation observed on each dimension.
    deviation: dict[str, float] = field(default_factory=dict)
    # Best segment label per dimension, for human-readable causes.
    label: dict[str, str] = field(default_factory=dict)
    strong_dimensions: list[str] = field(default_factory=list)
    evidence_by_dimension: dict[str, list[str]] = field(default_factory=dict)

    def conc(self, dimension: str) -> float:
        return self.concentration.get(dimension, 0.0)

    def dev(self, dimension: str) -> float:
        return abs(self.deviation.get(dimension, 0.0))


def extract_features(bundle: EvidenceBundle) -> CauseFeatures:
    f = CauseFeatures(
        dominant_error=bundle.dominant_error_code,
        dominant_share=bundle.dominant_error_share,
        latency_shift_ms=bundle.latency_shift_ms,
    )
    f.traffic_tvd = max((abs(v) for v in bundle.traffic_composition_shift.values()), default=0.0)

    for finding in bundle.findings:
        f.evidence_by_dimension.setdefault(finding.dimension, []).append(finding.finding_id)

        if finding.dimension == "error_code" and finding.metrics.get("novel"):
            f.error_novel = True

        if finding.dimension == "config_change":
            minutes = float(finding.metrics.get("minutes_ago", 999))
            if f.config_change_minutes is None or minutes < f.config_change_minutes:
                f.config_change_minutes = minutes
                f.config_component = finding.metrics.get("component")
                f.config_change_id = finding.metrics.get("change_id")

        concentration = finding.metrics.get("concentration")
        deviation = finding.metrics.get("deviation")
        if concentration is None or deviation is None:
            continue
        # A finding explicitly identified as an echo of the primary fault is a real observation but
        # not independent evidence, so it must not establish its dimension as a separate cause.
        if finding.metrics.get("independent") is False:
            continue
        dim = finding.dimension
        if concentration > f.concentration.get(dim, 0.0):
            f.concentration[dim] = float(concentration)
            f.deviation[dim] = float(deviation)
            segment = finding.metrics.get("segment", {})
            f.label[dim] = ", ".join(f"{k}={v}" for k, v in segment.items())

    # A dimension counts as "strong" when its failures are deep and more than proportional.
    # The concentration bar has to stay low: a PSP carrying 55% of all traffic cannot exceed about
    # 1.8x concentration even when it is the sole cause, so a high bar would make the largest and
    # most expensive faults invisible to this test.
    f.strong_dimensions = [
        d for d, c in f.concentration.items() if c >= 1.15 and f.dev(d) >= 0.10
    ]
    return f


# --------------------------------------------------------------------------
# Hypothesis catalogue
# --------------------------------------------------------------------------


@dataclass
class HypothesisSpec:
    cause_id: str
    template: str
    # Dimensions whose evidence supports this hypothesis.
    dimensions: tuple[str, ...]
    description: str

    def describe(self, features: CauseFeatures) -> str:
        label = next((features.label[d] for d in self.dimensions if d in features.label), None)
        return self.template.format(segment=label or "the affected segment")


HYPOTHESIS_CATALOGUE: dict[str, HypothesisSpec] = {
    "psp_degradation": HypothesisSpec(
        "psp_degradation",
        "Payment service provider degradation on {segment}",
        ("psp", "route_id"),
        "A specific PSP or acquiring route is failing while others stay healthy.",
    ),
    "issuer_degradation": HypothesisSpec(
        "issuer_degradation",
        "Issuing bank degradation affecting {segment}",
        ("issuer",),
        "A specific issuing bank is declining or timing out authorisations.",
    ),
    "gateway_degradation": HypothesisSpec(
        "gateway_degradation",
        "Gateway degradation on {segment}",
        ("gateway", "route_id"),
        "A gateway is slow or erroring across the methods it serves.",
    ),
    "config_regression": HypothesisSpec(
        "config_regression",
        "Merchant configuration change regression affecting {segment}",
        ("config_change", "app_version", "amount_band"),
        "A recent merchant-side change immediately precedes the degradation.",
    ),
    "checkout_client_issue": HypothesisSpec(
        "checkout_client_issue",
        "Client-side checkout failure on {segment}",
        ("app_version", "os"),
        "One client build or OS is failing to complete checkout.",
    ),
    "traffic_mix_shift": HypothesisSpec(
        "traffic_mix_shift",
        "Traffic composition change, no underlying fault",
        ("traffic_mix",),
        "Customers moved to methods that always convert worse; nothing is broken.",
    ),
    "payment_method_degradation": HypothesisSpec(
        "payment_method_degradation",
        "Broad degradation of {segment} across providers",
        ("payment_method",),
        "An entire payment method is degraded across every provider serving it.",
    ),
    "latency_timeout_cascade": HypothesisSpec(
        "latency_timeout_cascade",
        "Latency-driven timeout cascade on {segment}",
        ("latency", "route_id", "gateway"),
        "Response times exceeded the timeout budget, converting slow calls into failures.",
    ),
    "multi_factor": HypothesisSpec(
        "multi_factor",
        "Multiple concurrent degradations",
        (),
        "Several independent segments degraded at once; no single cause explains the drop.",
    ),
}


def score_hypotheses(f: CauseFeatures) -> dict[str, tuple[float, list[str]]]:
    """Score every hypothesis in [0, 1] with the reasons that drove the score.

    Scores are deliberately additive and readable rather than learned: at this data scale a fitted
    model would encode the simulator's quirks, and nobody could argue with its output during an
    incident review.
    """
    scores: dict[str, tuple[float, list[str]]] = {}
    infra_error = f.dominant_error not in CUSTOMER_ERRORS and f.dominant_error is not None
    customer_dominated = f.dominant_error in CUSTOMER_ERRORS and f.dominant_share >= 0.35

    # --- PSP -------------------------------------------------------------
    s, why = 0.0, []
    if f.conc("psp") >= 1.25:
        s += 0.35 * min(1.0, f.conc("psp") / 3.0) + 0.30 * min(1.0, f.dev("psp") / 0.30)
        why.append(f"failures concentrated on one PSP ({f.conc('psp'):.1f}x its traffic share)")
    if f.dominant_error == "PSP_UNAVAILABLE":
        s += 0.30
        why.append("dominant failure code is PSP_UNAVAILABLE")
    if f.error_novel and infra_error:
        s += 0.10
        why.append("the dominant failure code is new since the baseline window")
    scores["psp_degradation"] = (s, why)

    # --- Issuer ----------------------------------------------------------
    s, why = 0.0, []
    if f.conc("issuer") >= 1.4:
        s += 0.40 * min(1.0, f.conc("issuer") / 3.0) + 0.30 * min(1.0, f.dev("issuer") / 0.30)
        why.append(f"failures concentrated on one issuer ({f.conc('issuer'):.1f}x its traffic share)")
    if f.dominant_error in ("ISSUER_DECLINE", "BANK_DECLINE", "BANK_UNAVAILABLE"):
        s += 0.30
        why.append(f"dominant failure code {f.dominant_error} is issuer-side")
    scores["issuer_degradation"] = (s, why)

    # --- Gateway ---------------------------------------------------------
    s, why = 0.0, []
    if f.conc("gateway") >= 1.3 or f.conc("route_id") >= 1.5:
        s += 0.30 * min(1.0, max(f.conc("gateway"), f.conc("route_id")) / 3.0)
        s += 0.25 * min(1.0, max(f.dev("gateway"), f.dev("route_id")) / 0.30)
        why.append("failures concentrated on one gateway/route")
    if f.dominant_error == "GATEWAY_TIMEOUT":
        s += 0.35
        why.append("dominant failure code is GATEWAY_TIMEOUT")
    if f.latency_shift_ms >= 1500:
        s += 0.15
        why.append(f"p95 latency rose {f.latency_shift_ms:.0f} ms")
    scores["gateway_degradation"] = (s, why)

    # --- Config regression ------------------------------------------------
    s, why = 0.0, []
    if f.config_change_minutes is not None:
        # Recency is the signal. A change 20 minutes before onset is strong evidence; one from
        # three hours ago is background noise that would otherwise blame every deploy.
        recency = max(0.0, 1.0 - f.config_change_minutes / 120.0)
        s += 0.55 * recency
        why.append(
            f"configuration change to {f.config_component} {f.config_change_minutes:.0f} minutes ago"
        )
        if f.conc("app_version") >= 1.5 or f.conc("amount_band") >= 1.5:
            s += 0.30
            why.append("failures concentrated in the segment that change would affect")
    if f.dominant_error in ("CHECKOUT_CALLBACK_TIMEOUT", "RISK_RULE_DECLINE"):
        s += 0.20
        why.append(f"failure code {f.dominant_error} is consistent with a merchant-side change")
    scores["config_regression"] = (s, why)

    # --- Client / checkout -------------------------------------------------
    s, why = 0.0, []
    if f.conc("app_version") >= 1.5:
        s += 0.40 * min(1.0, f.conc("app_version") / 3.0) + 0.30 * min(1.0, f.dev("app_version") / 0.35)
        why.append("failures concentrated on one client build")
    if f.dominant_error == "CHECKOUT_CALLBACK_TIMEOUT":
        s += 0.30
        why.append("dominant failure code is a checkout callback timeout")
    if f.config_change_minutes is not None and f.config_component == "checkout_sdk":
        # The config-change hypothesis explains the same evidence and names the fix, so it should
        # win; this stays a live alternative rather than a competitor.
        s -= 0.15
        why.append("a recorded SDK change may explain this more directly")
    scores["checkout_client_issue"] = (s, why)

    # --- Traffic mix -------------------------------------------------------
    s, why = 0.0, []
    if f.traffic_tvd >= 0.05:
        s += min(0.75, 3.0 * f.traffic_tvd)
        why.append(f"payment method mix moved by {f.traffic_tvd:.0%}")
    if customer_dominated:
        s += 0.25
        why.append(f"failures are dominated by {f.dominant_error}, a customer-side code")
    # Absence of a localised fault only argues for a mix change if the mix actually moved, or if
    # the failures look customer-side. Otherwise every clean single-cause incident hands this
    # hypothesis free probability for no reason.
    if not f.strong_dimensions and (f.traffic_tvd >= 0.03 or customer_dominated):
        s += 0.20
        why.append("no provider, issuer or client segment is disproportionately failing")
    scores["traffic_mix_shift"] = (s, why)

    # --- Whole payment method ---------------------------------------------
    s, why = 0.0, []
    # The concentration bar matches `strong_dimensions` above, and for the same reason stated
    # there: a slice carrying most of the traffic cannot show a high concentration ratio even when
    # it is entirely responsible. UPI is the largest payment method, so a rails-wide UPI failure
    # tops out near 1.3x - under the old 1.4 bar this hypothesis scored exactly 0.0 on the one
    # scenario built to test it, and the diagnosis defaulted to a single PSP on error-code
    # evidence alone, with no concentration evidence behind it at all.
    if f.conc("payment_method") >= 1.15 and f.dev("payment_method") >= 0.10:
        s += 0.35 * min(1.0, f.conc("payment_method") / 2.5) + 0.30 * min(1.0, f.dev("payment_method") / 0.30)
        why.append("one payment method is degraded overall")
        # Only a broad method fault if no single provider inside it explains it. The bar matches
        # the recalibrated concentration scale: a provider carrying most of a method's traffic
        # cannot show a high concentration ratio even when it is entirely responsible.
        if f.conc("psp") < 1.25 and f.conc("issuer") < 1.25:
            s += 0.25
            why.append("no single PSP or issuer inside that method accounts for the failures")
        else:
            s -= 0.20
            why.append("a specific provider inside that method is the better explanation")
    scores["payment_method_degradation"] = (s, why)

    # --- Timeout cascade ----------------------------------------------------
    s, why = 0.0, []
    if f.latency_shift_ms >= 1200:
        s += min(0.55, f.latency_shift_ms / 6000.0)
        why.append(f"p95 latency rose {f.latency_shift_ms:.0f} ms")
    if f.dominant_error in ("GATEWAY_TIMEOUT", "AUTH_TIMEOUT", "CHECKOUT_CALLBACK_TIMEOUT"):
        s += 0.35
        why.append(f"failures are timeout-class ({f.dominant_error})")
    scores["latency_timeout_cascade"] = (s, why)

    # --- Multi-factor -------------------------------------------------------
    s, why = 0.0, []
    independent = _independent_dimensions(f)
    if len(independent) >= 2:
        # Scored from the hypotheses it subsumes rather than a flat prior. Each single cause here
        # explains one of the independent faults and is silent about the other, so a flat 0.40
        # meant the strongest partial explanation always outranked the account that covered
        # everything, and a second fault could never be reported however clear it became.
        #
        # Averaging the two keeps a strong fault beside a marginal one reading as a single cause -
        # which is what it is - while two comparable faults outrank either alone. The independence
        # comes from the residual test in the investigation, not a magnitude heuristic, so an echo
        # of the primary cannot inflate it.
        covered: list[float] = []
        for group in independent:
            dims = _DIMENSION_GROUPS.get(group, {group})
            covered.append(
                max(
                    (
                        score
                        for cause_id, (score, _reason) in scores.items()
                        if dims & set(HYPOTHESIS_CATALOGUE[cause_id].dimensions)
                    ),
                    default=0.0,
                )
            )
        covered.sort(reverse=True)
        s = (covered[0] + covered[1]) / 2 + 0.20 + 0.10 * min(2, len(independent) - 2)
        why.append(
            "several unrelated segments degraded together, and no single cause explains them "
            "all: " + ", ".join(sorted(independent))
        )
    scores["multi_factor"] = (s, why)

    return {k: (max(0.0, min(1.0, v[0])), v[1]) for k, v in scores.items()}


def _explanatory_coverage(bundle: EvidenceBundle, cause_id: str) -> float | None:
    """Share of all failures carried by the best segment this hypothesis is about.

    `None` when coverage is not a meaningful question for the cause: `config_regression` is
    evidenced by a change record rather than by a segment, so it has no failure share to be
    judged against and must not be capped by one.
    """
    spec = HYPOTHESIS_CATALOGUE.get(cause_id)
    if spec is None:
        return None
    dimensions = set(spec.dimensions)
    shares = [
        float(f.metrics["failure_share"])
        for f in bundle.findings
        if f.dimension in dimensions and f.metrics.get("failure_share") is not None
    ]
    return max(shares) if shares else None


# Dimensions that describe one fault from several angles. Route, gateway and PSP are the same
# routing decision seen three ways, so a single PSP outage lights all three up.
_DIMENSION_GROUPS: dict[str, set[str]] = {
    "routing": {"psp", "route_id", "gateway"},
    "issuer": {"issuer"},
    "client": {"app_version", "os"},
    "geography": {"geography"},
    "value": {"amount_band"},
}


def _independent_dimensions(f: CauseFeatures) -> set[str]:
    """Strong dimensions that are not aliases of one another.

    Collapsing aliases prevents one fault being mistaken for three and wrongly diagnosed as
    multi-factor.
    """
    hit = set()
    for group, dims in _DIMENSION_GROUPS.items():
        if any(d in f.strong_dimensions for d in dims):
            hit.add(group)
    return hit


def _contradictions(cause_id: str, f: CauseFeatures, bundle: EvidenceBundle) -> list[str]:
    """Evidence that argues against a hypothesis. Always computed, never skipped."""
    out: list[str] = []
    if cause_id != "traffic_mix_shift" and f.traffic_tvd >= 0.08:
        out.append(
            f"Traffic mix moved {f.traffic_tvd:.0%}, so part of the drop may be composition "
            "rather than failure."
        )
    if cause_id in ("psp_degradation", "gateway_degradation") and f.conc("issuer") >= 1.8:
        out.append("Failures are also concentrated on a specific issuer, which routing cannot fix.")
    if cause_id == "issuer_degradation" and f.conc("psp") >= 1.8:
        out.append("Failures are also concentrated on a specific PSP, pointing at routing instead.")
    if cause_id == "traffic_mix_shift" and f.strong_dimensions:
        out.append(
            "A specific segment is failing far more than its traffic share, which a pure mix "
            "change would not produce."
        )
    if cause_id != "config_regression" and f.config_change_minutes is not None and f.config_change_minutes <= 45:
        out.append(
            f"A merchant configuration change {f.config_change_minutes:.0f} minutes ago is an "
            "unexplained coincidence under this hypothesis."
        )
    if cause_id == "config_regression" and f.config_change_minutes is None:
        out.append("No merchant configuration change was found in the recent window.")
    if bundle.degraded:
        out.append("Evidence is incomplete: at least one investigation tool failed.")
    return out


# --------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------


class RootCauseAgent(Agent):
    name = "root_cause"
    state = IncidentState.DIAGNOSING

    def run(self, ctx: IncidentContext) -> AgentResult:
        bundle = ctx.incident.evidence
        anomaly = ctx.incident.anomaly
        if bundle is None or anomaly is None:
            return AgentResult(ok=False, summary="no evidence to diagnose", error="missing_evidence")

        features = extract_features(bundle)
        raw = score_hypotheses(features)
        candidates = self._to_hypotheses(raw, features, bundle, ctx)

        if not candidates:
            return AgentResult(
                ok=False, summary="no hypothesis scored above zero", error="no_hypothesis"
            )

        reasoner_name = "deterministic"
        try:
            ranking = ctx.reasoner.rank_hypotheses(
                bundle,
                candidates,
                {
                    "current_success_rate": anomaly.current_value,
                    "baseline_success_rate": anomaly.baseline,
                    "correlated_signals": bundle.correlated_signals,
                },
            )
            reasoner_name = ranking.reasoner
        except ReasonerUnavailable as exc:
            from ..llm.deterministic import DeterministicReasoner

            ranking = DeterministicReasoner().rank_hypotheses(bundle, candidates, {})
            ctx.publish("reasoner_degraded", {"error": str(exc)})

        ranked = ranking.hypotheses
        top = ranked[0]

        # Evidence incompleteness must cap confidence: a diagnosis built on partial data cannot
        # honestly present itself as though every tool had answered.
        confidence = top.probability
        if bundle.degraded:
            confidence = min(confidence, 0.5)

        # Explanatory coverage caps it too. A segment-level cause is the claim that one slice of
        # traffic is responsible, so it cannot honestly be stated more confidently than the share
        # of the failures that slice actually carries. Without this the agent reported 77%
        # confidence in a payment-method fault whose segment held a quarter of the failures -
        # a claim the evidence never made.
        #
        # This is a consistency rule, not a calibration fitted to outcomes: it never looks at
        # whether the diagnosis turned out right, only at whether the evidence supports the
        # strength of the claim. It runs after ranking, so it cannot reorder hypotheses.
        cover = _explanatory_coverage(bundle, top.cause_id)
        if cover is not None:
            confidence = min(confidence, cover + COVERAGE_ALLOWANCE)

        assessment = RootCauseAssessment(
            incident_id=ctx.incident.incident_id,
            most_likely_root_cause=top.cause,
            cause_id=top.cause_id,
            confidence=round(confidence, 3),
            hypotheses=ranked,
            supporting_evidence=top.supporting_evidence,
            contradicting_evidence=top.contradicting_evidence,
            ambiguous=ranking.ambiguous,
            reasoner=reasoner_name,
            narrative=ranking.narrative,
        )
        summary = f"{top.cause} at {confidence:.0%} confidence"
        if ranking.ambiguous:
            summary += " (ambiguous)"
        return AgentResult(ok=True, summary=summary, output=assessment, reasoner=reasoner_name)

    def _to_hypotheses(
        self,
        raw: dict[str, tuple[float, list[str]]],
        features: CauseFeatures,
        bundle: EvidenceBundle,
        ctx: IncidentContext,
    ) -> list[Hypothesis]:
        positive = {k: v for k, v in raw.items() if v[0] > 0.05}
        if not positive:
            return []

        priors = self._memory_priors(bundle)
        adjusted: dict[str, tuple[float, float, list[str]]] = {}
        for cause_id, (score, why) in positive.items():
            nudge = max(-MAX_MEMORY_ADJUSTMENT, min(MAX_MEMORY_ADJUSTMENT, priors.get(cause_id, 0.0)))
            adjusted[cause_id] = (max(0.0, score + nudge), nudge, why)

        # Normalise against a floor, not merely against the sum. Dividing by the sum alone would
        # report a lone hypothesis scoring 0.4 as 100% certain simply because nothing competed with
        # it - turning weak evidence into false confidence, and handing the policy gateway a number
        # that would authorise autonomous action it should not. The floor keeps absolute evidence
        # strength in the answer, so thin evidence yields low confidence even when it is unopposed.
        total = max(sum(v[0] for v in adjusted.values()), MIN_EVIDENCE_MASS)
        out: list[Hypothesis] = []
        for cause_id, (score, nudge, why) in adjusted.items():
            spec = HYPOTHESIS_CATALOGUE[cause_id]
            evidence_ids: list[str] = []
            for dim in spec.dimensions:
                evidence_ids.extend(features.evidence_by_dimension.get(dim, []))
            out.append(
                Hypothesis(
                    cause_id=cause_id,
                    cause=spec.describe(features),
                    probability=round(score / total, 4),
                    supporting_evidence=sorted(set(evidence_ids))[:6],
                    contradicting_evidence=_contradictions(cause_id, features, bundle),
                    deterministic_score=round(score, 4),
                    memory_adjustment=round(nudge, 4),
                    reasoning="; ".join(why),
                )
            )
        out.sort(key=lambda h: h.probability, reverse=True)
        _ = ctx
        return out

    def _memory_priors(self, bundle: EvidenceBundle) -> dict[str, float]:
        """Small prior nudges from resolved incidents that looked like this one."""
        priors: dict[str, float] = {}
        for match in bundle.similar_past_incidents:
            cause_id = match.get("root_cause_id")
            similarity = float(match.get("similarity", 0.0))
            if not cause_id or similarity <= 0:
                continue
            weight = MAX_MEMORY_ADJUSTMENT * similarity
            # A past false positive is evidence against repeating that diagnosis.
            if match.get("false_positive"):
                weight = -weight
            priors[cause_id] = priors.get(cause_id, 0.0) + weight
        return priors
