# End to end: one failure, all the way through

The other documents describe the system by part. [ARCHITECTURE](ARCHITECTURE.md) is the component
map, [DECISIONS](DECISIONS.md) is why each part is shaped the way it is, [EVALUATION](EVALUATION.md)
is how well it works and [INTEGRATION](INTEGRATION.md) is how to point it at a real merchant.

This one is the trace. It follows a single payment failure from the moment it enters the system to
the moment a person is handed something to do about it, naming the file and function at every step,
so you can read the code in the order it actually runs rather than in the order it is filed.

Read it with `pic/agents/supervisor.py` open. That file is the spine.

---

## The shape of the thing

```
  payments  ──▶  EventStore  ──▶  Detector  ──▶  Supervisor ──▶ ten agents
                                                      │              │
                                                      │              ▼
                                                      │        ToolRegistry ──▶ read tools
                                                      │              │
                                                      ▼              ▼
                                                PolicyGateway ──▶ write tools ──▶ ControlPlane
                                                      ▲
                                                      │ (never crosses this line)
                                              IncidentMemory ──▶ historical evidence
```

Three things are load-bearing and worth holding in mind the whole way down:

- **The reasoner never executes anything.** It ranks hypotheses and proposes an action. Every path
  from a proposal to a change in the world goes through `PolicyGateway`, which is plain Python
  evaluating the merchant's YAML with no model in it.
- **Agents never read the event store directly.** They call tools, and every call is recorded. That
  indirection is what makes "the agent did not invent this evidence" checkable rather than a claim.
- **Memory informs and never authorises.** History moves one efficacy prior by at most ±0.20 and is
  rendered to a human. It cannot create an action, change a rupee figure, or reach the gateway —
  the arrow above stops where it stops on purpose (ADR-010).

---

## 0. Where payments come from

There are two sources, and **everything downstream of this section is identical for both**.

| | Simulation | Live |
|---|---|---|
| Source | `PaymentSimulator.advance_seconds` | `POST /api/v1/events` → `pic/integration/ingest.py` |
| Clock | `simulator.now` | `datetime.now(timezone.utc)` |
| Waiting | generates the traffic for the interval | actually sleeps |
| Acting | `simulator.control` mutates route weights | your control plane, over HTTP |

Set up in `Engine.__init__` (`pic/engine.py`). The `live` flag picks the clock and the control
plane; the detector, the ten agents, the policy gateway and the tool registry are constructed the
same way either way. The demo, the dashboard and the benchmark all run through this one file — if
the benchmark and the demo used different wiring, neither number would mean anything.

Events land in `EventStore` (`pic/store.py`): a timestamp-sorted list with a parallel epoch array,
so a window slice is a bisect rather than a scan. Retention is 4.5 hours, which is the deepest
lookback any consumer asks for plus a margin.

---

## 1. Detection — three tests have to agree

`Detector.evaluate` (`pic/detection/detector.py`), reached via `IncidentSupervisor.observe`.

Detection runs at two levels, because real incidents arrive in two shapes.

**Headline** catches drops big enough to move the merchant's overall success rate. Three
independent tests must all pass:

1. *Statistical* — a MAD-based robust z-score against a rolling baseline. Robust, so that a
   previous incident sitting in the baseline cannot mask a new one.
2. *Material* — the drop is large enough in absolute and relative terms to be worth someone's night.
3. *Sufficient* — enough transactions in the window to trust the estimate at all.

**Segment** catches what headline monitoring structurally cannot. One issuer failing on cards might
move the overall rate by three points — invisible — while being one of the most expensive incidents
a merchant can have, because card traffic carries a high average order value. So each segment is
tested against *its own* baseline with a two-proportion test and promoted on revenue at risk.

A CUSUM change-point test runs alongside as corroboration. It raises confidence but never opens an
incident alone.

> **Why agreement?** `SCN-TRAFFIC-MIX` is the case this exists for. A campaign drives a surge of
> wallet and netbanking traffic that has always converted worse. The success rate genuinely falls,
> the statistical test genuinely fires, and nothing is broken. Requiring the material test to agree
> is what keeps the system quiet. That restraint is measured, not assumed — see the false-positive
> count in [EVALUATION](EVALUATION.md).

**The baseline freeze.** `mark_degraded_from` excludes known-bad periods from every future
baseline. Without it a long outage is quietly absorbed into "expected": the rolling baseline drifts
down to match the failure, the deviation shrinks to zero, and the detector stops reporting a problem
that is still costing money every minute.

### Correlation: why one fault is one incident

A degradation lasts many monitoring cycles and the detector fires on every one. `_correlate` folds
repeats into the incident already tracking them, matching on **overlap** between affected segments
rather than on the worst-hit segment being identical — during a live degradation the ranking
reshuffles between cycles, so exact matching recognises almost nothing.

Without this you get an incident storm: dozens of duplicates for one fault, revenue-at-risk summed
over all of them, and the merchant's hourly action budget spent on the same problem until every
later incident escalates for rate limiting.

---

## 2. The loop

`IncidentSupervisor.run_incident` is an explicit finite state machine, not an autonomous agent loop
(ADR-005). Each state has exactly one handler:

```
DETECTED ──▶ INVESTIGATING ──▶ IMPACT_ASSESSED ──▶ DIAGNOSING ──▶ DECIDING
                                                                     │
                                                                     ▼
                                                              POLICY_REVIEW
                                                                     │
                        ┌────────────────────────────┬───────────────┤
                        ▼                            ▼               ▼
             AWAITING_HUMAN_APPROVAL          ESCALATED         EXECUTING
                                                                     │
                                                                     ▼
                                                                 VERIFYING
                                                                     │
                                              ┌──────────────────────┤
                                              ▼                      ▼
                                        ROLLING_BACK             RESOLVED
                                              │                      │
                                              ▼                      ▼
                                         ESCALATED  ──────────▶  LEARNING ──▶ CLOSED
```

You can read every transition in one `for` loop of about forty lines. That is the point of choosing
a state machine: the set of things that can happen next is finite and visible, rather than emergent
from a prompt.

---

## 3. Investigation — gather evidence, and don't be fooled by echoes

`InvestigationAgent` (`pic/agents/investigation.py`) pulls evidence through read-only tools:
segment comparisons, error-code distributions, recent config changes, route health.

Its hardest job is the **residual test**. When UPI is broken and `psp_axis` carries most of the UPI
traffic, `psp_axis` looks broken too. The agent removes the already-identified culprit's traffic
from both windows and asks whether the second segment *still* looks degraded. If it recovers once
UPI is excluded, it was an echo, not a second fault.

That is what `EventStore.segment_stats(..., exclude=...)` exists for, and why the aggregation tests
cover the `exclude` path specifically.

Every finding gets an id. The Root Cause Agent may only cite these ids — it cannot assert a fact
that no tool returned.

---

## 4. Impact — price it before deciding anything

`ImpactAgent` (`pic/agents/impact.py`).

Revenue at risk is computed over a **disjoint partition** of traffic via
`EventStore.union_metric_window`. Summing per-segment estimates double-counts, because
`psp=psp_axis` and `route_id=route_A` frequently describe the same payments. The union counts each
payment once, which is the only way to get a number that is not inflated.

Money is integer paise everywhere (ADR-008), and the agent records its arithmetic as a list of
steps that the dashboard renders verbatim. If impact cannot be computed the supervisor escalates
rather than guessing — **never guess at money.**

---

## 5. Root cause — the reasoner re-ranks, it does not invent

`RootCauseAgent` (`pic/agents/root_cause.py`).

The agent scores hypotheses from the evidence deterministically first. The reasoner
(`pic/llm/`, Gemini or the deterministic fallback) is then allowed to **re-rank** that list and
explain it — not to add a cause, and not to cite a finding that does not exist. Anything it returns
outside the closed catalogue is dropped.

Ground truth is physically unreachable from the tool registry (ADR-007), and a test enforces it by
scanning the tool modules. Without that, diagnosis accuracy would be theatre.

If no hypothesis fits, the supervisor does **not** escalate immediately. Early on this usually means
detection fired on a real but still-weak signal. It waits one observation window, re-runs detection
so the segment attribution is fresh, and tries again — up to `MAX_DIAGNOSIS_RETRIES`. Re-investigating
against a stale anomaly would produce new evidence interpreted through an out-of-date picture of
what is broken.

---

## 6a. Recovery strategy — what could we do, and what happened last time we did it

`RecoveryStrategyAgent` (`pic/agents/strategy.py`) produces the closed set of options and prices
each one. A traffic shift is priced at several magnitudes, not one, because that is what lets
history argue about *size* and not only about *action*.

For each candidate it queries `IncidentMemory` for how that action performed at that magnitude
under a similar failure signature, and blends the observed rate into the action's efficacy prior:

```python
blended    = (4 * prior + n * observed) / (4 + n)
adjustment = clamp(blended - prior, ±0.20)
```

One past outcome barely moves it. A dozen consistent ones move it to the cap. Nothing moves it
further. The adjusted prior feeds expected value, and that is the whole mechanism by which learning
changes behaviour — six lines of arithmetic, no model.

The same statistics are pulled through the tool registry as `get_action_outcomes`, so the claim
"nine of eleven comparable incidents recovered" appears in the audit trail as a recorded tool call
rather than as an assertion.

Two properties are easy to miss and both matter:

- **A partial recovery is evidence the action helps.** It is reported separately as a partial, but
  scoring it as a failure would teach the system that a control-verified improvement does not work.
  Anything that had to be rolled back never counts as having helped.
- **The downside of a shift grows faster than its benefit.** Benefit is linear in the traffic moved;
  the destination-degradation risk is charged a surcharge for every point above the default size.
  Without that, the largest permitted shift is trivially optimal every time and history could never
  argue for a smaller one.

## 6b. Decision — expected value, and "do nothing" is a real answer

`DecisionAgent` (`pic/agents/decision.py`) chooses one of the priced options by expected value:
revenue protected, weighted by the (history-adjusted) probability the fix works, against a risk
score. It does not generate options and it does not price them — that separation is why it cannot
invent an option that was never priced.

`NO_ACTION` is a first-class proposal and reaching it is a **success**, not a failure. It is the
correct path for a traffic-mix change, where rerouting healthy traffic would add risk and protect
no revenue at all.

---

## 7. The policy gateway — the part with no model in it

`PolicyGateway.evaluate` (`pic/policies/gateway.py`). This is the only path from a proposal to an
executed action (ADR-002).

Three rules govern it:

- **Most restrictive wins.** Every rule is evaluated, never short-circuited, so the decision records
  every constraint that applied rather than the first one hit. An operator reading the audit log
  needs all of it.
- **Clamp before refusing.** A proposal that overshoots a numeric limit is reduced to the limit.
  Refusing outright would leave a live incident unmitigated because the agent was slightly too
  ambitious.
- **Unknown means no.** An action absent from the merchant's policy is denied. New capabilities are
  granted explicitly, never inherited.

It enforces the destination-health floor, the cumulative-shift ceiling, the per-incident attempt
cap, a cooling-off period after a failure, and the hourly action budget.

The outcome is one of `APPROVE`, `APPROVE_WITH_CLAMP`, `REQUIRE_APPROVAL` or `DENY`. On
`REQUIRE_APPROVAL` the incident moves to `AWAITING_HUMAN_APPROVAL` **and a human is told** —
pausing behind a guardrail without telling anyone is just an incident sitting unattended.

---

## 8. Action — the only agent that can write

`ActionAgent` (`pic/agents/action.py`).

The gate is in `ToolRegistry.call` (`pic/tools/registry.py`), and it is worth reading because it is
six lines that carry a lot of the system's safety:

```python
if spec.write:
    if approval is None or not approval.approved:
        raise PolicyViolation(...)
    if approval.action.value != name:
        raise PolicyViolation(...)
```

A write tool cannot be invoked without an approving decision, and the decision must name **that
exact action**. An approval to shift traffic is not an approval to disable a payment method. This is
structural: there is no prompt to talk past.

Every write tool declares its `inverse`, which is what makes the rollback in step 10 possible.

Some actions — notifying the merchant, filing a ticket, watching more closely — never change payment
behaviour. Running a recovery test on those would score them as failed interventions and burn a
retry, when the system has correctly concluded the fix belongs to a human. Those hand over directly
instead of pretending to verify.

---

## 9. Verification — against a control group, not against yesterday

`VerificationAgent` (`pic/agents/verification.py`). This is the step the project exists for.

The supervisor **genuinely waits** first (`clock.wait`) — in simulation by generating the traffic
for the interval, live by actually sleeping while other requests ingest real payments. Verifying an
intervention that has not had time to take effect is worthless.

Then it measures the treated traffic against a **concurrent control group that was deliberately
left alone**, with a two-proportion test (ADR-009). Comparing before-and-after would credit the fix
for an incident that recovered on its own. A control group is the only way to tell those apart.

The verdict is one of `RECOVERED`, `PARTIALLY_RECOVERED`, `FAILED`, `REGRESSED` or `INCONCLUSIVE`.
A partial recovery is reported as partial and the incident stays open — it is never rounded up to a
success.

---

## 10. Rollback and handover — the hard part

When verification says the action did not help, `_step_rollback` calls the inverse of the write tool
that was executed, and the incident goes to a human.

`EscalationAgent` (`pic/agents/escalation.py`) writes the handover. The thing to notice is what it
refuses to say. Not a category like "no effective action" — but the action it tried, the two success
rates it compared, and the one thing a person should do next.

And then it gives them something to press. `escalation.next_steps` are real, server-validated moves:

| Step | Endpoint | What it does |
|---|---|---|
| *Look again now* | `POST /incidents/{id}/retry` | Re-diagnoses against the traffic that has arrived since |
| *I have this* | `POST /incidents/{id}/acknowledge` | Records who took it; not a resolution, an owner |
| *Run it anyway* | `POST /incidents/{id}/override` | Runs what policy refused, on a named person's authority |

`override` is deliberately narrow. The supervisor decides which refusals are overridable — rules
expressing *uncertainty* can be overridden, rules that exist to prevent *harm* cannot — so the scope
cannot be widened by calling a different endpoint. An overridden action is still measured, and still
reverts itself if it did not help.

`SCN-UPI-PSP-BADFALLBACK` is the scenario built to exercise all of this: every UPI provider is
failing together, so the obvious fix — reroute to a healthier provider — cannot possibly work. The
system tries, measures, fails, reverts, and explains. **Acting is easy. Noticing the action did not
help, undoing it, and leaving a person something better than a paragraph of advice is the hard part.**

---

## 10b. Recovering the payments already lost

Fixing the routing stops further loss; it does nothing for the payments that failed while it was
broken. `OrderRecoveryAgent` (`pic/agents/recovery.py`) goes after those, once, after the
infrastructure question is settled — and not at all if the intervention had to be reverted, because
chasing customers back into a payment stack that is still broken is not a recovery.

`pic/recovery/orders.py` decides what is recoverable by three rules, each of which stops the system
promising money it will not get: the failure has to be the kind a second attempt can clear (a
gateway timeout, not an issuer decline); the order must not have already succeeded on its own; and
the recovery probability is measured from retry outcomes already in the store rather than assumed.

It is a policy-gated write like any other. Merchant policy says how many orders may be chased and
by what means — `retry` and `alternate_route` happen inside the payment stack, `payment_link`
reaches the merchant's customer — and a campaign requiring a method the merchant withheld is
clamped down to the ones permitted rather than refused outright.

## 10c. Learning — the part that makes the next incident different

`IncidentSupervisor._learn` runs while the incident is in `LEARNING`, for **every** closed incident:
resolved, escalated, rolled back or acknowledged.

1. `pic/revenue.py` turns per-hour rates into what the incident actually cost and saved. Three
   disjoint figures — protected (forward-looking), recovered (backward-looking), lost (the
   remainder) — so `at_risk = protected + recovered + lost` holds by construction (ADR-012).
2. `pic/memory/build.py` reduces the incident to one typed `IncidentOutcomeRecord`. Note that it is
   handed the executed action explicitly: a rollback clears `incident.action_result`, and reading
   that field at face value here would erase every failure from memory while keeping every success.
3. `pic/memory/patterns.py` re-mines recurring failures and emits `PreventionRecommendation`s.

A recommendation is a **document**. It has no path into the pipeline, and accepting one records who
accepted it and changes nothing — merchant policy is a file a person owns and diffs (ADR-011).

## 11. What reaches the screen

`pic/api/main.py` serves the dashboard and gives **every visitor their own simulation** — one shared
engine meant one person's injected scenario degraded everyone else's payments and their Reset wiped
everyone else's incidents.

- REST endpoints return `model_dump` of the same typed contracts the agents produced. Nothing on
  screen is written by hand.
- `GET /api/incidents/{id}/trace` (`pic/api/trace.py`) is the explainable trace: observation,
  evidence, hypothesis, historical evidence, candidate strategies, decision, policy check, action,
  control group, result, revenue, failed-payment recovery, learning. Each stage carries the
  structured evidence behind it, so every claim on screen is one click from the thing that produced
  it. It is defined once, in Python, next to the objects it describes — a second definition in
  JavaScript would drift, and the half that drifted would be the half a person is reading.
- A WebSocket streams lifecycle events, with a replay buffer for clients joining mid-incident.
- The simulation loop advances only the worlds someone is actually watching. A world with nobody
  looking at it stops, because detection costs real CPU and advancing an abandoned session steals
  time from a live one.

The React app (`web/src/`) polls on two cadences: live metrics at 1.5s, and segment health at 6s
because it describes a five-minute window and refetching it faster only redraws identical bars.

---

## Where to start reading

| If you want to understand… | Read |
|---|---|
| The whole flow | `pic/agents/supervisor.py` — start at `run_incident` |
| How everything is wired | `pic/engine.py` |
| Why detection is not an LLM | `pic/detection/detector.py`, then ADR-001 |
| The safety argument | `pic/policies/gateway.py` and `ToolRegistry.call`, then ADR-002 |
| What is actually guaranteed | `tests/test_safety_invariants.py` |
| How learning works, and what it cannot do | `pic/memory/store.py`, `pic/agents/strategy.py`, then ADR-010 |
| Why the money adds up | `pic/revenue.py`, then ADR-012 |
| Whether the numbers are real | `pic/evaluation/harness.py`, then [EVALUATION](EVALUATION.md) |

```bash
python -m pic.demo                  # the full narrative: three incidents, then the closed loop
python -m pic.demo --loop           # just the closed loop: four incidents, one memory
python -m pic.evaluation.harness    # the benchmark every published number comes from
python -m pytest -q                 # 135 tests
```
