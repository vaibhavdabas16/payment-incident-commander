"""The live path: real events in, a real incident out, a real webhook fired.

The simulator is not involved in any of this. Events arrive the way a merchant would send them,
the same detector and the same eight agents run, and the action leaves as a signed HTTP request to
an endpoint under the merchant's control. If this file passes, the system is not a simulation with
an API bolted on — it is the same system with a different source of traffic.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pic.engine import Engine, EngineConfig
from pic.integration.control import ActionRejected, ReadOnlyControlPlane, WebhookControlPlane
from pic.integration.ingest import EventIngestor
from pic.integration.signing import sign, verify
from pic.integration.wire import IngestEvent
from pic.schemas import IncidentState


def _event(index: int, *, ok: bool, when: datetime, method: str = "upi", psp: str = "psp_a"):
    return IngestEvent(
        payment_id=f"pay_{index}",
        timestamp=when,
        amount_paise=250_00,
        payment_method=method,
        status="captured" if ok else "failed",
        psp=psp,
        gateway="gw_main",
        issuer="hdfc",
        route_id="route_a" if psp == "psp_a" else "route_b",
        error_code=None if ok else "PSP_UNAVAILABLE",
    )


def _stream(ingestor: EventIngestor, *, minutes: int, per_minute: int, success_rate: float,
            start: datetime, offset: int = 0, psp: str = "psp_a") -> int:
    """Feed `minutes` of traffic at a given success rate. Returns the next free index."""
    index = offset
    for minute in range(minutes):
        for slot in range(per_minute):
            when = start + timedelta(minutes=minute, seconds=slot * (60 / per_minute))
            ok = (slot / per_minute) < success_rate
            ingestor.ingest([_event(index, ok=ok, when=when, psp=psp)], now=when)
            index += 1
    return index


def _merchant_policy(tmp_path) -> Path:
    """The bundled policy with this merchant's own route names.

    Worth spelling out, because it is the step an integrator will otherwise miss: `eligible_routes`
    in the shipped policy are the simulator's, so a real merchant's routes are not approved
    destinations and every traffic shift is denied until they say what their routes are called.
    """
    source = Path("pic/policies/merchant_policies.yaml").read_text(encoding="utf-8")
    swapped = source.replace("    - route_A\n    - route_B\n    - route_C", "    - route_a\n    - route_b")
    path = tmp_path / "merchant_policy.yaml"
    path.write_text(swapped, encoding="utf-8")
    return path


def test_ingested_traffic_produces_a_real_incident():
    """A degradation reported over the API is detected, priced and diagnosed.

    No simulator: `Engine(live=True)` generates nothing, and every payment the detector sees got
    there by being ingested. Only one route is reported here, so there is nowhere healthy to move
    traffic to and the correct outcome is a handover, not an action — which the assertions check,
    rather than accepting any terminal state.
    """
    engine = Engine(EngineConfig(live=True, reasoner="deterministic"))
    ingestor = EventIngestor(engine.store, "acme")

    # A baseline first. Detection compares against history, so a merchant that starts sending
    # during an outage has no way to know it is one - true of any monitoring system, and worth
    # having a test state plainly.
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=36)
    index = _stream(ingestor, minutes=33, per_minute=40, success_rate=0.94, start=start)
    # Then the degradation, in the window the detector is about to look at. Kept short because a
    # long one drags the rolling baseline down to meet it, which is exactly why detection has to
    # be early to be useful.
    _stream(
        ingestor,
        minutes=3,
        per_minute=40,
        success_rate=0.30,
        start=start + timedelta(minutes=33),
        offset=index,
    )

    assert len(engine.store) > 1_000, "the store should hold everything that was ingested"

    incident = engine.supervisor.observe()
    assert incident is not None, "a 60-point drop in success rate should be detected"
    engine.supervisor.run_incident(incident)

    assert incident.anomaly is not None
    assert incident.impact is not None and incident.impact.revenue_at_risk_per_hour_paise > 0
    assert incident.root_cause is not None and incident.root_cause.confidence > 0
    # With a single route there is no healthy destination, so it must hand over and say so rather
    # than move traffic from a route to itself.
    assert incident.escalation is not None
    assert incident.escalation.because


def test_the_whole_loop_runs_on_ingested_events_and_leaves_as_a_signed_webhook(tmp_path,
                                                                               monkeypatch):
    """Detect, diagnose, ask, act, measure, undo — on external data, over HTTP.

    This is the integration claim end to end. The traffic is ingested, the decision is made by the
    same agents the benchmark scores, the policy gateway holds the action for a human because
    confidence is below the merchant's autonomous floor, and approving it dispatches a signed
    request to the merchant's endpoint. Verification then measures the result and, finding no
    improvement, sends the exact inverse — which is the property that makes autonomous action
    survivable at all.
    """
    from pic.config import settings

    # Live, this stands back for three real minutes while traffic accumulates. The test needs the
    # same code path, not the same wall clock.
    monkeypatch.setattr(settings.verification, "observation_seconds", 2)

    calls: list[dict] = []

    def transport(url, headers, body):
        assert verify("whsec_test_secret", headers["X-PIC-Signature"], body), "unsigned webhook"
        calls.append(json.loads(body))
        return 200, {"weights_after": {"upi": {"route_a": 0.2, "route_b": 0.8}}}

    control = WebhookControlPlane(
        endpoint="https://merchant.example/actions", secret="whsec_test_secret", transport=transport
    )
    engine = Engine(
        EngineConfig(
            live=True,
            control=control,
            reasoner="deterministic",
            policy_path=_merchant_policy(tmp_path),
        )
    )
    ingestor = EventIngestor(engine.store, "acme")

    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=36)
    index = _stream(ingestor, minutes=33, per_minute=20, success_rate=0.94, start=start)
    index = _stream(ingestor, minutes=33, per_minute=20, success_rate=0.95, start=start,
                    offset=index, psp="psp_b")
    # One route collapses; the other keeps working, so there is somewhere to move traffic to.
    index = _stream(ingestor, minutes=3, per_minute=20, success_rate=0.25,
                    start=start + timedelta(minutes=33), offset=index)
    _stream(ingestor, minutes=3, per_minute=20, success_rate=0.95,
            start=start + timedelta(minutes=33), offset=index, psp="psp_b")

    incident = engine.supervisor.observe()
    assert incident is not None
    engine.supervisor.run_incident(incident)

    # Held for a human, and nothing dispatched before that human said yes.
    assert incident.state is IncidentState.AWAITING_HUMAN_APPROVAL, incident.state
    assert calls == [], "no traffic may move before approval"

    # Payments keep arriving while the agent waits and measures — which is the only condition
    # under which verification can conclude anything at all.
    stop = threading.Event()

    def keep_reporting() -> None:
        counter = 900_000
        while not stop.is_set():
            when = datetime.now(timezone.utc)
            batch = []
            for slot in range(20):
                batch.append(
                    _event(counter, ok=slot < 17, when=when, psp="psp_b")
                )
                counter += 1
            ingestor.ingest(batch, now=when)
            time.sleep(0.05)

    feeder = threading.Thread(target=keep_reporting, daemon=True)
    feeder.start()
    try:
        engine.supervisor.approve(incident, approver="ops@acme.example")
        engine.supervisor.run_incident(incident)
    finally:
        stop.set()
        feeder.join(timeout=5)

    actions = [call["action"] for call in calls]
    assert actions[0] == "shift_traffic", actions
    first = calls[0]["parameters"]
    assert first["from_route"] == "route_a" and first["to_route"] == "route_b"

    # It measured its own work rather than declaring victory on having acted.
    assert incident.verification is not None, "an action that is never measured is a guess"

    status = incident.verification.status.value
    if status in ("FAILED", "REGRESSED"):
        # The property that makes autonomous action survivable: an intervention that did not help
        # is undone, with the exact inverse of what was sent.
        assert len(calls) >= 2, f"{status} must be reverted, dispatched {len(calls)}"
        undo = calls[-1]["parameters"]
        assert undo["from_route"] == first["to_route"]
        assert undo["to_route"] == first["from_route"]
        assert undo["percentage"] == first["percentage"]
    elif status == "INCONCLUSIVE":
        # Not enough evidence either way. Reverting on no evidence would be as unjustified as
        # keeping it, so the incident goes to a human instead.
        assert incident.escalation is not None


def test_a_refused_webhook_is_not_treated_as_a_change_that_happened():
    """If the merchant's endpoint says no, nothing may claim the action was applied.

    This is the property the whole verification story rests on. An action recorded as done but
    never applied would be measured against traffic it never touched, and the system would draw a
    conclusion about a change that does not exist.
    """
    control = WebhookControlPlane(
        endpoint="https://merchant.example/actions",
        secret="whsec_test_secret",
        transport=lambda url, headers, body: (503, {"error": "maintenance"}),
    )
    with pytest.raises(ActionRejected) as raised:
        control.shift_traffic("route_a", "route_b", 30.0, "upi")
    assert "503" in str(raised.value)
    # And local state is untouched, so a later rollback cannot undo something that never happened.
    assert control.route_weights == {}


def test_read_only_merchant_cannot_be_made_to_act():
    """The default without an action endpoint: diagnose everything, change nothing."""
    control = ReadOnlyControlPlane()
    for call in (
        lambda: control.shift_traffic("route_a", "route_b", 10.0, "upi"),
        lambda: control.disable_method("upi"),
        lambda: control.configure_retry(3, True),
        lambda: control.rollback_config_change("chg_1"),
    ):
        with pytest.raises(ActionRejected):
            call()


def test_duplicate_and_malformed_events_are_rejected_individually():
    """A retried webhook must not invent an outage, and one bad row must not cost the batch."""
    engine = Engine(EngineConfig(live=True, reasoner="deterministic"))
    ingestor = EventIngestor(engine.store, "acme")
    now = datetime.now(timezone.utc)

    first = ingestor.ingest([_event(1, ok=True, when=now), _event(2, ok=False, when=now)], now=now)
    assert first.accepted == 2 and first.duplicates == 0

    # The same payments again, as a webhook retry would send them.
    again = ingestor.ingest([_event(1, ok=True, when=now), _event(2, ok=False, when=now)], now=now)
    assert again.accepted == 0 and again.duplicates == 2
    assert len(engine.store) == 2, "a duplicate payment must not become a second failure"

    # Out-of-range timestamps are refused with a reason, and the good event still lands.
    mixed = ingestor.ingest(
        [
            _event(3, ok=True, when=now + timedelta(hours=1)),
            _event(4, ok=True, when=now - timedelta(hours=9)),
            _event(5, ok=True, when=now),
        ],
        now=now,
    )
    assert mixed.accepted == 1
    assert len(mixed.rejected) == 2
    assert "future" in mixed.rejected[0].error
    assert "retention" in mixed.rejected[1].error


def test_unknown_payment_status_is_refused_rather_than_guessed():
    """Mapping an unrecognised status to success hides an outage; to failure, invents one."""
    with pytest.raises(ValueError):
        IngestEvent(
            payment_id="p1",
            timestamp=datetime.now(timezone.utc),
            amount_paise=100_00,
            payment_method="upi",
            status="pending_capture_maybe",
        )


def test_webhook_signature_rejects_replays_and_tampering():
    body = b'{"action":"shift_traffic"}'
    header = sign("whsec_test_secret", body, timestamp=1_700_000_000)

    assert verify("whsec_test_secret", header, body, now=1_700_000_060)
    # Outside the tolerance window: a captured request cannot be replayed later.
    assert not verify("whsec_test_secret", header, body, now=1_700_000_000 + 3_600)
    # Body changed after signing.
    assert not verify("whsec_test_secret", header, b'{"action":"disable_method"}', now=1_700_000_060)
    # Wrong secret.
    assert not verify("whsec_other", header, body, now=1_700_000_060)
    assert not verify("whsec_test_secret", "garbage", body, now=1_700_000_060)


def test_integration_api_refuses_unauthenticated_requests():
    """No key, no data. And a deployment with no merchant says so, rather than 401-ing forever."""
    from fastapi.testclient import TestClient

    from pic.api.main import app

    with TestClient(app) as client:
        response = client.get("/api/v1/status")
        # 503 when nothing is configured (this test process configures none), 401 when it is.
        assert response.status_code in (401, 503), response.text
        assert "PIC_MERCHANTS" in response.text or "API key" in response.text
