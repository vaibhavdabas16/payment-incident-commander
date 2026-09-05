"""Recapture the README/doc screenshots from a running server.

Runs in its own session id, so it builds and completes a throwaway incident without touching the
world you are demoing in. Nothing here is staged: it triggers a real scenario, approves it if
policy asks, waits for verification, and photographs whatever the agent actually did.

    python -m uvicorn pic.api.main:app --port 8000
    python tools/capture_ui.py
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
SESSION = "shots"
SCENARIO = "SCN-UPI-PSP"
OUT = pathlib.Path("docs/images")
VIEW: dict = {"width": 1560, "height": 940}


def api(path: str, method: str = "GET"):
    sep = "&" if "?" in path else "?"
    req = urllib.request.Request(f"{BASE}{path}{sep}session={SESSION}", method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"null")


def wait_for(what: str, probe, timeout: float):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        try:
            got = probe()
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
            got = None
        if got:
            return got
        time.sleep(1.0)
    print(f"  ! timed out waiting for {what}")
    return None


def build_world() -> str | None:
    print(f"building a throwaway world in session={SESSION}")
    api("/api/control/reset", "POST")
    seeded = api("/api/demo/seed-history", "POST")
    print(f"  seeded {seeded.get('added')} records")
    api("/api/control/speed?speedup=10", "POST")
    api(f"/api/scenarios/{SCENARIO}/trigger", "POST")

    inc = wait_for("an incident", lambda: (api("/api/incidents") or {}).get("incidents") or None, 240)
    if not inc:
        return None
    iid = inc[0]["incident_id"]
    print(f"  {iid} opened")

    wait_for("a proposal", lambda: (api(f"/api/incidents/{iid}") or {}).get("proposal"), 120)

    # The approval gate opens a moment after the proposal exists, so sampling the state once (as
    # this did) usually catches the incident mid-transition, skips the approval, and leaves it
    # parked forever -- which is why the verification and revenue shots came out empty.
    def gate():
        d = api(f"/api/incidents/{iid}") or {}
        if d.get("state") == "AWAITING_HUMAN_APPROVAL":
            return "approve"
        if d.get("action_result"):
            return "acted"
        return None

    decision = wait_for("the policy gate", gate, 150)
    if decision == "approve":
        try:
            api(f"/api/incidents/{iid}/approve", "POST")
            print("  approved")
        except urllib.error.HTTPError as exc:
            print(f"  ! approve {exc.code}")
    elif decision == "acted":
        print("  policy acted without needing a human")
    else:
        print("  ! never reached the policy gate")

    v = wait_for("verification", lambda: (api(f"/api/incidents/{iid}") or {}).get("verification"), 300)
    if v:
        print(f"  verification: {v.get('status')} (control group: {bool(v.get('control_used'))})")
    wait_for("close", lambda: (api(f"/api/incidents/{iid}") or {}).get("state") == "CLOSED" or None, 180)

    # The control-group comparison is the thing the README points at, so an incident that fell back
    # to before/after is the wrong photograph -- accurate about itself, but it illustrates the
    # opposite of the caption. Say so and let the caller try again.
    if not (v or {}).get("control_used"):
        print("  ! no control group on this incident; the verification shot would show the "
              "before/after fallback instead")
        return None
    return iid


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("pip install playwright && playwright install chromium")
        return 2
    try:
        api("/api/health")
    except Exception as exc:
        print(f"no server on {BASE} ({exc})")
        return 2

    iid = None
    for attempt in range(1, 4):
        print(f"attempt {attempt}")
        iid = build_world()
        if iid:
            break
    if not iid:
        print("could not get an incident with a control group after 3 attempts")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    shot_count = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--force-color-profile=srgb"])
        ctx = browser.new_context(viewport=VIEW, device_scale_factor=2)
        page = ctx.new_page()

        def go(hash_route: str, settle: int = 3500):
            page.goto(f"{BASE}/?session={SESSION}{hash_route}", wait_until="domcontentloaded")
            page.wait_for_timeout(settle)

        def full(name: str):
            nonlocal shot_count
            page.screenshot(path=str(OUT / name), full_page=False)
            shot_count += 1
            print(f"  {name}")

        def region(name: str, *needles: str):
            """Photograph the card containing one of these headings."""
            nonlocal shot_count
            ok = page.evaluate(
                """(needles) => {
                    const sel = 'h1,h2,h3,.section-h,.rec-eyebrow,.verify-eyebrow';
                    const els = [...document.querySelectorAll(sel)];
                    document.querySelectorAll('[data-shot]').forEach(e => e.removeAttribute('data-shot'));
                    for (const n of needles) {
                      const hit = els.find(e => (e.textContent||'').toLowerCase().includes(n.toLowerCase()));
                      if (!hit) continue;
                      let node = hit;
                      for (let i = 0; i < 6 && node; i++) {
                        if (node.classList.contains('card') || node.classList.contains('verify')
                            || node.classList.contains('rec')) break;
                        node = node.parentElement;
                      }
                      (node || hit).setAttribute('data-shot', '1');
                      (node || hit).scrollIntoView({block: 'center'});
                      return true;
                    }
                    return false;
                }""",
                list(needles),
            )
            if not ok:
                print(f"  ! skipped {name}: no match for {needles}")
                return
            page.wait_for_timeout(700)
            try:
                page.locator("[data-shot='1']").first.screenshot(path=str(OUT / name))
                shot_count += 1
                print(f"  {name}")
            except Exception as exc:
                print(f"  ! {name}: {exc}")

        print("\ncapturing:")
        # Landing is behind the "entered" flag, so it needs the bare URL.
        page.goto(f"{BASE}/?session={SESSION}", wait_until="networkidle")
        page.wait_for_timeout(3500)
        full("ui-landing.png")

        go("#/app/command")
        full("ui-overview.png")

        go("#/app/simulate")
        full("ui-simulate.png")

        go(f"#/app/incidents/{iid}", settle=4500)
        full("ui-incident-story.png")
        region("ui-region-hero.png", "Payment success rate", "success rate down")
        region("ui-region-recommendation.png", "Recommended recovery")
        region("ui-region-strategies.png", "Every recovery it considered")
        region("ui-region-verification.png", "Was the revenue actually recovered", "Recovery verification")
        region("ui-region-revenue.png", "Revenue actually recovered")
        region("ui-region-learning.png", "What the agent learned")

        go("#/app/learning")
        full("ui-learning.png")

        go("#/app/health")
        full("ui-health.png")

        go("#/app/proof")
        full("ui-proof.png")

        browser.close()

    print(f"\n{shot_count} screenshots written to {OUT}")
    print(f"photographed incident {iid} in session={SESSION} (your demo world is untouched)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
