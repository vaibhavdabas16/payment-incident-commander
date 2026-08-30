"""Investigation Agent.

Gathers evidence; does not guess. It runs a fixed battery of read tools across every dimension that
could explain a payment drop, and converts each tool result into `Finding` records with an explicit
`strength` reflecting how strongly that observation discriminates between causes.

Strength is the important part. "UPI is failing" is true in almost every incident and separates
almost nothing; "97% of failures carry an error code that did not exist an hour ago" separates a
great deal. Scoring findings by discriminating power, rather than treating every observation as
equally informative, is what stops diagnosis collapsing onto whichever cause has the most evidence
merely restated.

If a tool fails, the bundle is marked `degraded` and downstream confidence is capped — a diagnosis
built on partial evidence must not present itself as complete.
"""

from __future__ import annotations

import math
from typing import Any

from ..schemas import AgentResult, ConfigChange, EvidenceBundle, Finding, IncidentState, SegmentStat
from .base import Agent, IncidentContext

# Error codes that point at infrastructure rather than at the customer or their bank balance.
INFRA_ERRORS = {
    "PSP_UNAVAILABLE",
    "GATEWAY_TIMEOUT",
    "BANK_UNAVAILABLE",
    "CHECKOUT_CALLBACK_TIMEOUT",
    "AUTH_TIMEOUT",
    "RISK_RULE_DECLINE",
    "ISSUER_DECLINE",
}

# A joint slice must be at least this much worse than its parent dimension before the second
# dimension is credited. Below this it is an echo of the parent, not independent evidence.
MIN_MARGINAL_EXCESS = 0.08

# ...and the gap must also be too large to be chance. A joint slice often holds only ~60 payments,
# where the standard error on a success rate is around 4 points - so an 8-point gap is barely two
# sigma and will appear somewhere among the dozens of cells examined every cycle. Requiring a
# z-score as well is what stops the agent inventing a client-side regression during a PSP outage.
MIN_MARGINAL_Z = 3.0
MIN_CELL_VOLUME = 40

# Single-dimension findings are compared against their own baseline and get a lighter guard, since
# they aggregate far more traffic.
MIN_SEGMENT_Z = 2.5

# Dimensions worth testing for independence from the primary fault.
_RESIDUAL_DIMENSIONS = {"psp", "route_id", "gateway", "issuer", "geography", "payment_method", "app_version", "os"}

# After excluding the primary segment, a dimension must still be at least this degraded to count as
# an independent fault rather than an echo.
RESIDUAL_INDEPENDENCE_DROP = 0.08

# Before spending a tool call on the reversed independence test, the candidate must be a
# substantial fault in its own right - this deep below its baseline and this concentrated.
REVERSE_TEST_MIN_DROP = 0.08
REVERSE_TEST_MIN_CONCENTRATION = 1.3

# Dimensions that describe the same routing decision, so one implies the others.
_ROUTING_ALIASES = {"psp", "route_id", "gateway"}


def _aliases_primary(dimension: str, primary_dims: set[str]) -> bool:
    """True when a dimension is an alias of the primary segment's own dimensions.

    A route determines its gateway and PSP, so all three describe one routing decision. Likewise a
    joint primary such as (payment_method, psp) already contains the method.
    """
    if dimension in primary_dims:
        return True
    return dimension in _ROUTING_ALIASES and bool(_ROUTING_ALIASES & primary_dims)


def _label(segment: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in segment.items())


def _provably_disjoint(segment: dict[str, Any], primary: dict[str, str]) -> bool:
    """True when two segments cannot contain the same payment.

    They are disjoint whenever they name the same dimension with different values: a payment on
    `payment_method=card` is not a payment on `payment_method=upi`. Sharing no dimension at all
    proves nothing - the slices may overlap heavily - so that case is left to the residual test.
    """
    return any(
        key in primary and str(value) != str(primary[key]) for key, value in segment.items()
    )


def _deviation_z(observed_rate: float, expected_rate: float, n: int) -> float:
    """Standard-error z-score of an observed success rate against an expected one."""
    if n <= 0:
        return 0.0
    expected_rate = min(max(expected_rate, 1e-6), 1 - 1e-6)
    se = math.sqrt(expected_rate * (1 - expected_rate) / n)
    if se <= 1e-12:
        return 0.0
    return (observed_rate - expected_rate) / se

# Codes that are normal background noise at any success rate.
CUSTOMER_ERRORS = {"INSUFFICIENT_FUNDS", "USER_DROPPED", "CARD_EXPIRED", "INVALID_VPA"}


class InvestigationAgent(Agent):
    name = "investigation"
    state = IncidentState.INVESTIGATING

    def run(self, ctx: IncidentContext) -> AgentResult:
        anomaly = ctx.incident.anomaly
        if anomaly is None:
            return AgentResult(ok=False, summary="no anomaly to investigate", error="missing_anomaly")

        bundle = EvidenceBundle(incident_id=ctx.incident.incident_id)
        counter = _Counter()
        degraded = False

        # Normally the detection window. A caller may ask for a wider one - the post-action review
        # pools everything since onset, because a second fault is often too small to establish in
        # the two minutes available when the first decision has to be made.
        window_minutes = ctx.scratch.get("window_minutes") or max(
            2, int((anomaly.window_end - anomaly.window_start).total_seconds() // 60)
        )

        # --- failure mix -------------------------------------------------
        errors = ctx.call_tool("get_error_distribution", minutes=window_minutes)
        if errors is None:
            degraded = True
        else:
            bundle.error_distribution = {r["error_code"]: r["count"] for r in errors["errors"]}
            self._error_findings(bundle, errors, counter)

        # --- parent marginals, needed before any joint slice can be interpreted -------
        methods = ctx.call_tool("get_payment_method_metrics", minutes=window_minutes)
        method_deviation: dict[str, float] = {}
        if methods is None:
            degraded = True
        else:
            method_deviation = _deviation_index(methods["rows"], "payment_method")
            self._segment_findings(
                bundle, methods["rows"], "payment_method", counter, "get_payment_method_metrics"
            )

        # --- routing side ------------------------------------------------
        gateway = ctx.call_tool("get_gateway_metrics", minutes=window_minutes)
        if gateway is None:
            degraded = True
        else:
            self._segment_findings(bundle, gateway["by_psp"], "psp", counter, "get_gateway_metrics")
            self._segment_findings(
                bundle, gateway["by_route"], "route_id", counter, "get_gateway_metrics"
            )
            self._segment_findings(bundle, gateway["rows"], "gateway", counter, "get_gateway_metrics")

        # --- issuer side -------------------------------------------------
        banks = ctx.call_tool("get_bank_metrics", minutes=window_minutes)
        if banks is None:
            degraded = True
        else:
            self._segment_findings(bundle, banks["rows"], "issuer", counter, "get_bank_metrics")
            self._cross_segment_findings(
                bundle,
                banks["by_method_and_issuer"],
                secondary_dimension="issuer",
                parent_dimension="payment_method",
                parent_deviation=method_deviation,
                counter=counter,
                tool="get_bank_metrics",
            )

        # --- client side -------------------------------------------------
        devices = ctx.call_tool("get_device_metrics", minutes=window_minutes)
        if devices is None:
            degraded = True
        else:
            os_deviation = _deviation_index(devices["by_os"], "os")
            self._cross_segment_findings(
                bundle,
                devices["by_os_and_app_version"],
                secondary_dimension="app_version",
                parent_dimension="os",
                parent_deviation=os_deviation,
                counter=counter,
                tool="get_device_metrics",
            )

        # --- geography, value -----------------------------------
        geo = ctx.call_tool("get_geographic_metrics", minutes=window_minutes)
        if geo is not None:
            self._segment_findings(
                bundle, geo["rows"], "geography", counter, "get_geographic_metrics"
            )

        values = ctx.call_tool("get_order_value_distribution", minutes=window_minutes)
        if values is not None:
            self._cross_segment_findings(
                bundle,
                values["by_method_and_band"],
                secondary_dimension="amount_band",
                parent_dimension="payment_method",
                parent_deviation=method_deviation,
                counter=counter,
                tool="get_order_value_distribution",
            )

        # --- latency -----------------------------------------------------
        latency = ctx.call_tool("get_latency_metrics", minutes=window_minutes)
        if latency is not None:
            bundle.latency_shift_ms = float(latency["delta_ms"])
            self._latency_findings(bundle, latency, counter)

        # --- traffic composition ----------------------------------------
        composition = ctx.call_tool("get_traffic_composition", dimension="payment_method", minutes=window_minutes)
        if composition is not None:
            bundle.traffic_composition_shift = {
                r["value"]: r["share_delta"] for r in composition["rows"]
            }
            self._composition_findings(bundle, composition, counter)

        # --- merchant-side changes ---------------------------------------
        changes = ctx.call_tool("get_recent_configuration_changes", minutes=120)
        if changes is not None:
            self._config_findings(bundle, changes, counter)

        # --- retries ------------------------------------------------------
        retries = ctx.call_tool("get_retry_effectiveness", minutes=window_minutes)
        if retries is not None and retries.get("retry_attempts", 0) > 0:
            rate = retries.get("retry_success_rate")
            if rate is not None:
                bundle.findings.append(
                    Finding(
                        finding_id=counter.next(),
                        source_tool="get_retry_effectiveness",
                        dimension="retry",
                        statement=(
                            f"Retries are recovering {rate:.0%} of reattempted payments "
                            f"({retries['retry_successes']}/{retries['retry_attempts']})."
                        ),
                        metrics=retries,
                        strength=0.3,
                    )
                )

        # --- prior incidents ----------------------------------------------
        history = ctx.call_tool(
            "get_historical_incidents",
            limit=3,
            dominant_error_code=bundle.dominant_error_code,
        )
        if history and history.get("matches"):
            bundle.similar_past_incidents = history["matches"]
            top = history["matches"][0]
            bundle.findings.append(
                Finding(
                    finding_id=counter.next(),
                    source_tool="get_historical_incidents",
                    dimension="memory",
                    statement=(
                        f"Similar past incident {top.get('incident_id')}: {top.get('root_cause')} "
                        f"(outcome: {top.get('outcome')})."
                    ),
                    metrics=top,
                    strength=0.25,
                )
            )

        self._mark_echoes(ctx, bundle, window_minutes)

        bundle.top_segments = list(anomaly.affected_segments)
        bundle.degraded = degraded
        bundle.tools_used = sorted({f.source_tool for f in bundle.findings})
        bundle.correlated_signals = self._correlations(bundle)

        summary = (
            f"{len(bundle.findings)} findings across {len(bundle.tools_used)} tools; "
            f"dominant failure code {bundle.dominant_error_code or 'n/a'} "
            f"({bundle.dominant_error_share:.0%} of failures)"
        )
        if degraded:
            summary += " [partial: one or more tools failed]"
        return AgentResult(ok=True, summary=summary, output=bundle)

    # ------------------------------------------------------------- builders

    def _error_findings(self, bundle: EvidenceBundle, errors: dict[str, Any], counter: _Counter) -> None:
        rows = errors.get("errors", [])
        if not rows:
            return
        top = rows[0]
        bundle.dominant_error_code = top["error_code"]
        bundle.dominant_error_share = top["share"]

        # A code that barely existed before is far more informative than one that merely grew.
        if top.get("novel"):
            strength = 0.95
            phrasing = "did not occur at all in the baseline window"
        elif top.get("lift") and top["lift"] >= 2.0:
            strength = 0.85
            phrasing = f"is {top['lift']:.1f}x its baseline share"
        elif top["error_code"] in CUSTOMER_ERRORS:
            # Customer-side codes dominating means nothing broke on our side.
            strength = 0.55
            phrasing = "is a customer-side failure code at close to its usual share"
        else:
            strength = 0.5
            phrasing = f"holds {top['share']:.0%} of failures"

        bundle.findings.append(
            Finding(
                finding_id=counter.next(),
                source_tool="get_error_distribution",
                dimension="error_code",
                statement=(
                    f"Failure code {top['error_code']} accounts for {top['share']:.0%} of failures "
                    f"and {phrasing}."
                ),
                metrics=top,
                strength=strength,
            )
        )

        for row in rows[1:4]:
            if row.get("novel") or (row.get("lift") or 0) >= 3.0:
                bundle.findings.append(
                    Finding(
                        finding_id=counter.next(),
                        source_tool="get_error_distribution",
                        dimension="error_code",
                        statement=(
                            f"Secondary failure code {row['error_code']} is elevated "
                            f"({row['share']:.0%} of failures, baseline {row['baseline_share']:.0%})."
                        ),
                        metrics=row,
                        strength=0.6,
                    )
                )

    def _segment_findings(
        self,
        bundle: EvidenceBundle,
        rows: list[dict[str, Any]],
        dimension: str,
        counter: _Counter,
        tool: str,
        max_findings: int = 2,
    ) -> None:
        emitted = 0
        for row in rows:
            deviation = row.get("deviation")
            if deviation is None or deviation >= -0.05:
                continue
            if row.get("failure_share", 0) < 0.15:
                continue
            z = _deviation_z(
                row["success_rate"], row.get("baseline_success_rate") or 0.0, row.get("transactions", 0)
            )
            if z > -MIN_SEGMENT_Z:
                continue
            # Concentration is what makes a segment finding informative: a segment carrying far
            # more of the failures than of the traffic is a genuine localisation.
            traffic_share = max(row.get("traffic_share", 0.0), 1e-6)
            concentration = row["failure_share"] / traffic_share
            strength = min(0.92, 0.35 + 0.18 * concentration + 0.6 * abs(deviation))
            label = ", ".join(f"{k}={v}" for k, v in row["segment"].items())
            bundle.findings.append(
                Finding(
                    finding_id=counter.next(),
                    source_tool=tool,
                    dimension=dimension,
                    statement=(
                        f"Segment {label} is at {row['success_rate']:.1%} success versus a baseline "
                        f"of {row['baseline_success_rate']:.1%} ({deviation:+.1%}), carrying "
                        f"{row['failure_share']:.0%} of all failures on {traffic_share:.0%} of traffic."
                    ),
                    metrics={**row, "concentration": round(concentration, 2), "z": round(z, 2)},
                    strength=round(strength, 3),
                )
            )
            emitted += 1
            if emitted >= max_findings:
                return

    def _cross_segment_findings(
        self,
        bundle: EvidenceBundle,
        rows: list[dict[str, Any]],
        secondary_dimension: str,
        parent_dimension: str,
        parent_deviation: dict[str, float],
        counter: _Counter,
        tool: str,
        max_findings: int = 2,
    ) -> None:
        """Emit findings for a joint slice, but only for the part the second dimension explains.

        A joint slice inherits its parent's damage. When UPI is broken, `upi x AXIS` shows failures
        at roughly 1.7x its traffic share purely because UPI is 55% of traffic - the issuer has
        contributed nothing. Recording that as issuer evidence is how a PSP outage gets diagnosed
        as an issuing-bank problem, which then produces the wrong intervention: you cannot reroute
        around a bank.

        So the test is marginal, not absolute. The cell must be materially worse than its parent
        dimension already is before the second dimension is credited with anything.
        """
        emitted = 0
        for row in rows:
            deviation = row.get("deviation")
            if deviation is None or deviation >= -0.05:
                continue
            if row.get("failure_share", 0) < 0.12:
                continue

            if row.get("transactions", 0) < MIN_CELL_VOLUME:
                continue

            parent_value = row["segment"].get(parent_dimension)
            parent_dev = parent_deviation.get(str(parent_value), 0.0)
            # How much worse this cell is than its parent already was.
            excess = deviation - parent_dev
            if excess > -MIN_MARGINAL_EXCESS:
                continue

            # Under the hypothesis that only the parent is broken, this cell should sit at its own
            # baseline shifted by the parent's deviation. Anything not clearly below that is noise.
            expected = (row.get("baseline_success_rate") or 0.0) + parent_dev
            z = _deviation_z(row["success_rate"], expected, row["transactions"])
            if z > -MIN_MARGINAL_Z:
                continue

            traffic_share = max(row.get("traffic_share", 0.0), 1e-6)
            concentration = row["failure_share"] / traffic_share
            label = ", ".join(f"{k}={v}" for k, v in row["segment"].items())
            bundle.findings.append(
                Finding(
                    finding_id=counter.next(),
                    source_tool=tool,
                    dimension=secondary_dimension,
                    statement=(
                        f"Segment {label} is at {row['success_rate']:.1%} success versus a baseline "
                        f"of {row['baseline_success_rate']:.1%} ({deviation:+.1%}), which is "
                        f"{abs(excess):.1%} worse than {parent_dimension}={parent_value} overall "
                        f"({parent_dev:+.1%}). The excess is attributable to "
                        f"{secondary_dimension}."
                    ),
                    metrics={
                        **row,
                        "concentration": round(concentration, 2),
                        "parent_deviation": round(parent_dev, 4),
                        "marginal_excess": round(excess, 4),
                        "marginal_z": round(z, 2),
                    },
                    strength=round(min(0.92, 0.40 + 1.6 * abs(excess)), 3),
                )
            )
            emitted += 1
            if emitted >= max_findings:
                return

    def _latency_findings(
        self, bundle: EvidenceBundle, latency: dict[str, Any], counter: _Counter
    ) -> None:
        delta = latency["delta_ms"]
        if delta < 800:
            return
        worst = max(latency.get("by_route", []), key=lambda r: r["delta_ms"], default=None)
        statement = (
            f"p95 latency rose {delta:.0f} ms to {latency['p95_latency_ms']:.0f} ms."
        )
        if worst and worst["delta_ms"] > 0:
            statement += f" Concentrated on {worst['route_id']} (+{worst['delta_ms']:.0f} ms)."
        bundle.findings.append(
            Finding(
                finding_id=counter.next(),
                source_tool="get_latency_metrics",
                dimension="latency",
                statement=statement,
                metrics=latency,
                # Large latency shifts distinguish a timeout cascade from a decline spike, which
                # no other signal does.
                strength=min(0.9, 0.45 + delta / 10000.0),
            )
        )

    def _composition_findings(
        self, bundle: EvidenceBundle, composition: dict[str, Any], counter: _Counter
    ) -> None:
        tvd = composition.get("total_variation_distance", 0.0)
        if tvd < 0.05:
            return
        movers = sorted(composition["rows"], key=lambda r: abs(r["share_delta"]), reverse=True)[:3]
        detail = ", ".join(f"{m['value']} {m['share_delta']:+.0%}" for m in movers)
        bundle.findings.append(
            Finding(
                finding_id=counter.next(),
                source_tool="get_traffic_composition",
                dimension="traffic_mix",
                statement=(
                    f"Payment method mix shifted materially (total variation {tvd:.0%}): {detail}."
                ),
                metrics=composition,
                # A large mix shift is decisive: it can explain a falling success rate with nothing
                # actually broken, so it must be able to outweigh segment evidence.
                strength=min(0.95, 0.5 + 2.0 * tvd),
            )
        )

    def _config_findings(
        self, bundle: EvidenceBundle, changes: dict[str, Any], counter: _Counter
    ) -> None:
        rows = changes.get("changes", [])
        if not rows:
            return
        bundle.recent_config_changes = [
            ConfigChange(
                change_id=r["change_id"],
                timestamp=r["timestamp"],
                merchant_id="",
                component=r["component"],
                description=r["description"],
                changed_by=r["changed_by"],
                reversible=r["reversible"],
            )
            for r in rows
        ]
        for row in rows[:2]:
            minutes = row["minutes_ago"]
            # Proximity in time is the whole signal here; a change four hours ago is background.
            strength = 0.9 if minutes <= 30 else (0.65 if minutes <= 90 else 0.35)
            bundle.findings.append(
                Finding(
                    finding_id=counter.next(),
                    source_tool="get_recent_configuration_changes",
                    dimension="config_change",
                    statement=(
                        f"Configuration change {row['change_id']} to {row['component']} "
                        f"{minutes:.0f} minutes ago: {row['description']}"
                    ),
                    metrics=row,
                    strength=strength,
                )
            )

    def _mark_echoes(
        self, ctx: IncidentContext, bundle: EvidenceBundle, window_minutes: int
    ) -> None:
        """Separate independent faults from echoes of the primary one.

        When a single PSP fails, every issuer and every region also measures as degraded, because
        all of them route traffic through it. Those segments are real observations but not
        independent causes, and treating them as such is how one provider outage gets diagnosed as
        "multiple concurrent degradations" - which then produces no useful action at all, because no
        single intervention addresses a phantom multi-fault.

        The test is causal rather than a magnitude threshold: remove the primary segment's traffic
        and re-measure. A dimension still degraded without it is a genuine second fault; one that
        recovers was only ever reflecting the first.
        """
        candidates = [
            f
            for f in bundle.findings
            if f.metrics.get("deviation") is not None and f.dimension in _RESIDUAL_DIMENSIONS
        ]
        if len(candidates) < 2:
            return

        primary_segment = self._primary_segment(ctx, candidates)
        if not primary_segment:
            return
        bundle.primary_segment = dict(primary_segment)
        primary_dims = set(primary_segment)

        for finding in candidates:
            segment = finding.metrics.get("segment") or {}
            if segment == primary_segment:
                finding.metrics["independent"] = True
                continue

            # Aliases of the primary (route vs PSP vs gateway, or the method the primary is scoped
            # to) describe the same fault from another angle. They stay valid evidence for it -
            # excluding the primary would simply empty them out, so there is nothing to test. The
            # hypothesis scorer already groups these dimensions together, so keeping them cannot
            # inflate a single fault into several.
            if _aliases_primary(finding.dimension, primary_dims):
                finding.metrics["independent"] = True
                finding.metrics["describes"] = primary_segment
                continue

            # A segment that shares no traffic with the primary cannot be an echo of it. An echo
            # exists because the two overlap - `device=mobile` looks degraded during a UPI outage
            # because UPI is mobile-heavy. Where the two slices disagree on a dimension they both
            # name, they describe disjoint payments, and no amount of the primary's damage can
            # reach the candidate.
            #
            # The residual test cannot see this. It re-measures the candidate's *dimension*, which
            # for `card & ICICI` means the issuer across every method - diluted by ICICI's healthy
            # traffic until it fails the test. That is how the second fault in SCN-MULTI, a
            # 31-point drop on cards, was filed as an echo of a UPI provider outage.
            if _provably_disjoint(segment, primary_segment):
                finding.metrics["independent"] = True
                finding.metrics["disjoint_from_primary"] = dict(primary_segment)
                continue

            residual = ctx.call_tool(
                "get_residual_segment_metrics",
                dimension=finding.dimension,
                exclude_segment=primary_segment,
                minutes=window_minutes,
            )
            if residual is None:
                finding.metrics["independent"] = None
                continue

            value = str(segment.get(finding.dimension))
            row = next(
                (r for r in residual["rows"] if str(r["segment"].get(finding.dimension)) == value),
                None,
            )
            if row is None or row.get("deviation") is None:
                finding.metrics["independent"] = None
                continue

            residual_dev = float(row["deviation"])
            finding.metrics["residual_deviation"] = round(residual_dev, 4)
            z = _deviation_z(
                row["success_rate"], row.get("baseline_success_rate") or 0.0, row["transactions"]
            )
            independent = residual_dev <= -RESIDUAL_INDEPENDENCE_DROP and z <= -MIN_SEGMENT_Z
            # A candidate that lives *inside* the primary is emptied by the exclusion, so the
            # forward test cannot see it. Ask the question the other way round before filing it
            # as an echo.
            if not independent and self._primary_explained_by(
                ctx, finding, primary_segment, window_minutes
            ):
                finding.metrics["independent"] = True
                finding.metrics["explains_primary"] = dict(primary_segment)
                finding.statement += (
                    f" Excluding this segment, {_label(primary_segment)} returns to "
                    f"{finding.metrics['primary_residual_deviation']:+.1%}, so it is this segment "
                    "that degrades the wider one rather than the other way round."
                )
                continue
            finding.metrics["independent"] = independent
            if not independent:
                finding.metrics["echo_of"] = primary_segment
                finding.strength = round(finding.strength * 0.4, 3)
                finding.statement += (
                    f" Excluding {_label(primary_segment)} traffic, this segment returns to "
                    f"{residual_dev:+.1%}, so it reflects that fault rather than an independent one."
                )

    def _primary_explained_by(
        self,
        ctx: IncidentContext,
        finding: Finding,
        primary_segment: dict[str, str],
        window_minutes: int,
    ) -> bool:
        """Whether the primary segment is degraded only *because of* this candidate.

        The forward residual test asks "is this segment still broken once the primary's traffic is
        removed?". That question is unanswerable when the candidate sits inside the primary. An
        issuer inside `payment_method=card` keeps only its non-card traffic once cards are
        excluded, and that traffic is healthy - so a genuine issuer fault is filed as an echo of
        the very method it is degrading, and the diagnosis confidently names the aggregate instead
        of the cause.

        So the test is reversed: remove the candidate and re-measure the primary. If the primary
        recovers it was only ever reporting this candidate, and the candidate is the real fault. A
        true echo fails this test - excluding one region from a card-wide issuer outage leaves
        cards just as broken.
        """
        deviation = float(finding.metrics.get("deviation") or 0.0)
        concentration = float(finding.metrics.get("concentration") or 0.0)
        # Only worth a tool call for a candidate that is substantial in its own right.
        if deviation > -REVERSE_TEST_MIN_DROP or concentration < REVERSE_TEST_MIN_CONCENTRATION:
            return False
        # Only meaningful against a single broad slice; a compound primary is already specific.
        if len(primary_segment) != 1:
            return False
        primary_dim, primary_value = next(iter(primary_segment.items()))
        if primary_dim == finding.dimension:
            return False

        residual = ctx.call_tool(
            "get_residual_segment_metrics",
            dimension=primary_dim,
            exclude_segment=finding.metrics.get("segment") or {},
            minutes=window_minutes,
        )
        if residual is None:
            return False
        row = next(
            (
                r
                for r in residual["rows"]
                if str(r["segment"].get(primary_dim)) == str(primary_value)
            ),
            None,
        )
        if row is None or row.get("deviation") is None:
            return False
        primary_residual = float(row["deviation"])
        finding.metrics["primary_residual_deviation"] = round(primary_residual, 4)
        return primary_residual > -RESIDUAL_INDEPENDENCE_DROP

    def _primary_segment(
        self, ctx: IncidentContext, candidates: list[Finding]
    ) -> dict[str, str]:
        """The most credible culprit, which anchors every independence test.

        The detector's ranked attribution is used when available: it already scores depth,
        concentration and a Wilson lower bound together. Picking the merely *deepest* segment
        instead would hand the anchor to whichever small slice got unlucky, and every subsequent
        independence test would then be measured against noise.
        """
        anomaly = ctx.incident.anomaly
        if anomaly and anomaly.affected_segments:
            return dict(anomaly.affected_segments[0].segment.dimensions)
        best = max(
            candidates,
            key=lambda f: f.strength * abs(float(f.metrics.get("deviation") or 0.0)),
        )
        return dict(best.metrics.get("segment") or {})

    def _correlations(self, bundle: EvidenceBundle) -> list[str]:
        """Cross-signal observations worth stating explicitly to the reasoner."""
        out: list[str] = []
        code = bundle.dominant_error_code
        if code in INFRA_ERRORS and bundle.dominant_error_share >= 0.35:
            out.append(
                f"Dominant failure code {code} is infrastructure-side, not customer-side, "
                "so this is unlikely to be a demand or customer-behaviour effect."
            )
        if code in CUSTOMER_ERRORS and bundle.dominant_error_share >= 0.4:
            out.append(
                f"Failures are dominated by {code}, a customer-side code, which argues against a "
                "provider or routing fault."
            )
        if bundle.latency_shift_ms >= 1500 and code in ("GATEWAY_TIMEOUT", "AUTH_TIMEOUT", "CHECKOUT_CALLBACK_TIMEOUT"):
            out.append(
                "Latency rose sharply alongside timeout-class failures, consistent with a timeout "
                "cascade rather than declines."
            )
        if bundle.recent_config_changes:
            out.append(
                f"{len(bundle.recent_config_changes)} merchant configuration change(s) precede the "
                "degradation and must be considered as a cause."
            )
        mix = max((abs(v) for v in bundle.traffic_composition_shift.values()), default=0.0)
        if mix >= 0.08:
            out.append(
                "Traffic composition moved substantially, so part of the headline drop may be mix "
                "rather than failure."
            )
        return out


def _deviation_index(rows: list[dict[str, Any]], dimension: str) -> dict[str, float]:
    """Map a single dimension's values to their observed deviation, for marginal comparisons."""
    out: dict[str, float] = {}
    for row in rows:
        value = row.get("segment", {}).get(dimension)
        if value is not None and row.get("deviation") is not None:
            out[str(value)] = float(row["deviation"])
    return out


class _Counter:
    """Stable, human-readable finding IDs so evidence can be cited and verified."""

    def __init__(self) -> None:
        self._n = 0

    def next(self) -> str:
        self._n += 1
        return f"F{self._n}"


def segment_dimension_values(segments: list[SegmentStat], dimension: str) -> list[str]:
    return [s.segment.dimensions[dimension] for s in segments if dimension in s.segment.dimensions]
