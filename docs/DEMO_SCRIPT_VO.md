# Five-minute submission script, voice-over version

For a screen recording narrated with generated audio (ElevenLabs or similar), rather than spoken
live. If you are presenting live, use [DEMO_SCRIPT.md](DEMO_SCRIPT.md) instead: it quotes the
figures on screen, which is better live and impossible to pre-generate.

## The one rule that shapes this script

**Pre-generated audio cannot quote a live number.** Rupee figures, success rates and confidence
scores differ on every run, so narration recorded in advance can only *describe* them: "the
merchant revenue at risk, per hour", never "four point two lakh". Every figure actually spoken
below is a benchmark figure from `evaluation/results/latest.json`, identical on any machine.

That constraint turns out to be a feature. It means you can regenerate the video without
regenerating the audio, and re-record a fumbled take without anything going out of sync.

## Producing it

The clips are plain text files in [`vo/`](vo/), one per beat, already stripped of anything that
reads badly aloud: no markdown, no em dashes, no rupee symbol, no digits where words are safer.
Paste each one in separately and export eleven clips rather than generating one long take, so a
single mispronunciation costs you thirty seconds instead of five minutes.

Suggested ElevenLabs settings:

| Setting | Value | Why |
|---|---|---|
| Model | Multilingual v2 or v3 | Handles the Indian payments vocabulary without mangling it |
| Stability | ~50% | Lower drifts in tone across eleven separate clips |
| Similarity | ~75% | |
| Speed | 1.0 | The budgets below assume it. Faster and the clips undershoot their beats |
| Style | low | Exaggeration reads as salesy over a technical demo |

Check these words in the output before you commit to a full generation: **UPI** (say "you pee eye",
not "oopie"), **PSP**, **p value**, **paise**, **Razorpay**. If any comes out wrong, spell it
phonetically in that clip only.

## Recording the screen

Do not drive the demo by hand. Run:

```bash
python -m uvicorn pic.api.main:app --port 8000     # one terminal
python tools/record_demo.py                        # another
```

This drives the real UI through the beats below, waiting on real state rather than a stopwatch, and
holds each beat until its budget is spent. It writes `docs/vo/demo.webm` and `docs/vo/timing.json`,
the latter giving the exact second each clip should start. It also warns you if a beat overran,
which is the only thing that can push the audio out of alignment.

It is not a demo mode inside the product. It clicks what you would click, through the same API and
the same build that ships. If the agent decides something different on the day, the video shows
that.

## The timeline


| Starts | Clip | Beat | Spoken | On screen |
|---|---|---|---|---|
| **0:00** | 01 | Opening | 31s | Landing page, top. Headline and the product screenshot. |
| **0:31** | 02 | The loop | 9s | Scroll down to the loop band. Let the eight steps sit on screen. |
| **0:41** | 03 | Incident opens | 31s | Simulate, click UPI PSP route degradation, then Overview. Cut the dead wait in the edit; land on the incident appearing. |
| **1:13** | 04 | Diagnosis and recommendation | 35s | Open the incident. Hero, then scroll to Recommended recovery. Hold on the history line. |
| **1:49** | 05 | Exposure and options | 26s | Pan to the exposure panel on the right, then scroll to the strategy table. |
| **2:16** | 06 | The policy gate | 21s | Highlight the policy line, then click Approve on camera. |
| **2:38** | 07 | Verification | 39s | Scroll to Recovery verification. Hold on the control and treatment figures. |
| **3:19** | 08 | The money | 24s | Scroll to Revenue actually recovered. Hold on the four figures. |
| **3:44** | 09 | Learning | 34s | What the agent learned, then the Learning page and its playbook. |
| **4:20** | 10 | Prevention and rollback | 27s | Scroll to Prevention opportunities. |
| **4:48** | 11 | Benchmark and close | 28s | How it works, or the landing page proof strip. |

**Total 5:17** across 11 clips, 786 words of narration at 155 words per minute.

## The clips

Each is also a file in [`vo/`](vo/), ready to paste.

### Clip 01 — Opening

`docs/vo/01-opening.txt` · 79 words · about 31s · starts at 0:00

> **On screen:** Landing page, top. Headline and the product screenshot.

```text
Payment Incident Commander is an autonomous payment recovery and revenue protection agent.

When payments start failing, most tools tell you that something is wrong. The harder problem is everything after. Which of five providers is at fault. How much money is walking out of the door. What to do about it that will not make things worse. And whether the thing you did actually worked.

The goal is not to manage the incident. It is to recover the revenue.
```

### Clip 02 — The loop

`docs/vo/02-the-loop.txt` · 23 words · about 9s · starts at 0:31

> **On screen:** Scroll down to the loop band. Let the eight steps sit on screen.

```text
The loop runs in eight steps. Revenue at risk. Detect. Diagnose. Recover. Verify. Revenue protected. Learn. Prevent.

Money at both ends, on purpose.
```

### Clip 03 — Incident opens

`docs/vo/03-incident-opens.txt` · 79 words · about 31s · starts at 0:41

> **On screen:** Simulate, click UPI PSP route degradation, then Overview. Cut the dead wait in the edit; land on the incident appearing.

```text
I have just degraded a UPI provider. Nothing here is scripted. The same detector, agents and policy gateway run whether I am watching or the benchmark is.

Detection needs three independent statistical tests to agree before it opens an incident, so it will not react to one noisy window.

And there it is. The first thing the page reports is not a stack trace. It is the merchant revenue at risk, per hour, priced from this merchant's own traffic.
```

### Clip 04 — Diagnosis and recommendation

`docs/vo/04-diagnosis-and-recommendation.txt` · 90 words · about 35s · starts at 1:13

> **On screen:** Open the incident. Hero, then scroll to Recommended recovery. Hold on the history line.

```text
Underneath is the cause. UPI on one payment service provider, with a confidence score and the evidence behind it.

Now the part that makes this a product rather than a dashboard. The agent recommends shifting a share of UPI traffic off the degraded route, and states what it expects that to be worth, what it is risking, and what happened the last time it tried this.

Eight of nine comparable incidents improved. That is not a language model remembering. It is a count over structured records of what actually happened.
```

### Clip 05 — Exposure and options

`docs/vo/05-exposure-and-options.txt` · 67 words · about 26s · starts at 1:49

> **On screen:** Pan to the exposure panel on the right, then scroll to the strategy table.

```text
And unprompted, it tells me what happens if it is wrong. Only that share of traffic moves. The rest stays put, as a control group. If it does not help, it is undone automatically.

It also shows every recovery it did not pick, and what each was worth. The objective is not to maximise success rate. It is to maximise recovered revenue inside the merchant's risk limits.
```

### Clip 06 — The policy gate

`docs/vo/06-the-policy-gate.txt` · 54 words · about 21s · starts at 2:16

> **On screen:** Highlight the policy line, then click Approve on camera.

```text
Every action passes a deterministic policy gateway. Plain Python, evaluating the merchant's own configuration file. No model anywhere in that path.

Here it clamps the traffic shift down to what merchant policy allows, and holds it for a human, because confidence sits below the merchant's autonomous floor.

AI proposes. Policy decides. The system verifies.
```

### Clip 07 — Verification

`docs/vo/07-verification.txt` · 101 words · about 39s · starts at 2:38

> **On screen:** Scroll to Recovery verification. Hold on the control and treatment figures.

```text
This is the part I would point at first.

We do not claim recovery just because payment success improved. When the agent moved that traffic, it deliberately left the rest alone. So there is a control group living through the same outage, the same hour, the same customers.

The gap between those two numbers is the intervention. Not the incident recovering on its own.

That distinction matters, because those two situations call for opposite responses. Without a control group, a fix applied during a worsening outage looks like it caused the damage, and the system rolls back something that was working.
```

### Clip 08 — The money

`docs/vo/08-the-money.txt` · 63 words · about 24s · starts at 3:19

> **On screen:** Scroll to Revenue actually recovered. Hold on the four figures.

```text
So the ledger closes. Revenue at risk. Revenue protected. Revenue recovered from payments that had already failed. And the remainder, lost.

Those figures are disjoint by construction. They sum to the exposure exactly. The benchmark asserts that identity on every single incident, because a system reporting a hundred and thirty percent recovery rate has not had a good day. It has a bug.
```

### Clip 09 — Learning

`docs/vo/09-learning.txt` · 88 words · about 34s · starts at 3:44

> **On screen:** What the agent learned, then the Learning page and its playbook.

```text
When the incident closes, the whole thing becomes one structured record.

On the Learning page that becomes a playbook. For this failure signature the system now prefers a moderate traffic shift, and it says what to avoid. The larger shift helped in only two of five comparable incidents, and three of those had to be rolled back.

To be straight with you, fourteen of these records are seeded demo history, and the interface labels them as seeded. But they match this failure on merit. Nothing was special cased.
```

### Clip 10 — Prevention and rollback

`docs/vo/10-prevention-and-rollback.txt` · 69 words · about 27s · starts at 4:20

> **On screen:** Scroll to Prevention opportunities.

```text
The last step is prevention. This pattern has recurred often enough to be worth stopping rather than recovering, so the system proposes preventing it. That is a recommendation, not an action.

There is also a scenario where every UPI provider fails at once, so the obvious reroute cannot possibly work. The agent tries it, measures no improvement against the control, reverts its own change, and escalates with the evidence.
```

### Clip 11 — Benchmark and close

`docs/vo/11-benchmark-and-close.txt` · 73 words · about 28s · starts at 4:48

> **On screen:** How it works, or the landing page proof strip.

```text
Across the benchmark. Zero policy violations. Zero unauthorised executions. Every rollback succeeded. Detection precision, ninety nine point five percent. Root cause accuracy, ninety one percent. Median time to mitigate, one hundred and thirty six seconds, against a modelled human baseline of twenty seven minutes.

Every one of those is generated by a reproducible harness. None of it is typed into a slide.

Detect. Recover. Verify. Learn. Recover the revenue, not just the incident.
```

## Assembling

1. Generate the eleven clips, keep the file names.
2. Drop `demo.webm` on the video track.
3. Place each clip at the `starts_at` second from `timing.json`, not from the table above. The
   table is the plan; the JSON is what actually happened on your take.
4. Leave the gaps silent. They are breathing room, and they absorb small differences between the
   generated length of a clip and its budget.

If a clip runs long, trim the sentence marked optional in its text file rather than speeding up the
audio. Sped-up TTS is instantly recognisable.

## What to do if a take goes differently

The simulator is stochastic, so runs differ. None of the following is a failure, and the narration
is written to survive all of them without a re-record:

| What happens | Why the narration still fits |
|---|---|
| Policy auto-approves instead of asking | Clip 06 says policy decides, not that it always asks. The driver logs which happened. |
| Verification returns partially recovered | Clip 07 never claims full recovery. It describes the comparison, not the verdict. |
| Verification is inconclusive | Same. Consider re-running for a cleaner result, but it is honest either way. |
| No prevention recommendation appears | Clip 10 describes what prevention is. If the panel is empty, hold on the Learning page instead. |
| Different rupee figures than a previous take | Nothing in the audio quotes them. This is the whole point of the rule above. |
