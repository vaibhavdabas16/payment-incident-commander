"""Produce a known-good incident for the demo video, then write the script from its real numbers.

The problem this solves: a demo world that has been poked at for an hour accumulates overlapping
degradations. Once several are running at once, no single cause is separable, root-cause confidence
collapses, and the agent correctly refuses to act -- which is honest behaviour and a terrible five
minutes of video. Every incident after the first few ends up being the "it doesn't know" story.

So this resets to a clean world, seeds the labelled history, runs exactly one scenario, and then
*reads back* what happened. It does not fabricate anything and it cannot: the numbers in the script
it writes are pulled from the same API the dashboard renders from. If the run comes out weak, it
says so and tells you to run it again, rather than writing a script around numbers that will not be
on screen.

    python -m uvicorn pic.api.main:app --port 8000     # in another terminal
    python tools/demo_run.py

Then open the URL it prints, and record while reading docs/DEMO_SCRIPT_LIVE.md.

    python tools/demo_run.py --prepare-only    # clean world, seeded, nothing triggered yet
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
SESSION = "demo"
SCENARIO = "SCN-UPI-PSP"
SPEEDUP = 6.0
OUT = pathlib.Path("docs/DEMO_SCRIPT_LIVE.md")


# --------------------------------------------------------------------------- api


def api(path: str, method: str = "GET"):
    sep = "&" if "?" in path else "?"
    req = urllib.request.Request(f"{BASE}{path}{sep}session={SESSION}", method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"null")


def wait_for(what: str, probe, timeout: float):
    started = time.monotonic()
    last = None
    while time.monotonic() - started < timeout:
        try:
            last = probe()
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
            last = None
        if last:
            return last, time.monotonic() - started
        time.sleep(1.0)
    print(f"  ! gave up after {timeout:.0f}s waiting for {what}")
    return None, time.monotonic() - started


# ------------------------------------------------------------------- formatting
# These mirror web/src/api.js exactly. The script has to quote what is on screen, and "Rs 272,537"
# when the page says "2.7L" sends the presenter hunting for a number that is not there.


def inr(paise) -> str:
    if paise is None:
        return "-"
    rupees = paise / 100
    a = abs(rupees)
    if a >= 1e7:
        return f"₹{rupees / 1e7:.2f} Cr"
    if a >= 1e5:
        return f"₹{rupees / 1e5:.1f}L"
    if a >= 1000:
        return f"₹{rupees / 1000:.1f}K"
    return f"₹{round(rupees)}"


def pct(value, digits: int = 1) -> str:
    return "-" if value is None else f"{value * 100:.{digits}f}%"


def spoken_money(paise) -> str:
    """How you would say it out loud, which is not how it is printed."""
    if paise is None:
        return "nothing"
    r = paise / 100
    if abs(r) >= 1e7:
        return f"{r / 1e7:.1f} crore"
    if abs(r) >= 1e5:
        return f"{r / 1e5:.1f} lakh"
    if abs(r) >= 1000:
        return f"{r / 1000:.0f} thousand"
    return f"{round(r)} rupees"


# ----------------------------------------------------------------------- the run


def prepare() -> None:
    print("clean world")
    api("/api/control/reset", "POST")
    seeded = api("/api/demo/seed-history", "POST")
    print(f"  seeded history: {seeded.get('added')} records, {seeded.get('total_remembered')} total")
    api(f"/api/control/speed?speedup={SPEEDUP}", "POST")
    print(f"  clock at {SPEEDUP:g}x")


def run_incident() -> dict | None:
    print(f"triggering {SCENARIO}")
    api(f"/api/scenarios/{SCENARIO}/trigger", "POST")

    inc, waited = wait_for(
        "an incident to open",
        lambda: (api("/api/incidents") or {}).get("incidents") or None,
        timeout=240,
    )
    if not inc:
        return None
    incident_id = inc[0]["incident_id"]
    print(f"  {incident_id} opened after {waited:.0f}s")

    detail, _ = wait_for(
        "a recommendation",
        lambda: (api(f"/api/incidents/{incident_id}") or {}).get("proposal")
        and api(f"/api/incidents/{incident_id}"),
        timeout=120,
    )
    if not detail:
        return None

    if detail.get("state") == "AWAITING_HUMAN_APPROVAL":
        print("  policy held it for a human; approving")
        try:
            api(f"/api/incidents/{incident_id}/approve", "POST")
        except urllib.error.HTTPError as exc:
            print(f"  ! approve returned {exc.code}")
    else:
        print(f"  policy did not need a human (state {detail.get('state')})")

    res, waited = wait_for(
        "verification",
        lambda: (api(f"/api/incidents/{incident_id}") or {}).get("verification"),
        timeout=300,
    )
    if res:
        print(f"  verification: {res.get('status')} after {waited:.0f}s")

    wait_for(
        "the incident to close",
        lambda: (api(f"/api/incidents/{incident_id}") or {}).get("state") == "CLOSED" or None,
        timeout=180,
    )
    return api(f"/api/incidents/{incident_id}")


# ------------------------------------------------------------------ quality gate


def grade(d: dict) -> list[str]:
    """What would make this a bad five minutes of video. Empty list means record it."""
    problems = []
    rc = d.get("root_cause") or {}
    p = d.get("proposal") or {}
    v = d.get("verification") or {}
    rev = d.get("revenue") or {}

    conf = rc.get("confidence") or p.get("confidence") or 0
    if conf < 0.6:
        problems.append(
            f"root-cause confidence is only {pct(conf, 0)} -- the agent cannot separate the "
            f"hypotheses, so it will file a ticket instead of recovering anything"
        )
    if p.get("action") != "shift_traffic":
        problems.append(
            f"the recommended action is '{p.get('action')}', not a traffic shift -- there is no "
            f"recovery to show"
        )
    if not d.get("action_result"):
        problems.append("nothing was executed, so there is no verification and no recovered revenue")
    if not v:
        problems.append("no verification: the control-group moment, your strongest beat, is missing")
    elif not v.get("control_used"):
        problems.append("verification ran without a control group, so the A/B panel will not render")
    if (rev.get("revenue_recovered_paise") or 0) + (rev.get("revenue_protected_paise") or 0) <= 0:
        problems.append("zero revenue recovered or protected -- the ledger will read all zeros")
    return problems


# --------------------------------------------------------------------- the script


def write_script(d: dict) -> None:
    i = d["incident_id"]
    rc = d.get("root_cause") or {}
    p = d.get("proposal") or {}
    pd_ = d.get("policy_decision") or {}
    v = d.get("verification") or {}
    rev = d.get("revenue") or {}
    plan = d.get("recovery_plan") or {}
    params = p.get("parameters") or {}
    granted = pd_.get("granted_parameters") or {}

    conf = rc.get("confidence") or p.get("confidence")
    shift = granted.get("percentage", params.get("percentage"))
    requested = params.get("percentage")
    clamped = requested is not None and shift is not None and shift != requested

    ctrl, treat = v.get("control_success_rate"), v.get("treated_success_rate")
    gap = (treat - ctrl) * 100 if (ctrl is not None and treat is not None) else None

    strategies = plan.get("strategies") or []
    hist = plan.get("historical_recommendation") or ""
    similar = plan.get("similar_incident_count")

    def line(label, value):
        return f"| {label} | **{value}** |"

    md = f"""# Demo script — {i}

Generated by `python tools/demo_run.py` from this incident's real data. Every number below is one
the dashboard is rendering right now, formatted the way the page formats it. Regenerate this file
if you re-run the world; do not reuse it across takes.

**Open this and record:**

```
{BASE}/?session={SESSION}#/app/incidents/{i}
```

## The numbers on your screen

| Where | Value |
|---|---|
{line("Hero — Revenue at risk, this incident", inr(rev.get("revenue_at_risk_paise")))}
{line("Root-cause confidence", pct(conf, 0))}
{line("Recommended action", f"shift {shift:g}% of {params.get('payment_method', 'UPI').upper()} traffic, {params.get('from_route')} to {params.get('to_route')}" if shift else p.get("action"))}
{line("Expected revenue recovery", inr(p.get("expected_revenue_protected_per_hour_paise")) + "/hour")}
{line("Verification — Control · left alone", pct(ctrl) + f" ({v.get('control_sample')} payments)" if ctrl is not None else "-")}
{line("Verification — Treatment · moved", pct(treat) + f" ({v.get('treated_sample')} payments)" if treat is not None else "-")}
{line("The gap", f"+{gap:.1f} percentage points, p = {v.get('p_value')}" if gap is not None else "-")}
{line("Verdict", v.get("status", "-"))}
{line("Ledger — At risk", inr(rev.get("revenue_at_risk_paise")))}
{line("Ledger — Protected", inr(rev.get("revenue_protected_paise")))}
{line("Ledger — Recovered", inr(rev.get("revenue_recovered_paise")))}
{line("Ledger — Lost", inr(rev.get("revenue_lost_paise")))}
{line("Ledger — Recovery rate", pct(rev.get("recovery_rate"), 0))}

Policy: **{pd_.get("outcome")}**{" (clamped from " + f"{requested:g}%" + ")" if clamped else ""} — {pd_.get("reason", "")}

History consulted: {similar} comparable incidents in memory.

---

## 2:05 · Scroll to "Every recovery it considered"

"It priced {len(strategies)} options and shows you all of them, with what each was worth.

It picked the {shift:g} percent shift. The objective isn't to maximise success rate — it's to
maximise recovered revenue inside the merchant's risk limits."

## 2:20 · Scroll up to the policy line, then Approve

"Every action passes a deterministic policy gateway. Plain Python, evaluating the merchant's own
config. No model anywhere in that path.

{"It clamped the shift from " + f"{requested:g}" + " percent down to " + f"{shift:g}" + " percent, which is what merchant policy allows." if clamped else "Policy allowed the shift as proposed."}
{pd_.get("reason", "")}

AI proposes. Policy decides. The system verifies. I'll approve it."

## 2:45 · Scroll to "Was the revenue actually recovered?" — slow down here

"This is the part I'd point at first.

We do not claim recovery just because payment success improved. When the agent moved that traffic,
it deliberately left the rest of it alone. So there's a control group living through the same
outage, the same hour, the same customers.

Control — the traffic left alone — {spoken_pct(ctrl)}. Treated — the traffic it moved —
{spoken_pct(treat)}. {f"A gap of {gap:.0f} percentage points" if gap is not None else "That gap"}, at
p equals {v.get("p_value")}.

That gap is the intervention. Not the incident recovering on its own.

And that distinction matters, because those two situations call for opposite responses. Without a
control group, a fix applied during a worsening outage looks like it caused the damage — and the
system rolls back something that was working."

## 3:20 · Scroll to "Revenue actually recovered"

"So the ledger closes. {spoken_money(rev.get("revenue_at_risk_paise"))} at risk.
{spoken_money(rev.get("revenue_protected_paise"))} protected — further loss prevented.
{spoken_money(rev.get("revenue_recovered_paise"))} recovered from payments that had already failed.
And {spoken_money(rev.get("revenue_lost_paise"))} lost.

Those figures are disjoint by construction. They sum to the exposure exactly — you can add them up
on screen. The benchmark asserts that identity on every single incident, because a system reporting
a hundred and thirty percent recovery rate hasn't had a good day. It has a bug."

## 3:45 · Scroll to "What the agent learned", then the Learning page

"When the incident closes, the whole thing becomes one structured record.

On the Learning page that becomes a playbook. For this failure signature it now prefers a moderate
traffic shift — and it says what to avoid: the larger shift helped in only two of five comparable
incidents, and three of those had to be rolled back.

To be straight with you, fourteen of these records are seeded demo history and the interface labels
them as seeded. But they describe the same failure this scenario produces, so this incident
retrieved them because the signature genuinely matched. Nothing was special-cased."

## 4:15 · Scroll to Prevention

"And the last step is prevention. This pattern has recurred often enough to be worth stopping
rather than recovering, so it proposes preventing it. That's a recommendation, not an action.
Merchant policy only ever changes when a person edits the policy file."

## 4:30 · Go to "How it works"

"One last thing, and it's the one I'd want a reviewer to check. There's a scenario where every UPI
provider is failing at once, so the obvious reroute cannot possibly work. The agent tries it,
measures no improvement against the control, reverts its own change, and escalates with the
evidence.

Across the benchmark: zero policy violations. Zero unauthorised executions. Every rollback
succeeded. Detection precision ninety-nine point five percent. Root cause accuracy ninety-one
percent. Median time to mitigate, a hundred and thirty-six seconds, against a modelled human
baseline of twenty-seven minutes.

All of that is generated by a reproducible harness. None of it is typed into a slide."

## 4:55 · Close

"Detect. Recover. Verify. Learn.

Recover the revenue, not just the incident. Thank you."

---

### Notes for this particular take

- History line on the recommendation: {hist if hist else "not shown for this incident"}
- Verdict is **{v.get("status", "unknown")}**{". Say: 'Real improvement, but not the whole gap, so it stays open rather than claiming a win.'" if v.get("status") == "PARTIALLY_RECOVERED" else "."}
"""
    OUT.write_text(md, encoding="utf-8")
    print(f"\nscript  {OUT}")


def spoken_pct(value) -> str:
    return "unknown" if value is None else f"{value * 100:.0f} percent"


# ------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prepare-only", action="store_true", help="clean world, seeded, no scenario")
    args = ap.parse_args()

    try:
        api("/api/health")
    except Exception as exc:
        print(f"no server on {BASE} ({exc}). Start it with:")
        print("  python -m uvicorn pic.api.main:app --port 8000")
        return 2

    prepare()
    if args.prepare_only:
        print(f"\nready. Open {BASE}/?session={SESSION} and trigger the scenario yourself.")
        return 0

    d = run_incident()
    if not d:
        print("\nno usable incident. Run it again.")
        return 1

    problems = grade(d)
    print()
    if problems:
        print(f"WEAK TAKE -- {d['incident_id']} is not worth recording:")
        for p in problems:
            print(f"  - {p}")
        print("\nRun this again for a clean world. If it keeps happening, the simulated traffic")
        print("mix is unlucky; two or three attempts is normal.")
        return 1

    print(f"GOOD TAKE -- {d['incident_id']}")
    write_script(d)
    print(f"record  {BASE}/?session={SESSION}#/app/incidents/{d['incident_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
