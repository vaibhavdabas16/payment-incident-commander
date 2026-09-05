"""Record the five-minute demo video against a locally running server.

Why this exists: the simulator is stochastic, so a hand-driven recording lands its beats at a
different moment every take, and the voice-over never lines up twice. This drives the *real* UI
through fixed beats, waiting on real state rather than on a stopwatch, and holds each beat until
its voice-over budget is spent. The result is a silent video whose beat boundaries match the clips
in `docs/vo/`, plus a timing report saying exactly where each clip starts.

Nothing here is a demo mode inside the product. It is an operator: it clicks what a person would
click and reads what a person would read, through the same API and the same build that ships. No
scenario is faked and no number is injected, so if the agent decides something different this run,
the video shows that.

    python -m uvicorn pic.api.main:app --port 8000        # in another terminal
    python tools/record_demo.py

Output:
    docs/vo/demo.webm     the screen recording, silent
    docs/vo/timing.json   the actual start time of every beat, for aligning the audio
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
OUT = pathlib.Path("docs/vo")
SCENARIO = "SCN-UPI-PSP"

# Simulated seconds per real second while we wait for the world to move. Detection needs three
# statistical tests to agree over real traffic; at 1x that is minutes of dead video.
SPEEDUP = 12.0

VIEWPORT: dict = {"width": 1600, "height": 900}

# Beat budgets are the spoken length of each clip in docs/vo, plus the breath after it. Keep these
# in step with the timing table in DEMO_SCRIPT_VO.md or the audio will drift.
BEATS = [
    ("01", "Opening", 32),
    ("02", "The loop", 10),
    ("03", "Incident opens", 32),
    ("04", "Diagnosis and recommendation", 36),
    ("05", "Exposure and options", 27),
    ("06", "The policy gate", 22),
    ("07", "Verification", 40),
    ("08", "The money", 25),
    ("09", "Learning", 35),
    ("10", "Prevention and rollback", 28),
    ("11", "Benchmark and close", 29),
]

FIND_AND_SCROLL = """(needles) => {
    const sel = 'h1,h2,h3,.card-title,.section-h,.rec-eyebrow,.verify-eyebrow';
    const els = [...document.querySelectorAll(sel)];
    for (const n of needles) {
      const hit = els.find(e => (e.textContent || '').toLowerCase().includes(n.toLowerCase()));
      if (hit) {
        const y = hit.getBoundingClientRect().top + window.scrollY - 90;
        window.scrollTo({ top: y, behavior: 'smooth' });
        return n;
      }
    }
    return null;
}"""


def api(path: str, method: str = "GET"):
    req = urllib.request.Request(f"{BASE}{path}", method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read() or b"null")


def wait_for(what: str, probe, timeout: float):
    """Poll until `probe` returns something truthy. Returns (value, seconds waited)."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        try:
            got = probe()
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
            got = None
        if got:
            return got, time.monotonic() - started
        time.sleep(1.0)
    print(f"    ! timed out after {timeout:.0f}s waiting for {what}")
    return None, time.monotonic() - started


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed:  pip install playwright && playwright install chromium")
        return 2

    try:
        api("/api/health")
    except Exception as exc:
        print(f"no server on {BASE} ({exc}). Start it first:")
        print("  python -m uvicorn pic.api.main:app --port 8000")
        return 2

    OUT.mkdir(parents=True, exist_ok=True)

    # A clean world, then the labelled seeded history the learning beat needs.
    print("resetting the world and loading seeded history")
    api("/api/control/reset", "POST")
    seeded = api("/api/demo/seed-history", "POST")
    print(f"    seeded: {seeded}")
    api(f"/api/control/speed?speedup={SPEEDUP}", "POST")

    timing: list[dict] = []
    t0: float | None = None

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--force-color-profile=srgb"])
        ctx = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=1,
            record_video_dir=str(OUT / "_raw"),
            record_video_size=VIEWPORT,
        )
        page = ctx.new_page()

        def js(script: str):
            try:
                return page.evaluate(script)
            except Exception as exc:  # a beat should never kill the take
                print(f"    ! {exc}")
                return None

        def goto(route: str):
            js(f"window.location.hash = {route!r}")
            page.wait_for_timeout(900)

        def scroll_to(*needles: str):
            """Smooth-scroll to the first heading containing one of these."""
            try:
                hit = page.evaluate(FIND_AND_SCROLL, list(needles))
            except Exception as exc:
                print(f"    ! scroll: {exc}")
                hit = None
            if hit is None:
                print(f"    ! nothing on screen matched {needles}")
            page.wait_for_timeout(1200)

        beats = iter(BEATS)
        state: dict = {}

        def beat(num: str, work=None):
            """Run a beat's actions, then hold until its voice-over budget is spent."""
            nonlocal t0
            b_num, title, budget = next(beats)
            assert b_num == num, f"beat order: expected {b_num}, got {num}"
            started = time.monotonic()
            if t0 is None:
                t0 = started
            at = started - t0
            print(f"  {int(at) // 60}:{int(at) % 60:02d}  clip {num}  {title}  (budget {budget}s)")
            if work:
                work()
            spent = time.monotonic() - started
            if spent > budget:
                print(f"    ! overran by {spent - budget:.0f}s -- audio after this will drift")
            else:
                page.wait_for_timeout(int((budget - spent) * 1000))
            timing.append(
                {
                    "clip": num,
                    "title": title,
                    "starts_at_s": round(at, 1),
                    "budget_s": budget,
                    "actual_s": round(time.monotonic() - started, 1),
                }
            )

        print("\nrecording:")
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(2500)

        # --- 01 the landing page ---------------------------------------------
        beat("01")

        # --- 02 the loop band -------------------------------------------------
        beat("02", lambda: js("window.scrollTo({top: 720, behavior: 'smooth'})"))

        # --- 03 break something, and wait for the incident to open ------------
        def open_incident():
            goto("#/app/command")
            api(f"/api/scenarios/{SCENARIO}/trigger", "POST")
            inc, waited = wait_for(
                "an incident to open",
                lambda: (api("/api/incidents") or {}).get("incidents") or None,
                timeout=150,
            )
            if inc:
                state["id"] = inc[0]["incident_id"]
                print(f"    incident {state['id']} opened after {waited:.0f}s")

        beat("03", open_incident)

        # --- 04 the diagnosis and the recommendation --------------------------
        def show_rec():
            if not state.get("id"):
                return
            goto(f"#/app/incidents/{state['id']}")
            wait_for(
                "a recommendation",
                lambda: (api(f"/api/incidents/{state['id']}") or {}).get("proposal"),
                timeout=60,
            )
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(1500)
            scroll_to("Recommended recovery", "Agent recommendation")

        beat("04", show_rec)

        # --- 05 the exposure panel, then the rejected options -----------------
        def show_options():
            scroll_to("What happens if this is wrong")
            page.wait_for_timeout(6000)
            scroll_to("Every recovery it considered", "Every option it considered")

        beat("05", show_options)

        # --- 06 the policy gate, and approval ---------------------------------
        def approve():
            scroll_to("Recommended recovery")
            page.wait_for_timeout(4000)
            try:
                btn = page.get_by_role("button", name="Approve")
                if btn.count():
                    btn.first.click()
                    print("    approved in the UI")
                elif state.get("id"):
                    api(f"/api/incidents/{state['id']}/approve", "POST")
                    print("    approved via API (no button: policy did not need a human)")
            except Exception as exc:
                print(f"    ! approve: {exc}")

        beat("06", approve)

        # --- 07 verification against the control group ------------------------
        def show_verification():
            if state.get("id"):
                res, waited = wait_for(
                    "verification to return",
                    lambda: (api(f"/api/incidents/{state['id']}") or {}).get("verification"),
                    timeout=180,
                )
                if res:
                    print(f"    verification: {res.get('result')} after {waited:.0f}s")
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(1500)
            scroll_to("Recovery verification", "Was the revenue actually recovered", "Verification")

        beat("07", show_verification)

        # --- 08 the revenue ledger --------------------------------------------
        beat("08", lambda: scroll_to("Revenue actually recovered", "What happened financially"))

        # --- 09 what it learned, then the playbook ----------------------------
        def show_learning():
            scroll_to("What the agent learned")
            page.wait_for_timeout(8000)
            goto("#/app/learning")
            page.wait_for_timeout(1500)
            scroll_to("Learned recovery playbook", "Learned recovery playbooks")

        beat("09", show_learning)

        # --- 10 prevention ------------------------------------------------------
        beat("10", lambda: scroll_to("Prevention opportunities", "Prevention"))

        # --- 11 the benchmark, and the close ------------------------------------
        def close():
            goto("#/app/proof")
            page.wait_for_timeout(9000)
            js("window.scrollTo({top: 900, behavior: 'smooth'})")

        beat("11", close)

        page.wait_for_timeout(1200)
        video = page.video
        ctx.close()
        browser.close()

        if video:
            dest = OUT / "demo.webm"
            if dest.exists():
                dest.unlink()
            pathlib.Path(video.path()).replace(dest)
            print(f"\nvideo  {dest}  ({dest.stat().st_size / 1e6:.1f} MB)")

    (OUT / "timing.json").write_text(json.dumps(timing, indent=2), encoding="utf-8")
    total = timing[-1]["starts_at_s"] + timing[-1]["actual_s"]
    print(f"timing {OUT / 'timing.json'}")
    print(f"total  {int(total) // 60}:{int(total) % 60:02d}")

    over = [t for t in timing if t["actual_s"] > t["budget_s"] + 1]
    if over:
        print("\noverran (the audio drifts from here; re-run, or trim the clip):")
        for t in over:
            print(f"  clip {t['clip']} {t['title']}: {t['actual_s']:.0f}s vs {t['budget_s']}s")
    else:
        print("every beat inside its budget: the voice-over clips line up as written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
