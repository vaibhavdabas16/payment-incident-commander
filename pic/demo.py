"""Terminal demo: two live incidents, one that works and one that does not.

    python -m pic.demo

Scenario 1 is the autonomous happy path. Scenario 2 is a failed intervention, and it is the more
important of the two: a system that only ever demonstrates success has demonstrated nothing about
whether it can be trusted with a payment system. In the second run the fallback is degraded too, so
the reroute cannot help — the agent measures that against a concurrent control, reverts its own
change and escalates rather than declaring victory.

Everything printed here comes from the same code path the API and the evaluation harness use.
Nothing is scripted or pre-computed.
"""

from __future__ import annotations

import argparse
import sys
import time

from .agents.impact import format_inr
from .engine import Engine, EngineConfig
from .llm.base import build_reasoner
from .schemas import IncidentRecord, IncidentState

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
        print(f"      {self.c(key.ljust(26), DIM)}{value}")


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Payment Incident Commander — live demo")
    parser.add_argument("--pace", type=float, default=0.45, help="Seconds between stages.")
    parser.add_argument("--scenario", default=None, help="Run a single scenario and exit.")
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

    print()
    p.rule("━")
    print(p.c("  Reproduce the benchmark:  python -m pic.evaluation.harness", BOLD))
    print(p.c("  Live dashboard:           uvicorn pic.api.main:app --reload", DIM))
    p.rule("━")
    print()


if __name__ == "__main__":
    main()
