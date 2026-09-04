"""Terminal demo.

    python -m pic.demo              the full narrative
    python -m pic.demo --loop       only the closed loop (act two)
    python -m pic.demo --scenario SCN-UPI-PSP

**Act one — can it be trusted with a payment system?** Three incidents. The first is the
autonomous happy path. The second is a failed intervention, and it is the more important of the
two: a system that only ever demonstrates success has demonstrated nothing. There the fallback is
degraded too, so the reroute cannot help — the agent measures that against a concurrent control,
reverts its own change and escalates rather than declaring victory. The third breaks nothing at
all, and the correct answer is to do nothing.

**Act two — does it get better?** Four incidents against one shared memory. The first has no
history and decides on live evidence alone. The second and third can see what happened to the
first. A failed intervention goes into the record as negative evidence, not quietly forgotten. By
the fourth, the system is quoting its own outcomes back as the reason for its decision, and a
recurring-failure pattern has surfaced with a preventive recommendation attached.

Everything printed here comes from the same code path the API and the evaluation harness use.
Nothing is scripted or pre-computed, and every rupee figure is arithmetic over measurements the
agents actually took.
"""

from __future__ import annotations

import argparse
import sys
import time

from .agents.impact import format_inr
from .engine import Engine, EngineConfig
from .llm.base import build_reasoner
from .memory.store import IncidentMemory
from .schemas import ActionType, IncidentRecord, IncidentState

# A fixed simulated cost per agent step. The demo runs several worlds in sequence and the story
# depends on them being comparable; charging measured wall time would make the same seed advance
# different distances depending on how busy the machine is.
DEMO_STEP_COST_S = 1.5

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BLUE = "\033[36m"

WIDTH = 78


def supports_colour() -> bool:
    return sys.stdout.isatty()


def _prepare_stdout() -> bool:
    """Return whether the console can render box drawing, switching it to UTF-8 if possible.

    Windows terminals default to cp1252, which cannot encode the rule and arrow glyphs and raises
    part-way through a line - leaving the demo half printed. Reconfiguring to UTF-8 fixes it where
    supported; where it does not, the ASCII glyph set below keeps the demo readable rather than
    crashing on decoration.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    encoding = getattr(sys.stdout, "encoding", "") or ""
    try:
        "━→⚡".encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


UNICODE_OK = True


def glyph(fancy: str, plain: str) -> str:
    return fancy if UNICODE_OK else plain


class Printer:
    def __init__(self, colour: bool, pace: float) -> None:
        self.colour = colour
        self.pace = pace

    def c(self, text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if self.colour else text

    def rule(self, char: str | None = None) -> None:
        print(self.c((char or glyph("─", "-")) * WIDTH, DIM))

    def header(self, text: str) -> None:
        print()
        heavy = glyph("━", "=")
        self.rule(heavy)
        print(self.c(f" {text}", BOLD))
        self.rule(heavy)

    def stage(self, index: int, name: str, detail: str, tone: str = "") -> None:
        marker = self.c(f"  [{index}] {name}", BOLD)
        print(f"{marker}")
        for line in _wrap(detail, WIDTH - 6):
            print(f"      {self.c(line, tone) if tone else line}")
        time.sleep(self.pace)

    def note(self, text: str) -> None:
        for line in _wrap(text, WIDTH - 6):
            print(self.c(f"      {line}", DIM))

    def kv(self, key: str, value: str) -> None:
        # `ljust` alone does nothing for a key longer than the column, so a long label ran
        # straight into its value: "revenue at risk across themINR 6.2L".
        label = key if len(key) < 26 else key + " "
        print(f"      {self.c(label.ljust(26), DIM)}{value}")


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]


def run_scenario(
    p: Printer,
    scenario_id: str,
    title: str,
    premise: str,
    seed: int,
    auto_approve: bool,
) -> IncidentRecord | None:
    p.header(title)
    p.note(premise)
    print()

    engine = Engine(EngineConfig(seed=seed, reasoner=None))
    engine.warmup(45)

    before = engine.current_metrics()
    print(f"  {p.c('NORMAL OPERATION', GREEN)}")
    p.kv("payment success rate", f"{before['success_rate']:.1%}")
    p.kv("baseline", f"{before['baseline_success_rate']:.1%}")
    p.kv("payments in window", str(before["transactions"]))
    time.sleep(p.pace * 2)

    scenario = engine.trigger(scenario_id)
    print()
    print(f"  {p.c('⚡ INJECTING: ' + scenario.name, YELLOW)}")
    time.sleep(p.pace)

    incident = engine.run_until_incident(max_ticks=40)
    if incident is None:
        print(f"\n  {p.c('No incident opened — the detector judged this within normal variation.', GREEN)}")
        p.note(
            "For a traffic-mix change that is the correct answer: success rate fell because "
            "customers moved to methods that always convert worse, and nothing is broken."
        )
        return None

    print()
    _render(p, engine, incident)

    if incident.state is IncidentState.AWAITING_HUMAN_APPROVAL and auto_approve:
        print()
        print(f"  {p.c(glyph('⏸', '||') + '  POLICY GATEWAY HELD THIS ACTION FOR A HUMAN', YELLOW)}")
        p.note(incident.policy_decision.reason if incident.policy_decision else "")
        p.note("Simulating an on-call engineer clicking Approve in the dashboard…")
        time.sleep(p.pace * 2)
        engine.supervisor.approve(incident, approver="ops_engineer")
        print()
        _render(p, engine, incident, from_stage=6)

    print()
    _summary(p, engine, incident)
    return incident


def _render(p: Printer, engine: Engine, incident: IncidentRecord, from_stage: int = 0) -> None:
    index = 0

    def emit(name: str, detail: str, tone: str = "") -> None:
        nonlocal index
        index += 1
        if index > from_stage:
            p.stage(index, name, detail, tone)

    a = incident.anomaly
    if a:
        emit(
            "DETECTION",
            f"{a.severity.value}: success rate {a.current_value:.1%} against a baseline of "
            f"{a.baseline:.1%} ({a.deviation:+.1%}). z={a.z_score}, confidence {a.confidence}, "
            f"n={a.sample_size}. Method: {' + '.join(a.detection_method)}.",
            RED,
        )

    e = incident.evidence
    if e:
        emit(
            "INVESTIGATION",
            f"{len(e.findings)} findings from {len(e.tools_used)} tools. Dominant failure code "
            f"{e.dominant_error_code} at {e.dominant_error_share:.0%} of failures.",
        )
        if index > from_stage:
            for finding in e.findings[:3]:
                p.note(f"[{finding.finding_id}] {finding.statement}")

    im = incident.impact
    if im:
        emit(
            "BUSINESS IMPACT",
            f"{format_inr(im.revenue_at_risk_per_hour_paise)} per hour at risk; "
            f"{im.transactions_at_risk_per_hour:,} extra failed payments/hour affecting about "
            f"{im.affected_customers_estimate:,} customers/hour.",
            RED,
        )
        if index > from_stage:
            for line in im.calculation[:4]:
                p.note(line)

    rc = incident.root_cause
    if rc:
        emit(
            "ROOT CAUSE",
            f"{rc.most_likely_root_cause} — confidence {rc.confidence:.0%}"
            + (" [AMBIGUOUS]" if rc.ambiguous else ""),
            BLUE,
        )
        if index > from_stage:
            for h in rc.hypotheses[:3]:
                p.note(f"{h.probability:.0%}  {h.cause}")
            for c in rc.contradicting_evidence[:2]:
                p.note(f"against: {c}")

    plan = incident.recovery_plan
    if plan:
        emit(
            "RECOVERY STRATEGY",
            f"{len(plan.strategies)} options priced against {plan.memory_size} remembered "
            f"incidents ({len(plan.similar_incidents)} comparable).",
            BLUE,
        )
        if index > from_stage:
            p.note(plan.historical_recommendation)
            for st in sorted(
                plan.strategies, key=lambda x: x.expected_value_paise, reverse=True
            )[:3]:
                size = f" {st.magnitude:g}%" if st.magnitude is not None else ""
                history = ""
                if st.historical_support and st.historical_support.matched_incidents:
                    stats = st.historical_support.stats
                    history = f", history {stats.helped}/{stats.attempts} helped"
                p.note(
                    f"{st.strategy_id} {st.action.value}{size} -> EV "
                    f"{format_inr(st.expected_value_paise)}, risk {st.risk_score}, "
                    f"p(works) {st.p_success}{history}"
                )

    prop = incident.proposal
    if prop:
        emit(
            "DECISION",
            f"{prop.action.value} {prop.parameters} — expected value "
            f"{format_inr(prop.expected_value_paise)}, protects "
            f"{format_inr(prop.expected_revenue_protected_per_hour_paise)}/hr, risk {prop.risk_score}.",
        )
        if index > from_stage:
            p.note(prop.rationale)

    pd = incident.policy_decision
    if pd:
        tone = GREEN if pd.approved else YELLOW
        emit("POLICY GATEWAY", f"{pd.outcome.value} — {pd.reason}", tone)
        if index > from_stage and pd.requested_parameters != pd.granted_parameters:
            p.note(f"requested {pd.requested_parameters}")
            p.note(f"granted   {pd.granted_parameters}  (clamped by merchant policy)")

    ar = incident.action_result
    if ar:
        emit(
            "EXECUTION",
            f"{ar.action.value} executed via {ar.adapter}: {ar.parameters}"
            + ("" if ar.success else f" — FAILED: {ar.error}"),
            GREEN if ar.success else RED,
        )

    v = incident.verification
    if v:
        tone = GREEN if v.status.value in ("RECOVERED", "PARTIALLY_RECOVERED") else RED
        emit("VERIFICATION", f"{v.status.value} — {v.explanation}", tone)
        if index > from_stage and v.control_used:
            p.note(
                f"Concurrent control: moved traffic {v.treated_success_rate:.1%} "
                f"(n={v.treated_sample}) versus unmoved {v.control_success_rate:.1%} "
                f"(n={v.control_sample}), p={v.p_value}."
            )

    orec = incident.order_recovery
    if orec is not None:
        emit(
            "FAILED-PAYMENT RECOVERY",
            orec.note,
            GREEN if orec.recovered else YELLOW,
        )
        if index > from_stage and orec.recoverable_payments:
            p.note(
                f"{orec.failed_payments:,} payments failed, {orec.recoverable_payments:,} still "
                f"recoverable ({format_inr(orec.recoverable_value_paise)} at face value)."
            )

    rev = incident.revenue
    if rev is not None and rev.measurable:
        emit(
            "REVENUE OUTCOME",
            f"{format_inr(rev.revenue_at_risk_paise)} at risk -> "
            f"{format_inr(rev.revenue_protected_paise)} protected + "
            f"{format_inr(rev.revenue_recovered_paise)} recovered = "
            f"{rev.recovery_rate:.0%} recovery.",
            GREEN if rev.recovery_rate >= 0.5 else YELLOW,
        )
        if index > from_stage:
            for line in rev.calculation:
                p.note(line)

    learning = incident.learning
    if learning is not None:
        size = (
            f" at {learning.intervention_magnitude:g}%"
            if learning.intervention_magnitude
            else ""
        )
        emit(
            "LEARNING",
            f"{learning.actual_action_executed}{size} -> {learning.verification_result}, "
            f"recorded against '{learning.failure_signature.label()}'.",
            BLUE,
        )

    if incident.escalation:
        emit(
            "ESCALATION",
            f"{incident.escalation.reason} "
            f"{glyph(chr(0x2192), '->')} {incident.escalation.recommended_human_action}",
            YELLOW,
        )

    _ = engine


def _summary(p: Printer, engine: Engine, incident: IncidentRecord) -> None:
    p.rule()
    outcome = incident.outcome or incident.state.value
    tone = GREEN if "RECOVER" in outcome or outcome == "NO_ACTION_REQUIRED" else YELLOW
    print(f"  {p.c('OUTCOME: ' + outcome, tone)}")

    if incident.verification and incident.verification.estimated_revenue_protected_per_hour_paise:
        p.kv(
            "revenue protected",
            f"{format_inr(incident.verification.estimated_revenue_protected_per_hour_paise)}/hour",
        )
    p.kv("interventions attempted", str(incident.attempts))
    p.kv("detections correlated", str(incident.correlated_detections))
    p.kv("agent steps", str(len(incident.steps)))
    p.kv("tool calls", str(sum(len(s.tool_calls) for s in incident.steps)))

    if incident.audit:
        print()
        print(f"      {p.c('AUDIT TRAIL', DIM)}")
        for record in incident.audit:
            p.note(
                f"{record.timestamp:%H:%M:%S}  {record.action} → {record.execution_result} "
                f"(approved by {record.approved_by}, policy {record.policy_outcome})"
            )

    weights = engine.simulator.control.snapshot()["route_weights"]["upi"]
    p.kv("final UPI route weights", str(weights))


def run_closed_loop(p: Printer) -> None:
    """Act two: four incidents, one memory, and a system that argues from its own record.

    Each incident runs in its own world - its own simulator, detector and traffic - so nothing is
    carried between them except what was deliberately written to memory when the previous one
    closed. That is the point: if the later incidents behave differently, the only thing that can
    account for it is what the system learned.
    """
    p.header("ACT TWO - THE AGENT GETS BETTER BECAUSE IT REMEMBERS")
    p.note(
        "Four incidents against one shared incident memory. Each runs in its own simulated world, "
        "so the only thing carried forward is what the system chose to record. Watch the "
        "historical evidence appear, change the numbers, and finally produce a prevention "
        "recommendation."
    )
    print()

    memory = IncidentMemory(persist=False)
    acts = [
        ("SCN-UPI-PSP", 7, "1. First time. Nothing in memory; it decides on live evidence alone."),
        ("SCN-UPI-PSP", 20260824, "2. Same failure pattern. Now there is one comparable incident."),
        (
            "SCN-UPI-PSP-BADFALLBACK",
            7,
            "3. Looks the same, but every route is degraded. The fix cannot work. Watch it act, "
            "measure against the control, revert its own change - and record the failure.",
        ),
        ("SCN-UPI-PSP", 991, "4. Same pattern again, now against a real record of what happened."),
    ]

    engine = None
    for scenario_id, seed, premise in acts:
        p.rule()
        print(f"  {p.c(premise, BOLD)}")
        engine = Engine(
            EngineConfig(
                seed=seed,
                # Pinned to the deterministic reasoner. The claim this act makes is that behaviour
                # changed *because of what the system remembered*, and that is only legible if
                # everything else is held constant - a model in the loop would leave a viewer
                # unable to tell learning from sampling. Act one runs with whatever reasoner is
                # configured, which is where the model earns its place.
                reasoner="deterministic",
                memory=memory,
                step_cost_s=DEMO_STEP_COST_S,
            )
        )
        engine.warmup(45)
        engine.trigger(scenario_id)
        incident = engine.run_until_incident(max_ticks=40)
        if incident is None:
            p.note("No incident opened - the detector judged this within normal variation.")
            continue
        if incident.state is IncidentState.AWAITING_HUMAN_APPROVAL:
            engine.supervisor.approve(incident, approver="ops_engineer")

        plan = incident.recovery_plan
        p.kv("incident", incident.incident_id)
        p.kv("memory before deciding", f"{plan.memory_size if plan else 0} incidents")
        # Remembered and *comparable* are different numbers, and the gap is the interesting part:
        # a network-wide UPI failure is not evidence about a single-PSP outage, and the system
        # declining to treat it as such is the retrieval working, not failing.
        p.kv(
            "comparable to this one",
            f"{len(plan.similar_incidents) if plan else 0} (matched on failure signature)",
        )
        if plan:
            for line in _wrap(plan.historical_recommendation, WIDTH - 8):
                print(f"      {p.c(line, BLUE)}")
            for st in sorted(
                (
                    s
                    for s in plan.strategies
                    if s.action is ActionType.SHIFT_TRAFFIC and s.magnitude is not None
                ),
                key=lambda x: x.magnitude or 0,
            ):
                support = st.historical_support
                note = ""
                if support and support.stats and support.matched_incidents:
                    note = (
                        f"  history {support.stats.helped}/{support.stats.attempts} helped, "
                        f"prior {support.efficacy_adjustment:+.2f}"
                    )
                p.kv(
                    f"option {st.action.value} {st.magnitude:g}%",
                    f"EV {format_inr(st.expected_value_paise)}, p(works) {st.p_success}{note}",
                )
        chosen = incident.proposal
        if chosen:
            p.kv(
                "chose",
                f"{chosen.action.value} {chosen.parameters.get('percentage', '')}"
                + (
                    " (clamped by policy)"
                    if incident.policy_decision
                    and incident.policy_decision.granted_parameters
                    != incident.policy_decision.requested_parameters
                    else ""
                ),
            )
        if incident.verification:
            p.kv("verified", f"{incident.verification.status.value}")
        if incident.order_recovery and incident.order_recovery.executed:
            p.kv(
                "failed payments recovered",
                f"{incident.order_recovery.recovered:,} worth "
                f"{format_inr(incident.order_recovery.recovered_value_paise)}",
            )
        if incident.revenue and incident.revenue.measurable:
            r = incident.revenue
            p.kv(
                "revenue",
                f"{format_inr(r.revenue_at_risk_paise)} at risk -> "
                f"{format_inr(r.revenue_protected_paise)} protected + "
                f"{format_inr(r.revenue_recovered_paise)} recovered "
                f"({r.recovery_rate:.0%})",
            )
        if incident.learning:
            p.kv("learned", f"{incident.learning.verification_result} -> memory")
        print()

    if engine is None:
        return

    p.rule()
    print(f"  {p.c('WHAT THE SYSTEM NOW KNOWS', BOLD)}")
    p.note(
        "Every incident above is in here, including the one that had to be rolled back. Note "
        "that it is counted as an attempt that did not help - an agent that remembered only its "
        "successes would keep making the bet that produced them."
    )
    outcomes = memory.get_historical_recovery_outcomes()
    p.kv("incidents remembered", str(outcomes["incidents"]))
    p.kv("revenue at risk across them", format_inr(outcomes["revenue_at_risk_paise"]))
    p.kv("protected", format_inr(outcomes["revenue_protected_paise"]))
    p.kv("recovered", format_inr(outcomes["revenue_recovered_paise"]))
    p.kv("recovery rate", f"{outcomes['recovery_rate']:.0%}")
    for row in outcomes["actions"]:
        p.kv(
            f"{row['action']} {row['magnitude_band']}",
            f"{row['helped']}/{row['attempts']} helped, {row['successes']} fully recovered"
            + (f", {row['rollbacks']} rolled back" if row["rollbacks"] else ""),
        )

    if engine.supervisor.prevention:
        print()
        print(f"  {p.c('PREVENTION RECOMMENDATION', YELLOW)}")
        for rec in engine.supervisor.prevention:
            p.note(rec.pattern)
            for condition in rec.conditions:
                p.note(f"  when {condition}")
            p.kv(
                "proposes",
                f"{rec.proposed_action.value} {rec.proposed_parameters}",
            )
            p.kv("historical loss", format_inr(rec.historical_revenue_lost_paise))
            p.kv("estimated benefit", format_inr(rec.estimated_benefit_paise))
            p.kv("authority", "merchant approval required - policy is unchanged")
    else:
        p.note(
            "No recurring pattern has cleared the evidence bar yet; a pattern needs at least "
            "three comparable incidents before it is worth a merchant's attention."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Payment Incident Commander — live demo")
    parser.add_argument("--pace", type=float, default=0.45, help="Seconds between stages.")
    parser.add_argument("--scenario", default=None, help="Run a single scenario and exit.")
    parser.add_argument(
        "--loop", action="store_true", help="Run only the closed-loop act and exit."
    )
    parser.add_argument("--no-colour", action="store_true")
    args = parser.parse_args()

    global UNICODE_OK
    UNICODE_OK = _prepare_stdout()
    p = Printer(colour=supports_colour() and not args.no_colour, pace=args.pace)

    print()
    print(p.c("  PAYMENT INCIDENT COMMANDER".center(WIDTH), BOLD))
    print(
        p.c(
            f"  detect {glyph(chr(0xb7), '-')} investigate {glyph(chr(0xb7), '-')} quantify "
            f"{glyph(chr(0xb7), '-')} decide {glyph(chr(0xb7), '-')} gate {glyph(chr(0xb7), '-')} "
            f"act {glyph(chr(0xb7), '-')} verify".center(WIDTH),
            DIM,
        )
    )
    print(p.c(f"  reasoner: {build_reasoner().name}".center(WIDTH), DIM))

    if args.loop:
        run_closed_loop(p)
        print()
        return

    if args.scenario:
        run_scenario(p, args.scenario, f"SCENARIO: {args.scenario}", "", seed=7, auto_approve=True)
        return

    run_scenario(
        p,
        "SCN-UPI-PSP",
        "SCENARIO 1 — a UPI provider degrades, and the agent fixes it",
        "A single UPI PSP starts declining collect requests. UPI is the merchant's dominant "
        "payment method, so the headline success rate falls hard. A healthy alternative route "
        "exists, which means this one is genuinely fixable without a human.",
        seed=7,
        auto_approve=True,
    )

    run_scenario(
        p,
        "SCN-UPI-PSP-BADFALLBACK",
        "SCENARIO 2 — the same symptoms, but the fix does not work",
        "This time every UPI provider is degrading together: an upstream rails problem, not a "
        "provider problem. Random variation still makes one PSP look worst, so the evidence points "
        "at a reroute that cannot possibly help. Watch the agent act, measure the result against a "
        "concurrent control, and then undo its own change.",
        seed=7,
        auto_approve=True,
    )

    run_scenario(
        p,
        "SCN-TRAFFIC-MIX",
        "SCENARIO 3 — success rate falls, but nothing is broken",
        "A campaign drives a surge of wallet and netbanking traffic that has always converted "
        "worse. The headline number drops. The correct response is to do nothing, and rerouting "
        "healthy traffic here would add risk while protecting no revenue at all.",
        seed=7,
        auto_approve=False,
    )

    run_closed_loop(p)

    print()
    p.rule("━")
    print(p.c("  Reproduce the benchmark:  python -m pic.evaluation.harness", BOLD))
    print(p.c("  Live dashboard:           uvicorn pic.api.main:app --reload", DIM))
    p.rule("━")
    print()


if __name__ == "__main__":
    main()
