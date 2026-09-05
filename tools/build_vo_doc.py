"""Assemble docs/DEMO_SCRIPT_VO.md from the clip files in docs/vo/.

The clip text files are the source of truth: they are what gets pasted into ElevenLabs, so the
document is generated from them rather than maintained alongside them. Otherwise the two drift and
the recording ends up matching neither.
"""

from __future__ import annotations

import pathlib
import re

VO = pathlib.Path("docs/vo")
WPM = 155.0  # ElevenLabs at speed 1.0, conversational delivery
GAP_S = 1.2  # breath between clips

# What is on screen while each clip plays. Keyed by clip number.
SHOTS = {
    "01": "Landing page, top. Headline and the product screenshot.",
    "02": "Scroll down to the loop band. Let the eight steps sit on screen.",
    "03": "Simulate, click UPI PSP route degradation, then Overview. Cut the dead wait in the edit; land on the incident appearing.",
    "04": "Open the incident. Hero, then scroll to Recommended recovery. Hold on the history line.",
    "05": "Pan to the exposure panel on the right, then scroll to the strategy table.",
    "06": "Highlight the policy line, then click Approve on camera.",
    "07": "Scroll to Recovery verification. Hold on the control and treatment figures.",
    "08": "Scroll to Revenue actually recovered. Hold on the four figures.",
    "09": "What the agent learned, then the Learning page and its playbook.",
    "10": "Scroll to Prevention opportunities.",
    "11": "How it works, or the landing page proof strip.",
}

HEAD = """# Five-minute submission script, voice-over version

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

"""

TAIL = """
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
"""


def main() -> None:
    clips = sorted(VO.glob("[0-9][0-9]-*.txt"))
    if not clips:
        raise SystemExit("no clip files in docs/vo -- nothing to build from")

    rows, at = [], 0.0
    for path in clips:
        num = path.name[:2]
        title = path.stem[3:].replace("-", " ").capitalize()
        body = path.read_text(encoding="utf-8").strip()
        words = len(re.findall(r"[A-Za-z0-9']+", body))
        secs = words / WPM * 60
        rows.append((num, title, path.name, body, words, secs, at))
        at += secs + GAP_S

    def ts(s: float) -> str:
        return f"{int(s) // 60}:{int(s) % 60:02d}"

    out = [HEAD]
    out.append("| Starts | Clip | Beat | Spoken | On screen |")
    out.append("|---|---|---|---|---|")
    for num, title, _f, _b, words, secs, start in rows:
        out.append(f"| **{ts(start)}** | {num} | {title} | {secs:.0f}s | {SHOTS.get(num, '')} |")
    total_words = sum(r[4] for r in rows)
    out.append("")
    out.append(
        f"**Total {ts(at)}** across {len(rows)} clips, {total_words} words of narration "
        f"at {WPM:.0f} words per minute."
    )
    out.append("")
    out.append("## The clips")
    out.append("")
    out.append("Each is also a file in [`vo/`](vo/), ready to paste.")
    out.append("")
    for num, title, fname, body, words, secs, start in rows:
        out.append(f"### Clip {num} — {title}")
        out.append("")
        out.append(f"`docs/vo/{fname}` · {words} words · about {secs:.0f}s · starts at {ts(start)}")
        out.append("")
        out.append(f"> **On screen:** {SHOTS.get(num, '')}")
        out.append("")
        out.append("```text")
        out.append(body)
        out.append("```")
        out.append("")
    out.append(TAIL.strip())

    dest = pathlib.Path("docs/DEMO_SCRIPT_VO.md")
    dest.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"{dest}  ({len(rows)} clips, {total_words} words, {ts(at)})")


if __name__ == "__main__":
    main()
