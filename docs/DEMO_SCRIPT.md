# Five-minute submission script

A spoken walkthrough for the Razorpay Buildathon submission. Roughly 730 words at a measured demo
pace — about five minutes with the pauses where things load.

**Two kinds of number appear below.**

- **Fixed** figures come from the benchmark (`evaluation/results/latest.json`) and are the same on
  every machine. They are written out in full and safe to say.
- **Live** figures come from whatever the simulator generates while you record, so they change every
  run. They appear as `⟨read what is on screen⟩`. Never rehearse a live number — say the one you
  can see.

---

## Before you record

```bash
cd web && npm run build && cd ..
PIC_LLM_PROVIDER=deterministic uvicorn pic.api.main:app --port 8000
```

**Open the dashboard at this exact URL**, not the bare one:

```
http://127.0.0.1:8000/?session=demo
```

Each browser gets its own private world, keyed by that `session` parameter. Pinning it to `demo`
means the terminal command in step 2 talks to the same world you are looking at. Without it, that
command changes a world nobody is watching and your screen never moves.

Then, in a second terminal:

```bash
# Detection needs ~2 minutes of real traffic before three tests agree. At 4x that is ~30 seconds,
# which is exactly the length of the narration written to cover the wait.
curl -X POST "http://127.0.0.1:8000/api/control/speed?speedup=4&session=demo"
```

1. Let the page sit for about twenty seconds so the charts have some traffic in them.
2. **Overview → Load demo history.** This seeds fourteen labelled historical records so the
   learning section has something to have learned from. Say so on camera when you get there — they
   are marked *seeded* in the interface and you should not pretend otherwise.
3. **Simulate**, and have *UPI PSP route degradation* ready to click. If you are re-taking, hit
   **Reset** here first: it clears incidents, memory and the policy rate limiter back to a clean
   baseline, so the second take behaves like the first.
4. The deterministic reasoner is the default and is what the benchmark uses. Leave it.

Two things worth knowing before you start, because both are fine and neither is worth a re-take:

- **Policy may not ask you to approve.** If confidence clears the merchant's autonomous floor, the
  agent acts on its own. That is the system working. The line to say is in the 2:00 beat either way.
- **Verification can come back partially recovered.** Also fine, and arguably a better demo. The
  fallback table at the end of this document has the line for each outcome.

If you have twelve minutes rather than five, run the terminal demo too — `python -m pic.demo`
tells the same story with no browser.

---

## The script

### 0:00 — What this is *(≈25s)*

> **[Landing page]**

"Payment Incident Commander. It's an autonomous payment recovery and revenue protection agent.

When payments start failing, most tools tell you *that* something is wrong. The interesting problem
isn't detection — it's everything after. Which of five providers is at fault. How much money is
actually walking out of the door. What to do about it that won't make things worse. And whether the
thing you did actually worked.

The tagline is the whole product: **recover the revenue, not just the incident.**"

> **[Point at the loop band under the hero]**

"Revenue at risk, detect, diagnose, recover, verify, revenue protected, learn, prevent. That loop is
what I'll walk through."

---

### 0:25 — Money at risk *(≈45s)*

> **[Simulate → click "UPI PSP route degradation" → Overview]**

"I've just broken a UPI provider. Nothing here is scripted — the same detector, agents and policy
gateway run whether I'm watching or the benchmark is.

Detection needs three independent statistical tests to agree before it will open an incident, so
this takes a few seconds of real traffic."

> **[Incident appears. Open it.]**

"And there it is. The first thing the page tells me is not a stack trace — it's
`⟨read the revenue-at-risk figure⟩` an hour of merchant revenue at risk. That number is priced from
this merchant's own traffic, per payment method and order-value band, and the arithmetic is on the
page.

Underneath: the cause. UPI on PSP Axis, at `⟨read the confidence⟩` confidence."

---

### 1:10 — The recommendation *(≈50s)*

> **[Scroll to "Recommended recovery"]**

"Now the part that makes this a product rather than a dashboard. The agent recommends shifting
`⟨read the percentage⟩` of UPI traffic off the degraded route, and it tells me what it expects that
to be worth, how confident it is, what it's risking — and what history says.

**Eight of nine comparable incidents improved** with this action. That's not a language model
remembering — it's a count over structured records of what actually happened. (Read the count off
the screen — the live incident joins the history, so it may say nine of ten by the time it closes.)

And on the right, unprompted: *what happens if this is wrong.* Only that percentage moves. The rest
stays put as a control group. Nothing is judged for three minutes. If it doesn't help, it's undone
automatically."

> **[Scroll to the strategy table]**

"It also shows every option it *didn't* pick, and what each was worth. The objective isn't success
rate — it's recovered revenue inside the merchant's risk limits."

---

### 2:00 — The gate *(≈35s)*

> **[Point at the policy line, then click Approve]**

"Every action goes through a deterministic policy gateway. Plain Python, evaluating the merchant's
own YAML. No model anywhere in that path.

Here it's clamping the shift to what merchant policy allows, and holding it for a human because
confidence is below their autonomous floor. **AI proposes, policy decides, the system verifies.**
I'll approve it."

---

### 2:35 — Proof *(≈45s)*

> **[Scroll to verification]**

"This is the part I'd point at first if you only had thirty seconds.

We do not claim recovery just because success rate went up. When the agent moved that traffic, it
deliberately left the rest of it alone — so there's a control group living through the same outage,
the same hour, the same customers.

Control: `⟨read control %⟩`. Treated: `⟨read treatment %⟩`. That gap, at that p-value, is the
intervention — not the incident recovering on its own.

That distinction matters because those two situations call for opposite responses. Without a
control, a fix applied during a worsening outage looks like it caused the damage, and the system
rolls back something that was working."

---

### 3:20 — The money *(≈30s)*

> **[Scroll to "Revenue actually recovered"]**

"So: `⟨at risk⟩` at risk, `⟨protected⟩` protected, `⟨recovered⟩` recovered from payments that had
already failed, and the remainder lost.

Those three figures are disjoint by construction — they sum to the exposure exactly. The benchmark
asserts that identity on every incident, because a system reporting a 130% recovery rate hasn't had
a good day, it has a bug."

---

### 3:50 — It gets better *(≈40s)*

> **[Scroll to "What the agent learned", then go to the Learning page]**

"When the incident closes, the whole thing becomes one structured record.

On the Learning page that becomes a playbook. For this failure signature it now prefers a moderate
traffic shift — and it tells me what to *avoid*: the larger shift, because it improved payments in
two of five comparable incidents and three of those had to be rolled back.

To be straight with you: fourteen of these records are seeded demo history, and the UI labels them.
But they describe the same failure this scenario produces, so the live incident retrieved them
because the signature genuinely matched — not because anything was special-cased."

> **[Scroll to Prevention]**

"And then the last step: this pattern has recurred, so it proposes preventing it. That's a
recommendation, not an action — approving it records the request. Merchant policy only ever changes
when a person edits the policy file."

---

### 4:30 — When it goes wrong *(≈30s)*

> **[Overview, or the rolled-back incident if you have one open]**

"One more thing, and it's the one I'd want a reviewer to check. There's a scenario where every UPI
provider is failing together, so the obvious reroute *cannot* work. The agent tries it, measures no
improvement against the control, reverts its own change from a recorded inverse, and escalates with
the evidence.

Across the benchmark: **zero policy violations, zero unauthorised executions, and every rollback
succeeded.** Detection precision is 99.5%, root-cause accuracy 91%, median mitigation 136 seconds
against a modelled human baseline of 27 minutes.

Every one of those is generated by a reproducible harness, not typed into a slide."

---

### 5:00 — Close

"Detect. Recover. Verify. Learn. Recover the revenue — not just the incident. Thank you."

---

## If something doesn't fire

Traffic is sampled, so runs differ. None of these are failures of the system:

| What you see                                         | Say this                                                                                          |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| Detection takes longer than expected                 | "It needs three tests to agree — it won't open an incident on one noisy window."                 |
| Policy holds it for approval                         | Good. That*is* the safety story — approve it on camera.                                        |
| Verification comes back**partially recovered** | "Real improvement, but not the whole gap — so it stays open rather than claiming a win."         |
| Verification comes back**inconclusive**        | "Not enough traffic yet to judge, so it waits rather than guessing."                              |
| Order recovery says it wasn't authorised             | "Merchant policy withheld that recovery method — it says so instead of doing it quietly."        |
| No prevention recommendation                         | A pattern needs three comparable incidents. Run the scenario again, or say it needs more history. |

---

## The thirty-second cut

If you are cut short:

"Payments start failing. This prices the revenue at risk, picks the safest recovery, executes it
inside the merchant's policy, then proves it worked by comparing against traffic it deliberately
left alone. If it didn't work, it rolls itself back. Every incident becomes a record that makes the
next recovery better. Zero policy violations across the benchmark, and the AI never executes
anything — it proposes, policy decides."
