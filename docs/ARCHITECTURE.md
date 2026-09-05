# Payment Incident Commander — Architecture

> An autonomous payment reliability and revenue recovery agent. It detects payment failures,
> understands their cause, chooses the safest recovery strategy, executes it inside merchant-defined
> guardrails, experimentally verifies whether revenue was actually recovered, learns from the
> outcome, and uses that knowledge to recover better next time and to argue for prevention.

Everything in `pic/` implements what is specified here. For the index of which file does what — including the dashboard, screen by screen — see [PROJECT_MAP.md](PROJECT_MAP.md).

The system is a **closed loop**:

```
OBSERVE -> UNDERSTAND -> INTERVENE -> MEASURE -> LEARN -> PREVENT -> OBSERVE AGAIN
```

The first four steps are the incident response. The last two are what make the fifth incident
better handled than the first, and they are the reason the memory subsystem exists. Section 9 is
about how learning changes behaviour and, more importantly, about the things it is structurally
incapable of changing.

---

## 1. Operating principle

The system is an **explicit state machine**, not an autonomous LLM loop. The supervisor advances
an incident through fixed states. Each state is owned by exactly one agent, consumes a typed
input, and emits a typed output. An agent that fails or times out degrades the incident to a
safe terminal state (`ESCALATED`) rather than letting the workflow improvise.

The division of labour between code and model is deliberate and is the core engineering claim
of this project:

| Concern | Implementation | Why |
|---|---|---|
| Anomaly detection | **Deterministic** (EWMA + robust z-score + CUSUM change point) | Must be reproducible, calibrated, testable. An LLM cannot be precision/recall-measured per window. |
| Evidence gathering | **Deterministic tools** over the event store | Tools return facts; the agent may not author facts. |
| Segment attribution | **Deterministic** (lift + concentration + Wilson lower bound) | Statistical attribution is a solved problem; an LLM would only add variance. |
| Impact estimation | **Deterministic arithmetic**, formula recorded | "Do not invent numbers." Every figure carries its derivation. |
| Hypothesis *scoring* | **Deterministic** likelihood model over evidence features | Reproducible, calibratable confidence. |
| Hypothesis *selection & narrative* | **LLM** (Gemini), constrained to the evidence bundle | Genuine reasoning where ambiguity is real; may reject all hypotheses. |
| Action proposal | **LLM** proposes, from a closed action catalogue | Judgement under trade-offs. |
| Action authorisation | **Deterministic policy gateway** | An LLM must never authorise a financial action. |
| Execution | **Deterministic** adapters, audited | Side effects must be replayable. |
| Verification | **Deterministic** two-proportion z-test | "Did it work" is a statistics question, not an opinion. |
| Historical retrieval | **Deterministic** weighted match over structured signatures | An agent that can recall a past incident can invent one. Memory is written and queried only by code. |
| Efficacy priors from history | **Deterministic** shrinkage estimator, hard-capped | Learning must be a bounded numeric update, not a change of mind. |
| Revenue accounting | **Deterministic arithmetic** over recorded measurements | The primary business metric cannot be a model's opinion. |
| Prevention | **Deterministic** pattern mining; output is a document | A system that can widen its own policy has no guardrails. |

**The LLM can never widen its own authority.** It emits a *proposal*; the policy gateway is plain
Python with no model anywhere in the path.

**Nor can memory.** The model is shown historical records; it can never author one, and nothing
retrieved from memory can create an action, change a rupee figure, raise a policy limit or reach
the gateway. What history is permitted to do is move one efficacy prior, inside a hard bound, and
be rendered to a human. See ADR-010.

---

## 2. Component map

```
                        +---------------------------+
                        |   Incident Supervisor     |   explicit FSM + event bus
                        +-------------+-------------+
                                      |
   +-------------+---------------+----+-----------+---------------+--------------+
   v             v               v                v               v              v
Detection   Investigation   Business Impact   Root Cause   Recovery Strategy   Decision
  Agent        Agent            Agent           Agent          Agent            Agent
   |             |               |                |               |               |
   +-------------+-------+-------+----------------+------+--------+---------------+
                         v                               v
               +-------------------+          +---------------------+
               |  TOOL REGISTRY    |          |  INCIDENT MEMORY    |  typed outcome records,
               +---------+---------+          +----------+----------+  deterministic retrieval
                         v                               |
               +-------------------+                     | advisory only, bounded
               |  EVENT STORE      |                     | (never authority)
               +-------------------+                     v
                                              historical evidence on each
                                              priced candidate strategy

Decision Agent --> POLICY GATEWAY (deterministic) --> approved? --+-- yes --> Action Agent
                                                                  +-- no  --> Escalation Agent

     Action Agent --> Verification Agent --> RESOLVED --> Order Recovery Agent
                              |                            (its own policy decision)
                  regressed?  +--> Rollback --> Escalation           |
                                                    |                v
                                                    +--------> LEARNING
                                                                     |
                       +---------------------------+-----------------+
                       v                           v
              Revenue ledger              Incident Memory record
              (at risk / protected /      (signature, options, control vs
               recovered / lost)           treatment, money, rollback)
                                                   |
                                                   v
                                          Pattern mining --> Prevention
                                                             recommendation
                                                             (a document; needs a
                                                              person to apply it)
```

---

## 3. State machine

Canonical states (`pic/schemas.py::IncidentState`):

```
        OBSERVING
            | anomaly passes detection thresholds
            v
        DETECTED ----------------------------------+
            |                                      |
            v                                      |
       INVESTIGATING                               |
            | evidence bundle complete             |
            v                                      |
      IMPACT_ASSESSED                              |
            |                                      |
            v                                      |
        DIAGNOSING --------------------------------+ no hypothesis above floor
            | root cause selected                  |
            v                                      |
     RECOVERY_PLANNING                             |  options priced, history retrieved
            |                                      |
            v                                      |
         DECIDING ---------------------------------+ no positive-EV action
            | action proposed                      |
            v                                      |
      POLICY_REVIEW -------------------------------+ DENY
            |                                      |
            +-- APPROVE / APPROVE_WITH_CLAMP --+   | REQUIRE_APPROVAL
            |                                  |   v
            v                                  |  AWAITING_HUMAN_APPROVAL
        EXECUTING <--------- human approves ---+---+
            |                                      | human rejects / times out
            v                                      |
        VERIFYING                                  |
            |                                      |
            +-- RECOVERED / PARTIALLY_RECOVERED --> RESOLVED
            +-- FAILED ---------------------------> (re-enter DIAGNOSING, attempt <= 2)
            +-- REGRESSED --> ROLLING_BACK ---------+
                                                    v
        RESOLVED                               ESCALATED
            |                                       |
            v                                       |
     RECOVERING_ORDERS                              |  failed payments swept, own policy decision
            |                                       |
            +---------------+-----------------------+
                            v
                        LEARNING --> CLOSED
```

Invariants enforced in code:

- `EXECUTING` is reachable **only** from a `PolicyDecision` with `approved == True`.
- Every transition appends an immutable `AgentStep` record (agent, state, output, latency, tool calls).
- Re-entry into `DIAGNOSING` after `FAILED` is capped (`max_intervention_attempts`, default 2),
  then forced to `ESCALATED`. This prevents an agent thrashing a live payment system.
- `ROLLING_BACK` runs the recorded inverse of the executed action envelope; if the inverse fails,
  the incident escalates with reason `rollback_failed`.
- `RECOVERY_PLANNING` cannot fail the incident. If the strategy stage errors, the Decision Agent
  generates and prices the same option set from the same inputs; the cost is the historical
  evidence, never the response.
- `RECOVERING_ORDERS` is skipped when the intervention was reverted. Chasing customers back into a
  payment stack that is still broken is not a recovery.
- `LEARNING` is where the incident is priced and written to memory. It runs for **every** closed
  incident — resolved, escalated, rolled back or acknowledged — because an agent that remembers
  only its successes will keep repeating whatever produced them.

---

## 4. Agent contracts

Each agent implements `pic/agents/base.py::Agent` with `run(ctx) -> AgentResult`, and declares its
tools, its timeout, and its failure behaviour.

| Agent | Input | Output | On failure |
|---|---|---|---|
| **Detection** | metric windows, baselines | `AnomalySignal` (severity, deviation, confidence, segments, revenue at risk) | Emit nothing; incident never opens (fail-closed) |
| **Investigation** | `AnomalySignal` | `EvidenceBundle` (findings, correlated signals, attribution, config changes) | Partial bundle + `degraded=True`; diagnosis confidence capped at 0.5 |
| **Impact** | `AnomalySignal`, `EvidenceBundle` | `ImpactAssessment` (revenue at risk/hr, txns, customers, formula) | Escalate — never guess money |
| **Root Cause** | `EvidenceBundle` | `RootCauseAssessment` (ranked hypotheses, supporting + contradicting evidence) | Deterministic ranking only; flag `llm_unavailable` |
| **Recovery Strategy** | diagnosis, impact, route health, memory | `RecoveryPlan` (priced candidates, each with `HistoricalSupport`) | Decision Agent prices the same set; history is lost, the response is not |
| **Decision** | `RecoveryPlan` | `ActionProposal` (action, params, benefit, risk, reversibility, EV) | Escalate |
| **Action** | approved `ActionProposal` | `ActionResult` + `AuditRecord` | Mark failed, attempt rollback, escalate |
| **Verification** | pre/post windows, treatment vs control | `VerificationResult` (status, z-test, p-value, side effects) | Treat as `FAILED` (conservative) |
| **Order Recovery** | resolved incident, event store | `OrderRecoveryResult` (failed / recoverable / attempted / recovered) | Record that nothing was attempted, and why |
| **Escalation** | any | `Escalation` (reason, urgency, recommended human action, context pack) | Log loudly; terminal |

The Recovery Strategy Agent exists because generating and pricing options is arithmetic and
choosing between priced options is judgement. Splitting them means the Decision Agent cannot
invent an option that was never priced, and the strategy layer cannot select. Neither half can do
the other's job.

---

## 5. Event schema

The atomic record produced by the simulator and consumed by every tool:

```jsonc
{
  "payment_id": "pay_00000123",
  "order_id": "order_00000123",
  "timestamp": "2026-08-24T10:31:04Z",   // UTC
  "merchant_id": "merch_acme",
  "amount_paise": 249900,                // integer minor units - never float money
  "payment_method": "upi",               // upi|card|netbanking|wallet|emi
  "gateway": "gw_primary",
  "psp": "psp_axis",                     // UPI PSP / acquirer
  "issuer": "HDFC",
  "network": "rupay",                    // card network, null for non-card
  "geography": "MH",                     // Indian state code
  "device": "mobile",                    // mobile|desktop|tablet
  "os": "android",                       // android|ios|windows|macos
  "app_version": "8.4.1",
  "status": "failed",                    // success|failed
  "error_code": "BANK_DECLINE",          // null on success
  "latency_ms": 4180,
  "retry_count": 1,
  "is_retry": false,
  "route_id": "route_A"
}
```

Money is **integer paise** everywhere; display formatting (₹3.7L) happens only at the edge.

---

## 6. Tool contracts

All tools live in `pic/tools/`, are registered in a `ToolRegistry`, and are invoked through a
wrapper that records `(tool, args, latency, ok, error)` against the incident. Read tools are pure
functions of the event store and a time window. Write tools are the only side-effecting code in
the system and each returns an `AuditRecord`.

**Read tools** (Investigation / Impact / Verification):
`get_success_rate_series` · `get_transactions` · `get_payment_failures` · `get_error_distribution`
· `get_payment_method_metrics` · `get_gateway_metrics` · `get_bank_metrics`
· `get_geographic_metrics` · `get_device_metrics` · `get_order_value_distribution`
· `get_latency_metrics` · `get_traffic_composition` · `get_recent_configuration_changes`
· `get_historical_incidents` · `get_action_outcomes` · `get_recoverable_orders`

**Write tools** (Action Agent and Order Recovery Agent only, and only after policy approval):
`shift_traffic` · `disable_payment_method` · `configure_retry` · `rollback_change`
· `set_monitoring_frequency` · `recover_failed_payments` · `notify_merchant`
· `create_incident_ticket`

Every write tool declares an inverse, used by `ROLLING_BACK`. `recover_failed_payments` inverts to
`cancel_order_recovery`, which halts the campaign — and says plainly, in its own result, that it
cannot reverse payments a customer has already completed. An inverse that implied otherwise would
be worse than none.

`get_action_outcomes` is how a historical claim reaches the audit trail: the Recovery Strategy
Agent pulls the same outcome statistics through the registry that it prices with, so "nine of
eleven comparable incidents recovered" is a recorded tool call rather than an assertion.

---

## 7. Policy model

The gateway is deterministic Python evaluating a merchant policy document
(`pic/policies/merchant_policies.yaml`). Every proposal is evaluated against ordered rules and the
**most restrictive outcome wins**.

Outcomes: `APPROVE` · `APPROVE_WITH_CLAMP` (parameter reduced to the limit) · `REQUIRE_APPROVAL` · `DENY`.

Rule classes:

1. **Capability** — is the action in the merchant's allowed set at all?
2. **Bound** — numeric limits (e.g. `max_traffic_shift_pct: 20`) produce a clamp.
3. **Reversibility** — irreversible actions always `REQUIRE_APPROVAL`.
4. **Blast radius** — revenue at risk above `human_approval_revenue_threshold_paise` → `REQUIRE_APPROVAL`.
5. **Confidence floor** — root-cause confidence below `min_confidence_for_autonomous_action` → `REQUIRE_APPROVAL`.
6. **Rate limit** — max autonomous actions per hour, max intervention duration, cooloff after failure.
   The per-incident *attempt* cap is counted over configuration-changing actions only: recovering
   payments that already failed is not another attempt at the fix, and letting a mop-up exhaust
   the budget set aside for fixing the fault would be the cap working against its own purpose.
7. **Conflict** — an action contradicting an active intervention on the same route → `DENY`.
8. **Recovery** — which means the agent may use to complete already-failed payments. `retry` and
   `alternate_route` happen inside the payment stack; `payment_link` reaches the merchant's
   customer, which is a different kind of act, so a campaign requiring it is clamped down to the
   methods the merchant has authorised rather than refused outright.

Output quotes the rule that bound it:

```json
{"action":"shift_traffic","requested":{"percentage":30},"granted":{"percentage":20},
 "outcome":"APPROVE_WITH_CLAMP","approved":true,
 "bound_by":["bound:max_traffic_shift_pct"],"reason":"merchant policy limit exceeded"}
```

---

## 8. Data model (SQLite default, Postgres compatible)

| Table | Purpose |
|---|---|
| `payment_events` | the simulated event stream (indexed on timestamp + segment columns) |
| `config_changes` | merchant-side changes, so "config regression" is discoverable evidence |
| `incidents` | lifecycle row: state, severity, timestamps, root cause, outcome |
| `agent_steps` | one row per agent execution — the observability spine |
| `tool_calls` | every tool invocation with args, latency, ok/error |
| `policy_decisions` | every gateway evaluation, approved or not |
| `audit_records` | every executed action and its result |
| `incident_memory` | one row per learned incident: merchant, signature, action, outcome, money, plus the full structured record as JSON (including whether it was seeded) |
| `prevention_recommendations` | recurring patterns and who, if anyone, acknowledged them. Stored, never applied |
| `ground_truth` | injected scenario labels, used by the evaluation harness only |

`ground_truth` is **never** reachable by any agent tool — enforced by the registry and asserted in
`tests/test_no_ground_truth_leak.py`. Without that isolation the evaluation would be worthless.

---

## 9. Learning: memory, revenue, and prevention

### 9.1 What is remembered

Every closed incident is reduced to one `IncidentOutcomeRecord` (`pic/schemas.py`), built by
`pic/memory/build.py`. It is typed and complete rather than free-form: the failure signature, the
affected segments, the detected metrics, every hypothesis and the one selected, the priced option
set, what policy granted, what was executed at what magnitude, the treatment and control
measurements, the money, whether it had to be rolled back, how long it took, whether a human was
involved, and how it ended.

Typed matters. Every retrieval, success rate and prevention pattern is then a deterministic query
over these fields, so a historical claim can be recomputed and checked rather than recalled.

One property is easy to get wrong and is worth stating: **a reverted intervention still records
what was tried.** The rollback clears `incident.action_result` — correctly, so the reverted shift
stops counting against the cumulative-shift ceiling — and taking that field at face value at
learning time would erase every failure from memory while keeping every success. The supervisor
keeps the executed action separately and passes it in.

### 9.2 How it is retrieved

No vector database (ADR-006). Similarity is a **deterministic weighted match** over the structured
`FailureSignature`: root cause, dominant error code, PSP, payment method, issuer, gateway, route,
geography, severity, degradation magnitude, latency shift, traffic share, affected segment keys and
whether a healthy destination route existed. Explainable ("matched INC-0031: same PSP, same error
code, and a 20% shift fixed it"), reproducible, no embedding service.

Root cause is weighted highest by a clear margin. Two incidents with identical symptoms and
different causes call for opposite actions, and treating them as comparable is the specific mistake
history is most likely to encourage.

Every query is scoped to one merchant by default. One merchant's outage must never become evidence
about another merchant's routing.

### 9.3 How it changes the next decision

For each candidate strategy, memory is queried for the observed outcome of that action at that
magnitude under a similar signature. The result updates the action's efficacy prior through a
shrinkage estimator:

```
blended    = (W * prior + n * observed) / (W + n)          W = HISTORY_PRIOR_WEIGHT = 4
adjustment = clamp(blended - prior, ±MAX_HISTORY_ADJUSTMENT)   cap = 0.20
```

So a single past outcome barely moves the estimate, a dozen consistent ones move it to the cap, and
nothing moves it further. The adjusted prior feeds expected value, which is what the Decision Agent
ranks on — that is the entire mechanism by which learning changes behaviour, and it is six lines of
arithmetic with no model in it.

The prior is updated toward the rate at which the action *helped*, not the rate at which it fully
recovered the incident. A control-verified partial recovery is evidence that rerouting works; it is
reported separately as a partial, but scoring it as a failure would teach the system that a working
intervention does not work. Anything that had to be rolled back never counts as having helped.

Because the option set includes several magnitudes, history can argue about *size* and not only
about *action*: "a 20% shift worked here and a 50% one did not" is expressible, and it changes
which option wins on expected value. The downside of a shift is charged a surcharge for every point
above the default size, so expected value has an interior maximum and an oversized shift can
genuinely be the worse option rather than trivially the best one.

### 9.4 What learning cannot do

| It can | It cannot |
|---|---|
| adjust an efficacy prior within ±0.20 | change a measured rupee figure |
| add a magnitude to the priced option set | create an action outside the diagnosis's catalogue |
| attach evidence to an option and explain it | approve, execute, or call a write tool |
| raise or lower expected value | raise a merchant policy limit, or skip the gateway |
| argue for a preventive policy change | apply one |

Asserted directly in `tests/test_learning_and_recovery.py` under *Safety under learning*, including
the case of a memory containing a hundred consecutive successes at a magnitude the merchant does
not permit.

### 9.5 Revenue

`pic/revenue.py` turns per-hour rates into what the incident actually cost and saved. Three figures,
deliberately disjoint so nothing is counted twice:

| Figure | What it is | Rate applied to |
|---|---|---|
| `revenue_at_risk` | what the degradation threatened | seconds the incident was open |
| `revenue_protected` | future loss the intervention prevented, per the control comparison | seconds the intervention was in force |
| `revenue_recovered` | payments that had already failed and were then completed | not a rate — a sum of amounts |

`protected` is forward-looking and `recovered` is backward-looking, so a rupee can appear in only
one. `lost` is the remainder and `recovery_rate = (protected + recovered) / at_risk`. The identity
`at_risk = protected + recovered + lost` holds by construction and is asserted per incident in the
benchmark — if it ever fails, some rupee is being double counted and every figure above it is
fiction.

Portfolio rates are recomputed from totals, never averaged across incidents: the mean of
per-incident percentages is not a percentage of anything.

### 9.6 The second recovery layer

Fixing the routing stops further loss. It does not recover the payments that failed while it was
broken. `pic/recovery/orders.py` identifies which of those are still completable, by three rules:
the failure has to be the kind a second attempt can clear (a gateway timeout, not an issuer
decline); the order must not have already succeeded on its own (counting a customer's own retry is
the easiest way to make these numbers a lie); and the recovery probability is measured from the
retry outcomes already in the store, falling back to a documented prior only where there are too
few to measure.

Execution is a policy-gated write like any other. Recovered orders are counted as recovered orders
and **not** injected into the payment stream — see ADR-013.

### 9.7 The learned playbook

`pic/memory/playbook.py` is the between-incidents view: per failure signature, the intervention
that has actually worked, its success rate, median recovery and revenue protected, and — where the
evidence separates one — the approach to avoid and why.

It is a **read-only projection**. The strategy layer queries `store.py` directly, so nothing in the
playbook sits on the path from a proposal to an execution; deleting the file would leave the
agent's behaviour identical and only darken the Learning page. A test asserts that no module on the
decision path imports it.

Two rules keep it honest. It names a preferred approach only above `MIN_EVIDENCE` comparable
incidents, and it advises *against* an approach only when that approach is both measurably worse
and has failed more than once — at four attempts a single differing outcome is a 25-point rate gap,
and telling a merchant to avoid something on the strength of one bad day is the kind of confident
nonsense that makes an operator stop reading.

### 9.8 Seeded demo history

Learning is invisible until there is something to have learned from, and running a dozen incidents
live takes longer than anyone watching a demo will wait. `pic/simulation/seed_memory.py` loads a
fixed population of `IncidentOutcomeRecord`s, and **every one carries `seeded=True`**, which travels
into every query, aggregate and screen that renders it.

What makes them a fixture rather than fake data: they describe the same failure `SCN-UPI-PSP`
actually produces, so a live incident retrieves them because the signature genuinely matches; they
obey the same revenue identity a measured record does, so they cannot express an outcome a real
incident could not; and the pattern they encode — moderate shifts work here, large ones regress —
is stated as data rather than asserted in prose.

`POST /api/demo/seed-history` loads them. It touches no payment stream, no control plane and no
policy.

### 9.9 Prevention

`pic/memory/patterns.py` groups records by failure signature and reports the ones that recur at
least three times, with the conditions they shared, the money they have cost, and the action that
has actually worked against them, sized down for preemptive use. Output is a
`PreventionRecommendation`: a document. It cannot write to `merchant_policies.yaml`, it cannot enter
the pipeline as an action, and acknowledging one records who acknowledged it and nothing more
(ADR-011).

---

## 10. Evaluation methodology

Detection quality is measured **per time window**, not per incident, so precision and recall are
well defined. A window is positive if any injected scenario is active during it.

- **Detection**: precision, recall, F1, false-positive rate on clean windows, detection latency
  (seconds from scenario onset to `DETECTED`).
- **Diagnosis**: top-1 root-cause accuracy vs ground truth, evidence-grounding rate (fraction of
  cited evidence traceable to a real tool call), confidence calibration (Brier score).
- **Business**: revenue-at-risk estimation error vs simulator ground truth, revenue protected,
  time to mitigation, incident duration.
- **Revenue recovery**: revenue at risk, protected, recovered and lost across the benchmark; the
  portfolio recovery rate; the identity check above; and how many already-failed payments were
  recovered of how many were recoverable.
- **Learning**: the same failure repeated against one shared memory, recording per repetition how
  much comparable history was retrievable and how far it moved the efficacy prior — alongside the
  safety columns, because the claim being tested is not merely "history changes the decision" but
  "history changes the decision without touching anything that keeps the decision safe".
- **Reliability**: tool-call success rate, invalid-action rate, policy violations (must be 0),
  unnecessary escalation rate, successful rollback rate.
- **End-to-end**: AI run vs a `ManualBaseline` — a parameterised human operator (detection lag,
  triage, investigation, action time) whose parameters are stated in `docs/EVALUATION.md` rather
  than tuned to flatter the AI.

Every number in the README and dashboard is produced by `python -m pic.evaluation.harness`, which
writes `evaluation/results/latest.json`. Fixed seeds mean reproducible output. **No hand-written
numbers.**

---

## 11. Failure-mode demo

Scenario 2 of the demo is a deliberate *failed intervention*: the fallback route is also degraded,
so verification observes no significant improvement, and the system rolls back and escalates rather
than declaring victory. This exercises `FAILED -> DECIDING -> REGRESSED -> ROLLING_BACK ->
ESCALATED` and is asserted in `tests/test_e2e_failure_scenario.py`.
