"""Verification Agent.

Independently observes whether the intervention actually worked. It does not trust the Action
Agent's report, and it does not compare two percentages and call the larger one a win.

**Why before/after is not enough.** A payment incident is usually still worsening when the agent
acts — degradations ramp rather than step. Comparing the window before the action against the
window after it therefore measures the incident's own trajectory as much as the intervention, and
a genuinely helpful fix applied during a worsening outage looks like it caused the damage. Acting
on that reading is actively harmful: the system rolls back a working intervention and escalates.

**So the primary test is a concurrent control.** A traffic shift moves part of a segment to a new
route and leaves the rest where it was, which is a natural A/B: the traffic still on the old route
is a control group living through the same ramp, the same hour and the same customers. Comparing
treated against control isolates the effect of the intervention from everything else happening at
the same time, and lets the agent distinguish *"my action hurt"* from *"the incident got worse"* —
two situations that call for opposite responses.

Where no control group exists (a config rollback affects everyone at once), the agent falls back to
before/after, anchored to the observed degraded state rather than to a window straddling onset, and
says so in its explanation.

Recovery still requires three things at once (ADR-009):

1. a statistically significant improvement (one-sided two-proportion z-test),
2. an absolute improvement large enough to matter operationally,
3. enough post-action volume to have measured anything at all.

Fail any of them and the verdict is `INCONCLUSIVE` or `FAILED`, never `RECOVERED`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..config import settings
from ..detection.statistics import two_proportion_ztest
from ..schemas import (
    ActionType,
    AgentResult,
    IncidentState,
    VerificationResult,
    VerificationStatus,
)
from .base import Agent, IncidentContext
from .impact import format_inr

# Minimum payments on each side before a control comparison is trusted.
MIN_CONTROL_SAMPLE = 30


class VerificationAgent(Agent):
    name = "verification"
    state = IncidentState.VERIFYING

    # Set per run: the shortfall against baseline that remains after the intervention.
    baseline_gap: float = 0.0

    def run(self, ctx: IncidentContext) -> AgentResult:
        incident = ctx.incident
        action = incident.action_result
        anomaly = incident.anomaly
        if anomaly is None:
            return AgentResult(ok=False, summary="no anomaly to verify", error="missing_anomaly")

        cfg = settings.verification
        executed_at = ctx.scratch.get("action_executed_at") or (
            action.executed_at if action else ctx.now
        )

        # The "before" state is the degraded state we measured, never a window reaching back into
        # healthy pre-incident traffic — that would compare a fully degraded "after" against a
        # mostly healthy "before" and manufacture a regression out of the ramp alone.
        before_start = max(
            anomaly.window_start, executed_at - timedelta(seconds=cfg.observation_seconds)
        )
        before = ctx.store.metric_window(before_start, executed_at)
        after = ctx.store.metric_window(executed_at, ctx.now)

        baseline_rate = anomaly.baseline
        improvement = after.success_rate - before.success_rate
        gap = max(1e-9, baseline_rate - before.success_rate)

        # How far the merchant's overall success rate still sits below baseline, right now.
        self.baseline_gap = max(0.0, baseline_rate - after.success_rate)

        control = self._control_comparison(ctx, executed_at, ctx.now)
        side_effects = self._side_effects(ctx, executed_at, ctx.now)

        status, explanation, caused_harm, recovery_ratio = self._classify(
            cfg=cfg,
            before=before,
            after=after,
            improvement=improvement,
            gap=gap,
            control=control,
            side_effects=side_effects,
        )

        revenue_protected = self._revenue_protected(ctx, after, control, improvement, status)

        result = VerificationResult(
            incident_id=incident.incident_id,
            status=status,
            before_success_rate=round(before.success_rate, 4),
            after_success_rate=round(after.success_rate, 4),
            improvement=round(improvement, 4),
            baseline_success_rate=round(baseline_rate, 4),
            recovery_ratio=round(recovery_ratio, 3),
            p_value=round(control["test"].p_value if control else 1.0, 5),
            statistically_significant=bool(control and control["test"].significant),
            before_sample=before.total,
            after_sample=after.total,
            control_used=control is not None,
            treated_success_rate=round(control["treated_rate"], 4) if control else None,
            control_success_rate=round(control["control_rate"], 4) if control else None,
            treated_sample=control["treated_total"] if control else 0,
            control_sample=control["control_total"] if control else 0,
            caused_harm=caused_harm,
            estimated_revenue_protected_per_hour_paise=revenue_protected,
            side_effects=side_effects,
            rollback_recommended=caused_harm,
            explanation=explanation,
        )

        if control:
            summary = (
                f"{status.value}: treated {control['treated_rate']:.1%} vs control "
                f"{control['control_rate']:.1%} (p={control['test'].p_value:.3f}, "
                f"n={control['treated_total']}/{control['control_total']})"
            )
        else:
            summary = (
                f"{status.value}: {before.success_rate:.1%} -> {after.success_rate:.1%} "
                f"({improvement:+.1%}, n={after.total})"
            )
        return AgentResult(ok=True, summary=summary, output=result)

    # -------------------------------------------------------------- control

    def _control_comparison(
        self, ctx: IncidentContext, start: datetime, end: datetime
    ) -> dict[str, Any] | None:
        """Compare traffic moved by the intervention against traffic left behind.

        Only a traffic shift produces a usable control group. Disabling a method or rolling back a
        config changes the experience for everyone at once, leaving nothing to compare against.
        """
        action = ctx.incident.action_result
        if action is None or action.action is not ActionType.SHIFT_TRAFFIC:
            return None

        to_route = action.parameters.get("to_route")
        from_route = action.parameters.get("from_route")
        method = action.parameters.get("payment_method")
        if not to_route or not from_route:
            return None

        base: dict[str, str] = {"payment_method": method} if method else {}
        treated = ctx.store.metric_window(start, end, {**base, "route_id": to_route})
        control = ctx.store.metric_window(start, end, {**base, "route_id": from_route})

        if treated.total < MIN_CONTROL_SAMPLE or control.total < MIN_CONTROL_SAMPLE:
            return None

        test = two_proportion_ztest(
            control.successes,
            control.total,
            treated.successes,
            treated.total,
            alpha=settings.verification.significance_level,
        )
        return {
            "treated_rate": treated.success_rate,
            "control_rate": control.success_rate,
            "treated_total": treated.total,
            "control_total": control.total,
            "diff": treated.success_rate - control.success_rate,
            "test": test,
            "to_route": to_route,
            "from_route": from_route,
            "moved_fraction": float(action.parameters.get("percentage", 0)) / 100.0,
        }

    # ------------------------------------------------------------- classify

    def _classify(
        self,
        *,
        cfg,
        before,
        after,
        improvement: float,
        gap: float,
        control: dict[str, Any] | None,
        side_effects: list[str],
    ) -> tuple[VerificationStatus, str, bool, float]:
        if after.total < cfg.min_post_sample or before.total == 0:
            return (
                VerificationStatus.INCONCLUSIVE,
                (
                    f"Only {after.total} payments observed since the intervention, below the "
                    f"{cfg.min_post_sample} needed to measure a change. Holding the incident open "
                    "rather than guessing."
                ),
                False,
                0.0,
            )

        if control is not None:
            diff = control["diff"]
            significant = control["test"].significant

            # Harm is judged against the control, not against the past. Traffic we moved doing
            # measurably worse than traffic we left behind is the only evidence that the
            # intervention itself is responsible.
            if diff <= -cfg.regression_threshold and control["test"].p_value > 0.5:
                return (
                    VerificationStatus.REGRESSED,
                    (
                        f"Traffic moved to {control['to_route']} is converting at "
                        f"{control['treated_rate']:.1%} against {control['control_rate']:.1%} for "
                        f"traffic left on {control['from_route']} — the intervention is making "
                        "things worse. Rolling back."
                    ),
                    True,
                    0.0,
                )

            if not significant or diff < cfg.min_meaningful_improvement:
                return (
                    VerificationStatus.FAILED,
                    (
                        f"Traffic moved to {control['to_route']} converts at "
                        f"{control['treated_rate']:.1%} versus {control['control_rate']:.1%} on the "
                        f"unmoved control ({diff:+.1%}, p={control['test'].p_value:.3f}). That is "
                        "not a meaningful improvement, so the intervention did not work."
                    ),
                    False,
                    0.0,
                )

            # The shift only moved part of the segment, so the recovery it can deliver is the
            # per-payment gain scaled by the share actually moved.
            realised = diff * control["moved_fraction"]
            recovery_ratio = max(0.0, realised / gap)
            detail = (
                f"Traffic moved to {control['to_route']} is converting at "
                f"{control['treated_rate']:.1%} against {control['control_rate']:.1%} for the "
                f"{control['control_total']} payments left on {control['from_route']} "
                f"({diff:+.1%}, p={control['test'].p_value:.3f}). Measured against a concurrent "
                "control, so the incident's own trajectory cannot account for it."
            )
            if side_effects:
                return (
                    VerificationStatus.PARTIALLY_RECOVERED,
                    detail + " Side effects need review: " + "; ".join(side_effects),
                    False,
                    recovery_ratio,
                )

            # A working intervention is not the same as a resolved incident. The control proves the
            # action helped the traffic it moved; whether the merchant is whole again is a separate
            # question, and the honest test is what the overall success rate is doing now. Closing
            # on the control alone would mark an incident recovered while payments were still
            # failing at 74% - and worse, would let the frozen baseline start absorbing the outage.
            residual_gap = self.baseline_gap
            if residual_gap <= cfg.min_meaningful_improvement:
                return (VerificationStatus.RECOVERED, detail, False, recovery_ratio)
            return (
                VerificationStatus.PARTIALLY_RECOVERED,
                detail
                + f" Overall success rate is still {residual_gap:.1%} below baseline, so the "
                "incident stays open.",
                False,
                recovery_ratio,
            )

        # ---- no control group available: fall back to before/after ----
        test = two_proportion_ztest(
            before.successes, before.total, after.successes, after.total, alpha=cfg.significance_level
        )
        recovery_ratio = max(0.0, improvement / gap)
        caveat = (
            " Measured before/after because this action left no control group, so an incident that "
            "worsened independently could affect this reading."
        )

        if improvement <= -cfg.regression_threshold:
            return (
                VerificationStatus.REGRESSED,
                f"Success rate fell a further {abs(improvement):.1%} after the intervention." + caveat,
                True,
                0.0,
            )
        if not test.significant or improvement < cfg.min_meaningful_improvement:
            return (
                VerificationStatus.FAILED,
                (
                    f"Change of {improvement:+.1%} (p={test.p_value:.3f}) is not distinguishable "
                    "from normal variation, so the intervention did not work." + caveat
                ),
                False,
                recovery_ratio,
            )
        if side_effects:
            return (
                VerificationStatus.PARTIALLY_RECOVERED,
                f"Success rate improved {improvement:+.1%}, but side effects need review: "
                + "; ".join(side_effects),
                False,
                recovery_ratio,
            )
        if recovery_ratio >= cfg.full_recovery_ratio:
            return (
                VerificationStatus.RECOVERED,
                f"Success rate improved {improvement:+.1%}, closing {recovery_ratio:.0%} of the gap "
                f"back to baseline (p={test.p_value:.3f})." + caveat,
                False,
                recovery_ratio,
            )
        if recovery_ratio >= cfg.partial_recovery_ratio:
            return (
                VerificationStatus.PARTIALLY_RECOVERED,
                f"Success rate improved {improvement:+.1%}, closing {recovery_ratio:.0%} of the gap "
                "to baseline. Real but incomplete; the incident stays open." + caveat,
                False,
                recovery_ratio,
            )
        return (
            VerificationStatus.FAILED,
            f"Improvement of {improvement:+.1%} recovered only {recovery_ratio:.0%} of the gap to "
            "baseline, too little to call the incident mitigated." + caveat,
            False,
            recovery_ratio,
        )

    # -------------------------------------------------------------- effects

    def _side_effects(self, ctx: IncidentContext, start: datetime, end: datetime) -> list[str]:
        """Damage the intervention itself may have caused."""
        out: list[str] = []
        action = ctx.incident.action_result
        if action is None or action.action is not ActionType.SHIFT_TRAFFIC:
            return out

        destination = action.parameters.get("to_route")
        if not destination:
            return out

        window = ctx.store.metric_window(start, end, {"route_id": destination})
        if window.total < 40:
            return out

        # A destination taking on load and then degrading is the specific harm a traffic shift can
        # cause, so it is checked explicitly rather than left to the headline number.
        if window.success_rate < 0.80:
            out.append(
                f"destination route {destination} is now at {window.success_rate:.1%} success "
                f"on {window.total} payments"
            )

        baseline = ctx.detector.baseline(start)
        if baseline.windows:
            reference = ctx.store.metric_window(
                baseline.start, baseline.end, {"route_id": destination}
            )
            if reference.total >= 40 and window.p95_latency_ms > reference.p95_latency_ms * 1.8:
                out.append(
                    f"p95 latency on {destination} rose from {reference.p95_latency_ms:.0f}ms to "
                    f"{window.p95_latency_ms:.0f}ms after the shift"
                )
        return out

    def _revenue_protected(
        self,
        ctx: IncidentContext,
        after,
        control: dict[str, Any] | None,
        improvement: float,
        status: VerificationStatus,
    ) -> int:
        """Revenue the intervention is protecting, per hour.

        Valued from the control comparison where one exists, because that is the part of the
        improvement actually attributable to the action. An intervention that made things worse
        protected nothing — reporting a negative figure as "protected" would be misleading.
        """
        if status in (VerificationStatus.REGRESSED, VerificationStatus.FAILED, VerificationStatus.INCONCLUSIVE):
            return 0
        if after.total == 0:
            return 0
        hours = (after.end - after.start).total_seconds() / 3600.0
        if hours <= 0:
            return 0

        avg_value = (after.gmv_paise + after.failed_gmv_paise) / after.total
        attempts_per_hour = after.total / hours

        if control is not None:
            realised = max(0.0, control["diff"] * control["moved_fraction"])
        else:
            realised = max(0.0, improvement)
        _ = ctx
        return int(realised * attempts_per_hour * avg_value)


def describe_outcome(result: VerificationResult) -> str:
    """One-line operator summary used by the dashboard and incident memory."""
    if result.status is VerificationStatus.RECOVERED:
        return (
            f"Recovered: {result.before_success_rate:.1%} -> {result.after_success_rate:.1%}, "
            f"protecting {format_inr(result.estimated_revenue_protected_per_hour_paise)}/hour."
        )
    if result.status is VerificationStatus.PARTIALLY_RECOVERED:
        return (
            f"Partially recovered: {result.recovery_ratio:.0%} of the gap to baseline closed."
        )
    if result.status is VerificationStatus.REGRESSED:
        return "Intervention caused harm; rolled back."
    if result.status is VerificationStatus.INCONCLUSIVE:
        return f"Inconclusive: only {result.after_sample} payments observed after the action."
    return "Intervention did not produce a measurable improvement."
