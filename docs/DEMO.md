# Demo guide

Two ways to run it. The terminal demo is the reliable one for a judging slot; the dashboard is the
one that looks good on a projector.

---

## Option A — terminal demo (30 seconds, no build step)

```bash
pip install -r requirements.txt
python -m pic.demo            # both acts
python -m pic.demo --loop     # act two only, if you are short of time
```

Two acts, back to back against live simulated traffic. Nothing is scripted: the same supervisor,
agents, tools and policy gateway that the API and the benchmark use.

**Act one — can it be trusted with a payment system?** Three incidents: one it fixes, one where the
fix cannot work and it has to notice and undo its own change, and one where nothing is broken and
the right answer is to do nothing.

**Act two — does it get better?** Four incidents against one shared memory. This is the act that
demonstrates the actual product claim, and it is the one to run if you only have two minutes.

Use `--pace 0` to run it instantly, or `--pace 1.2` to let it breathe while you narrate.

---

## Option B — live dashboard

```bash
pip install -r requirements.txt
cd web && npm install && npm run build && cd ..
uvicorn pic.api.main:app --port 8000
# open http://127.0.0.1:8000
```

The backend warms up 45 simulated minutes of healthy traffic (about 20 seconds), then runs the
simulation at 60× real time. Click any scenario tile to inject an incident and watch the lifecycle
advance stage by stage over the WebSocket.

For hot-reloading frontend work, run `npm run dev` in `web/` and use `http://localhost:5173` — Vite
proxies `/api` and `/ws` to port 8000.

---

## The narrative

### Scenario 1 — the agent fixes it

*A single UPI PSP starts declining collect requests. UPI is the merchant's dominant method, so the
headline success rate falls hard. A healthy alternative route exists.*

Watch for:

1. **Detection** fires on a robust z-score plus a materiality floor plus a CUSUM change point —
   three independent tests that must agree. No model is involved.
2. **Investigation** runs a battery of read tools and produces findings with IDs. Every later claim
   cites those IDs, and the benchmark verifies that each cited ID exists.
3. **Impact** shows its arithmetic. Attempt rate × shortfall × per-stratum order value. You can
   check the multiplication on screen.
4. **Root cause** names the PSP with a confidence, and lists what argues *against* its own leading
   hypothesis.
5. **Decision** proposes moving 15% of UPI traffic, priced: expected value, revenue protected, risk.
   Note it scopes the shift to UPI rather than moving the merchant's healthy card volume.
6. **Policy gateway** approves — plain Python, no model in the path.
7. **Verification** is the part worth pausing on. It reports something like *treated 95% vs control
   29%*: the traffic that moved against the traffic left behind, in the same minutes. That is a
   concurrent control group, not a before/after comparison.

The incident usually ends **PARTIALLY_RECOVERED** after a second intervention, because a 15% shift
cannot close the whole gap and the system says so rather than claiming a win.

### Scenario 2 — the fix does not work

*The same symptoms, but every UPI provider is degrading together. Random variation still makes one
look worst, so the evidence points at a reroute that cannot possibly help.*

This is the scenario that matters. Watch for:

1. Confidence lands around **0.44**, below the merchant's 0.70 autonomous floor, so the **policy
   gateway holds the action for a human** — the "agent recommends action, human approval required"
   state. A human is notified and a ticket is filed; the incident does not sit silently.
2. The demo simulates an operator approving it.
3. **Verification fails against the control**: treated ≈ 45%, control ≈ 42%, p ≈ 0.29. The
   improvement is not distinguishable from noise.
4. The agent **rolls back its own change** — the audit trail shows the inverse action, and the final
   route weights are exactly what they were before.
5. It **escalates** rather than trying the same thing again.

If a judge asks one question, it will be about this scenario. The answer is that the control group
is what makes the failure detectable: a before/after comparison during a worsening outage cannot
distinguish "my fix did nothing" from "the incident got worse".

### Scenario 3 — nothing is broken

*A campaign drives a surge of wallet and netbanking traffic that has always converted worse.*

The headline success rate falls. **No incident opens.** Rerouting healthy traffic here would add
risk and protect no revenue, and the system's willingness to do nothing is a feature — it is the
false-positive control in the benchmark.

### Act two — the agent gets better because it remembers

Four incidents, each in its own simulated world with its own traffic, sharing one incident memory.
The only thing carried between them is what was deliberately written when the previous one closed,
so if the later ones behave differently, nothing else can account for it.

| | What happens | What to point at |
|---|---|---|
| **1** | A UPI PSP degrades. Nothing in memory. | *"No comparable incidents in memory yet; this decision rests on live evidence alone."* This is the baseline. |
| **2** | The same failure pattern. | The historical recommendation appears, quoting the first incident. The efficacy prior moves — `prior +0.03` next to each option. |
| **3** | Looks the same, but every route is degraded, so the reroute cannot work. | It acts, measures against the control, reverts its own change — and the failure goes into the record. Watch `learned REGRESSED -> memory`. |
| **4** | The same pattern again, now against a mixed record. | The prior has gone *negative*. The system is arguing from its own failures as well as its successes. |

Then the summary:

```
WHAT THE SYSTEM NOW KNOWS
  incidents remembered        4
  revenue at risk across them INR 6.2L
  protected                   INR 2.3L
  recovered                   INR 1.9L
  recovery rate               68%
  shift_traffic moderate      3/4 helped, 1 rolled back

PREVENTION RECOMMENDATION
  upi · psp_axis · PSP_UNAVAILABLE has degraded 3 times between 10:00 and 11:00 UTC.
    when p95 latency up at least ...
    when a healthy destination route was available in 3 of 3 cases
  proposes            shift_traffic 8% route_A -> route_C for upi
  historical loss     INR 64,117
  estimated benefit   INR 64,117
  authority           merchant approval required - policy is unchanged
```

The last line is the one worth dwelling on. The most valuable thing the system has learned is the
thing it is least able to act on alone.

*(Figures are from one run at the shipped seeds; yours will differ slightly with the traffic
sampling. Every one of them is arithmetic over measurements the agents actually took.)*

---

## Things worth pointing at

**The clamp.** Trigger a scenario and look at the policy stage. When a proposal exceeds a merchant
limit the gateway reduces it rather than refusing it, and records both:

```json
{"requested": {"percentage": 30}, "granted": {"percentage": 20},
 "outcome": "APPROVE_WITH_CLAMP", "bound_by": ["bound:max_traffic_shift_pct"]}
```

**The echo test.** In the investigation findings you will sometimes see a line like *"Excluding
payment_method=upi traffic, this segment returns to −5.9%, so it reflects that fault rather than an
independent one."* That is the system distinguishing a genuine second fault from a statistical
shadow of the first. Without it, one PSP outage reads as "multiple concurrent degradations" and
produces no actionable diagnosis.

**Incident correlation.** Leave the dashboard running through a long scenario. One fault stays one
incident, with a correlated-detection count, rather than opening a new one every monitoring cycle.

**The money adds up.** On the Overview, the bar under the headline has three segments — protected,
recovered, lost — and they sum to the revenue at risk exactly. That identity is asserted per
incident in the benchmark (`revenue_identity_rate`, which must be 1.0), because a system reporting
a 130% recovery rate has not had a good day, it has a bug.

**The decision trace.** Open any incident and scroll to *Decision trace*. Every stage can be
expanded: the findings and the tool that produced each one, every hypothesis with what argues
against it, the comparable incidents the decision was weighed against, every option that was priced
with its historical support, and every policy rule the gateway evaluated. Nothing on that page is a
sentence without working behind it.

**The Learning page.** Run the same scenario three or four times from Simulate and the page fills
in: the incident memory, what has actually worked (with partials and rollbacks counted separately),
and eventually a prevention recommendation. Click *Record as accepted* and check `GET /api/policy`
before and after — it is byte-identical. The card says so, and it is true.

**Recovering what was already lost.** The trace's *Failed-payment recovery* stage reports the whole
population: how many payments failed, how many were still recoverable, how many were attempted, and
how many came back. Note the line about payment links — the merchant has not authorised that method
for autonomous use, so those orders are counted as recoverable and deliberately not attempted.

**The benchmark panel** at the bottom of the dashboard reads `evaluation/results/latest.json`. If it
says no results, run `python -m pic.evaluation.harness`. The dashboard never renders a number the
harness did not produce.

---

## If something goes wrong

**Port 8000 in use** — another instance is already running. On Windows:
`Get-NetTCPConnection -LocalPort 8000 -State Listen | Stop-Process -Id { $_.OwningProcess } -Force`

**Dashboard shows "no evaluation results"** — run the harness once; it takes a few minutes for three
seeds, or use `--seeds 7` for one.

**No incident appears after triggering** — detection needs enough volume in a window. Give it 30–60
seconds of wall clock at 60× speed. `SCN-TRAFFIC-MIX` is *supposed* to produce nothing.

**A Gemini key is set and calls are failing** — the system falls back to the deterministic reasoner
automatically and keeps running; the event stream shows `reasoner_degraded`. Benchmarks pin the
deterministic reasoner regardless, so results are unaffected. Act two of the demo also pins it
deliberately: the claim being made is that behaviour changed *because of what the system
remembered*, and that is only legible if everything else is held constant.

**Act two shows "no recurring pattern yet"** — a pattern needs at least three comparable incidents
before it is worth a merchant's attention. If one of the four repetitions did not open an incident
(traffic sampling), you get three records and no pattern. Re-run it.

**The Learning page is empty** — memory is per visitor and is cleared by Reset, deliberately: what
the system learned belongs to the incidents Reset is clearing, and leaving it would judge the next
incident against history from a world that no longer exists.
