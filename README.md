# Payment Incident Commander

**An autonomous AI operations system that detects payment degradation, investigates why it happened,
estimates revenue at risk, safely takes corrective action, and verifies that payments recovered.**

It does not just tell a merchant that payments are failing. It investigates, decides, acts within
the merchant's own policy, and then checks its own work — and when the fix does not help, it says
so, undoes what it did, and hands over to a human.

```
OBSERVE → DETECT → INVESTIGATE → HYPOTHESIZE → DECIDE → [POLICY GATE] → ACT → VERIFY → LEARN
                                                             │
                                                             └─ or ESCALATE
```

---

## Quick start

```bash
pip install -r requirements.txt
python -m pic.demo            # three live incidents, ~30s, no build step
```

That runs against simulated traffic through the same supervisor, agents, tools and policy gateway
the dashboard and benchmark use. Nothing is scripted.

<details>
<summary>Dashboard, benchmark, tests</summary>

```bash
# Reproducible benchmark (writes evaluation/results/latest.json)
python -m pic.evaluation.harness

# Tests
python -m pytest -q

# Live dashboard at http://127.0.0.1:8000
cd web && npm install && npm run build && cd ..
uvicorn pic.api.main:app --port 8000
```

Optional: `cp .env.example .env` and add a `GEMINI_API_KEY` to have the Root Cause and Decision
agents reason with Gemini. Without a key the system runs end to end on a deterministic reasoner —
see [ADR-003](docs/DECISIONS.md).

</details>

---

## What makes this different

Most "AI for payments" demos stop at detection, or wrap an LLM around a dashboard. Four things here
are load-bearing.

### 1. The LLM cannot execute anything

Every action passes through a **deterministic policy gateway** (`pic/policies/gateway.py`) — plain
Python evaluating a merchant YAML policy, with no model anywhere in the path. The Action Agent
physically cannot invoke a write tool without a `PolicyDecision` naming that exact action, and the
tool registry raises if it tries.

That makes "zero policy violations" a **testable property** rather than a hope. It is asserted in
the test suite and measured in the benchmark.

The gateway clamps rather than refuses, so an over-ambitious proposal still mitigates the incident:

```json
{"action": "shift_traffic",
 "requested": {"percentage": 30}, "granted": {"percentage": 20},
 "outcome": "APPROVE_WITH_CLAMP", "approved": true,
 "bound_by": ["bound:max_traffic_shift_pct"]}
```

### 2. Verification uses a concurrent control group, not before/after

A payment incident is usually still worsening when the agent acts. Comparing the window before the
action to the window after it measures the incident's own trajectory as much as the intervention —
so a genuinely helpful fix applied during a worsening outage *looks like it caused the damage*, and
the system rolls back something that was working.

A traffic shift moves part of a segment and leaves the rest in place, which is a natural A/B. The
traffic still on the old route is a **control group living through the same ramp, the same hour and
the same customers**:

```
VERIFICATION  RECOVERED
  Traffic moved to route_B is converting at 95.3% against 29.0% for the
  124 payments left on route_A (+66.3%, p=0.000). Measured against a
  concurrent control, so the incident's own trajectory cannot account for it.
```

This is what lets the agent distinguish *"my action hurt"* from *"the incident got worse"* — two
situations calling for opposite responses.

### 3. It knows the difference between a fault and an echo

When one PSP fails, every issuer and every region also measures as degraded, because they all route
traffic through it. Treating those as independent faults turns one provider outage into "multiple
concurrent degradations", which produces no actionable diagnosis at all.

So segments are tested **causally**: remove the primary segment's traffic and re-measure. A
dimension still degraded without it is a real second fault; one that recovers was only ever a
shadow of the first. You can see this in the investigation output:

> Segment psp=psp_hdfc is at 73.8% success versus a baseline of 91.9% (−18.1%)… **Excluding
> payment_method=upi traffic, this segment returns to −5.9%, so it reflects that fault rather than
> an independent one.**

### 4. It is willing to do nothing, and to admit failure

- A **traffic-mix change** — customers moving to methods that always convert worse — opens no
  incident. Nothing is broken; rerouting healthy traffic would add risk and protect no revenue.
- An **issuer outage** gets `notify_merchant`, not a reroute. No routing change can fix a bank
  declining its own cards, and the system does not pretend otherwise.
- A **failed intervention** is reverted from a recorded inverse and escalated, rather than retried
  into the ground. Demo scenario 2 exists specifically to show this.

---

## Benchmark

<!-- BENCHMARK:START -->
Run `python -m pic.evaluation.harness` then `python scripts/update_readme_metrics.py` to populate
this section.
<!-- BENCHMARK:END -->

Every figure above is generated from `evaluation/results/latest.json` by
`scripts/update_readme_metrics.py`. **No number in this README is written by hand.** Methodology,
including what the human baseline is and is not, is in [docs/EVALUATION.md](docs/EVALUATION.md) —
which also lists the known weaknesses rather than hiding them.

---

## Architecture

```
                        ┌───────────────────────────┐
                        │   Incident Supervisor     │  explicit FSM, no autonomous loop
                        └─────────────┬─────────────┘
   ┌──────────────┬───────────────────┼───────────────────┬──────────────┐
   ▼              ▼                   ▼                   ▼              ▼
Detection    Investigation      Business Impact       Root Cause      Decision
   │              │                   │                   │              │
   └──────────────┴─────────┬─────────┴───────────────────┴──────────────┘
                            ▼
                  ┌───────────────────┐
                  │   TOOL REGISTRY   │  typed, audited, read-only by default
                  └─────────┬─────────┘
                            ▼
                  ┌───────────────────┐
                  │    EVENT STORE    │  payment events + baselines
                  └───────────────────┘

Decision ──▶ POLICY GATEWAY (deterministic) ──┬── approved ──▶ Action ──▶ Verification
                                              └── refused / needs human ──▶ Escalation
                                                                               │
                                                          failed / regressed ──┴──▶ Rollback
```

**Where the model is used, and where it deliberately is not:**

| Concern | Implementation | Why |
|---|---|---|
| Anomaly detection | Deterministic (EWMA, robust z, CUSUM) | Must be reproducible and precision/recall-measurable across thousands of windows. |
| Evidence gathering | Deterministic tools | Tools return facts; an agent may not author them. |
| Segment attribution | Deterministic (lift, concentration, Wilson bound, residual test) | Solved statistically; a model would only add variance. |
| Impact estimation | Deterministic arithmetic, derivation recorded | "Do not invent numbers" has to be checkable. |
| Hypothesis *scoring* | Deterministic likelihood over evidence features | Reproducible, inspectable confidence. |
| Hypothesis *selection & narrative* | **Gemini** | Genuine ambiguity; may declare itself unable to separate causes. |
| Action proposal | **Gemini**, from a closed catalogue | Judgement under trade-offs. |
| Action authorisation | Deterministic policy gateway | **A model must never authorise a financial action.** |
| Verification | Deterministic two-proportion z-test | "Did it work" is a statistics question. |

Full detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). The reasoning behind each significant
choice — and what it cost — is in [docs/DECISIONS.md](docs/DECISIONS.md).

---

## The simulator is not a data faker

Two properties make it usable as an evaluation substrate:

**The control plane responds to actions.** Route weights, disabled methods and retry policy live in
a `ControlPlane` object that the Action Agent's write tools mutate. When traffic is shifted, the
generator samples differently from that moment on and success rate genuinely moves. Verification is
measuring a real effect, not a scripted one.

**Ground truth comes from the generative process.** Degradation multiplies a known nominal success
probability, so the true expected loss is `amount × (p_nominal − p_effective)`, bucketed per minute.
The agent's estimate is scored against that — and the agent physically cannot read it
([ADR-007](docs/DECISIONS.md), enforced by a test).

Nine scenarios: PSP degradation, issuer outage, Android checkout regression, gateway latency
cascade, traffic-mix change (the no-fault control), multi-factor, network-wide UPI failure with an
unhealthy fallback, region-specific netbanking failures, and high-value card declines.

---

## Repository layout

```
pic/
  schemas.py          typed contracts — every agent boundary is here
  config.py           thresholds and tunables
  store.py            in-memory event index (bisect-sliced windows)
  database.py         SQLAlchemy audit schema (SQLite default, Postgres compatible)
  engine.py           wires simulator + agents + supervisor together
  demo.py             the terminal demo
  simulation/         traffic generator, control plane, scenario catalogue
  detection/          statistics + the two-tier deterministic detector
  agents/             detection, investigation, impact, root_cause, decision,
                      action, verification, escalation, supervisor (FSM)
  policies/           merchant_policies.yaml + the deterministic gateway
  tools/              registry, read tools, write tools
  llm/                Reasoner interface, Gemini client, deterministic fallback
  memory/             incident memory + deterministic similarity retrieval
  evaluation/         the benchmark harness
  api/                FastAPI backend + WebSocket stream
web/                  React dashboard (Vite)
tests/                safety invariants, detection quality, end-to-end lifecycles
docs/                 ARCHITECTURE · DECISIONS · EVALUATION · DEMO
```

---

## Honest limitations

- **Confidence calibration is the weakest metric.** Mean confidence when correct is close to mean
  confidence when wrong. Reported, not tuned away.
- **Nine scenarios is a small corpus.** Accuracy figures carry wide confidence intervals.
- **Revenue estimates are noisy early in an incident.** Order amounts are lognormal; a two-minute
  estimate has real sampling error, flagged as provisional below 400 payments.
- **`rollback_change` is modelled, not simulated end to end.** The simulator has no notion of
  un-deploying an SDK, so it records intent and notifies the owning team, flagged `partial_effect`
  so verification does not expect a full recovery.
- **This runs against a simulator.** Write tools are an adapter layer with the Razorpay call shape;
  nothing above that layer changes when a real API is substituted.
