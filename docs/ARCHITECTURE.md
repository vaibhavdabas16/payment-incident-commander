# Payment Incident Commander — Architecture

> An autonomous AI operations system that detects payment degradation, investigates why it
> happened, estimates revenue at risk, safely takes corrective action, and verifies recovery.

This document is the Phase-1 contract. Everything in `pic/` implements what is specified here.

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

**The LLM can never widen its own authority.** It emits a *proposal*; the policy gateway is plain
Python with no model anywhere in the path.

---

## 2. Component map

```
                        +---------------------------+
                        |   Incident Supervisor     |   explicit FSM + event bus
                        +-------------+-------------+
                                      |
   +--------------+-------------------+-------------------+--------------+
   v              v                   v                   v              v
Detection    Investigation      Business Impact       Root Cause      Decision
 Agent          Agent               Agent               Agent          Agent
   |              |                   |                   |              |
   +--------------+---------+---------+-------------------+--------------+
                            v
                  +-------------------+
                  |  TOOL REGISTRY    |  typed, audited, read-only by default
                  +---------+---------+
                            v
                  +-------------------+
                  |  EVENT STORE      |  SQLite/Postgres - payment events + baselines
                  +-------------------+

Decision Agent --> POLICY GATEWAY (deterministic) --> approved? --+-- yes --> Action Agent
                                                                  +-- no  --> Escalation Agent

               Action Agent --> Verification Agent --> Incident Resolution
                                        |
                            regressed?  +--> Rollback --> Escalation
                                                              |
                                                              v
                                                    Incident Memory (learning)
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
            +-- RECOVERED / PARTIALLY_RECOVERED --> RESOLVED --> LEARNING --> CLOSED
            +-- FAILED ---------------------------> (re-enter DECIDING, attempt <= 2)
            +-- REGRESSED --> ROLLING_BACK ---------+
                                                    v
                                               ESCALATED --> LEARNING --> CLOSED
```

Invariants enforced in code:

- `EXECUTING` is reachable **only** from a `PolicyDecision` with `approved == True`.
- Every transition appends an immutable `AgentStep` record (agent, state, output, latency, tool calls).
- Re-entry into `DECIDING` after `FAILED` is capped (`max_intervention_attempts`, default 2), then
  forced to `ESCALATED`. This prevents an agent thrashing a live payment system.
- `ROLLING_BACK` runs the recorded inverse of the executed action envelope; if the inverse fails,
  the incident escalates with reason `rollback_failed`.

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
| **Decision** | all of the above | `ActionProposal` (action, params, benefit, risk, reversibility, EV) | Escalate |
| **Action** | approved `ActionProposal` | `ActionResult` + `AuditRecord` | Mark failed, attempt rollback, escalate |
| **Verification** | pre/post windows | `VerificationResult` (status, z-test, p-value, side effects) | Treat as `FAILED` (conservative) |
| **Escalation** | any | `Escalation` (reason, urgency, recommended human action, context pack) | Log loudly; terminal |

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
· `get_historical_incidents`

**Write tools** (Action Agent only, and only after policy approval):
`shift_traffic` · `disable_payment_method` · `configure_retry` · `rollback_change`
· `set_monitoring_frequency` · `notify_merchant` · `create_incident_ticket`

Every write tool declares an inverse, used by `ROLLING_BACK`.

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
7. **Conflict** — an action contradicting an active intervention on the same route → `DENY`.

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
| `incident_memory` | closed-incident summaries + feature vector for retrieval |
| `ground_truth` | injected scenario labels, used by the evaluation harness only |

`ground_truth` is **never** reachable by any agent tool — enforced by the registry and asserted in
`tests/test_no_ground_truth_leak.py`. Without that isolation the evaluation would be worthless.

---

## 9. Incident memory and retrieval

No vector database. Incident similarity is a **deterministic weighted match** over a feature vector
(dominant error code, method, psp, issuer, device, os, geography concentration, severity band,
deviation magnitude, latency signature, config-change flag). This is explainable ("matched INC-0031:
same PSP and same error code"), reproducible, and needs no embedding service. Retrieved priors
adjust hypothesis scores by a bounded amount (±0.15) so memory can inform but never dominate live
evidence.

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
