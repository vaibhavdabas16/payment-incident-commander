"""FastAPI backend.

Serves the command centre and streams the agent lifecycle over a WebSocket, so the dashboard
renders what the agents are actually doing rather than a scripted animation. Every payload here is
a `model_dump` of the same typed contract the agents produced.

The simulation runs in a background task at a configurable speed-up, which is what makes the demo
live: an operator triggers a scenario, and detection, diagnosis, policy review and verification
unfold on screen in the order they really happen.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import settings
from ..integration.tenant import registry
from .v1 import router as v1_router
from ..engine import Engine, EngineConfig
from ..schemas import IncidentState
from ..simulation.scenarios import SCENARIOS, Effect, Scenario

WEB_DIST = Path(__file__).resolve().parent.parent.parent / "web" / "dist"


class Hub:
    """Fan-out for lifecycle events, with a replay buffer for clients that connect late."""

    def __init__(self, history: int = 400) -> None:
        self.clients: set[WebSocket] = set()
        self.buffer: list[dict[str, Any]] = []
        self.history = history
        self.loop: asyncio.AbstractEventLoop | None = None

    def publish(self, kind: str, payload: dict[str, Any]) -> None:
        event = {"kind": kind, **payload}
        self.buffer.append(event)
        if len(self.buffer) > self.history:
            self.buffer = self.buffer[-self.history :]
        if self.loop is None:
            return
        # Agents run on the simulation thread, so hand the send back to the event loop.
        asyncio.run_coroutine_threadsafe(self._broadcast(event), self.loop)

    async def _broadcast(self, event: dict[str, Any]) -> None:
        dead = []
        for client in list(self.clients):
            try:
                await client.send_text(json.dumps(event, default=str))
            except Exception:
                dead.append(client)
        for client in dead:
            self.clients.discard(client)


# One simulation per visitor. A single shared engine meant one person's injected scenario
# degraded everyone else's payments and their Reset wiped everyone else's incidents - fine for a
# single operator, wrong for a link several people open at once.
#
# It is affordable because an event costs about 560 bytes rather than the 3.2KB it did as a
# pydantic model: a warmed engine holding two simulated hours is roughly 19MB, so the cap below
# fits comfortably in a small container with room for the interpreter and request handling.
MAX_SESSIONS = 8
SESSION_TTL_S = 900
# A world with nobody looking at it stops. The dashboard polls while a tab is open, so any session
# being watched is touched several times a minute; one that goes quiet for longer than this has
# been abandoned, and advancing it only steals time from the sessions that are being watched.
ACTIVE_WINDOW_S = 75.0
# Detection reads a two-minute window, so sweeping it more often than once a simulated minute
# cannot see anything that was not there before - and it costs ~1.3s, which is the entire tick
# budget. Scheduled in simulated seconds so the cadence does not change with the speedup.
DETECT_EVERY_SIM_S = 60.0
# How far the world may catch up in a single tick when the loop falls behind. Without a cap, a
# stall produces one huge step, which costs proportionally more to generate and detect over, which
# makes the next tick later still.
MAX_CATCHUP_TICKS = 3.0


@dataclass
class Session:
    """One visitor's world: their own simulator, detector, agents and event stream."""

    session_id: str
    engine: Engine
    hub: Hub
    running: bool = True
    speedup: float = field(default_factory=lambda: settings.live_speedup)
    last_seen: float = field(default_factory=time.monotonic)
    # Wall clock of the last advance, and simulated seconds owed to the detector. Both are
    # bookkeeping for the loop; nothing else reads them.
    advanced_at: float = field(default_factory=time.monotonic)
    sim_since_detect: float = 0.0

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    def is_watched(self, now: float) -> bool:
        return now - self.last_seen <= ACTIVE_WINDOW_S


_sessions: dict[str, Session] = {}
_sessions_lock = threading.Lock()
_current: ContextVar[Session] = ContextVar("current_session")
_sim_task: asyncio.Task | None = None
# Background tasks other than the simulation loop, cancelled together on shutdown.
_tasks: list[asyncio.Task] = []


def _build_session(session_id: str) -> Session:
    """Create and warm a session. Blocking - callers hand this to a worker thread."""
    hub = Hub()
    engine = Engine(
        # The live dashboard paces itself between agent steps. Simulated waits are instantaneous,
        # so without this the pipeline finishes in milliseconds and a visitor watching sees the
        # verdict without ever seeing the work that produced it.
        EngineConfig(reasoner=None, step_pause_s=settings.live_step_pause_s),
        emit=hub.publish,
    )
    engine.warmup(45)
    return Session(session_id=session_id, engine=engine, hub=hub)


def _evict_locked() -> None:
    """Drop idle sessions, then the oldest, so a shared link cannot exhaust the container."""
    now = time.monotonic()
    for sid, sess in list(_sessions.items()):
        if sid != "public" and now - sess.last_seen > SESSION_TTL_S:
            del _sessions[sid]
    while len(_sessions) > MAX_SESSIONS:
        oldest = min((s for s in _sessions.values() if s.session_id != "public"),
                     key=lambda s: s.last_seen, default=None)
        if oldest is None:
            break
        del _sessions[oldest.session_id]


def get_session(session_id: str) -> Session:
    with _sessions_lock:
        sess = _sessions.get(session_id)
        if sess is not None:
            was_idle = not sess.is_watched(time.monotonic())
            sess.touch()
            if was_idle:
                # It stopped while nobody was watching. Resume from now rather than replaying the
                # gap, which the visitor did not see and does not want to wait for.
                sess.advanced_at = time.monotonic()
            return sess
    built = _build_session(session_id)
    with _sessions_lock:
        existing = _sessions.get(session_id)
        if existing is not None:
            return existing
        _sessions[session_id] = built
        _evict_locked()
    return built


def eng() -> Engine:
    """The engine belonging to the request being handled."""
    return _current.get().engine


def sess() -> Session:
    return _current.get()


async def _live_loop() -> None:
    """Sweep each configured merchant's real traffic on their own monitoring interval.

    Nothing here advances a clock: the world moves because payments are ingested by other
    requests. All this does is ask, on a schedule, whether what has arrived looks wrong — and if it
    does, run the same eight agents the demo runs.
    """
    if not registry.configured:
        return
    while True:
        await asyncio.sleep(1.0)
        now = time.monotonic()
        for tenant in registry.all():
            if now - tenant.last_detected_at < tenant.monitoring_interval_s:
                continue
            tenant.last_detected_at = now
            try:
                # Both calls block: detection is a full statistical sweep, and running an incident
                # genuinely waits real seconds before verifying. Neither belongs on the event loop.
                incident = await asyncio.to_thread(tenant.engine.supervisor.observe)
                if incident is not None:
                    await asyncio.to_thread(tenant.engine.supervisor.run_incident, incident)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one merchant's bad cycle must not stop the others
                print(f"[live] {tenant.merchant_id}: {type(exc).__name__}: {exc}", flush=True)


async def _simulation_loop() -> None:
    """Advance the worlds someone is watching, and detect on simulated time."""
    interval = 1.0
    while True:
        await asyncio.sleep(interval)
        now = time.monotonic()
        for session in sessions_to_advance(now):
            try:
                # Advance by the time that actually passed, so a slow tick catches up instead of
                # quietly slowing the simulated world down.
                elapsed = min(now - session.advanced_at, interval * MAX_CATCHUP_TICKS)
                session.advanced_at = now
                seconds = elapsed * float(session.speedup)
                if seconds <= 0:
                    continue
                session.sim_since_detect += seconds
                detect = session.sim_since_detect >= DETECT_EVERY_SIM_S
                if detect:
                    session.sim_since_detect = 0.0
                # Off the event loop so the WebSocket stays responsive.
                incident = await asyncio.to_thread(_advance_and_detect, session, seconds, detect)
                if incident is not None:
                    await asyncio.to_thread(session.engine.supervisor.run_incident, incident)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one bad cycle must not kill anyone else's session
                session.hub.publish("engine_error", {"error": f"{type(exc).__name__}: {exc}"})


def sessions_to_advance(now: float) -> list[Session]:
    """The worlds that should move this tick: running, and someone is looking at them.

    Also the expiry sweep. It used to run only when a new session was created, so an abandoned
    world could keep a detector sweeping forever - and detection is ~1.3s per session per tick
    against a 1s tick, so a handful of abandoned worlds is enough to stall every live one.
    """
    live = []
    with _sessions_lock:
        for sid, session in list(_sessions.items()):
            if sid != "public" and now - session.last_seen > SESSION_TTL_S:
                del _sessions[sid]
                continue
            if session.running and session.is_watched(now):
                live.append(session)
    return live


def _advance_and_detect(session: Session, seconds: float, detect: bool = True):
    session.engine.advance(seconds)
    return session.engine.supervisor.observe() if detect else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sim_task
    loop = asyncio.get_running_loop()
    # "public" is the fallback for a client that sends no session id. Warmed up front so the
    # first request does not pay for it.
    public = await asyncio.to_thread(get_session, "public")
    public.hub.loop = loop
    _sim_task = asyncio.create_task(_simulation_loop())
    merchants = registry.load()
    if merchants:
        print(f"[live] serving {len(merchants)} merchant(s): {', '.join(merchants)}", flush=True)
        _live_task = asyncio.create_task(_live_loop())
        _tasks.append(_live_task)
    try:
        yield
    finally:
        for task in _tasks:
            task.cancel()
        _tasks.clear()
        if _sim_task:
            _sim_task.cancel()


app = FastAPI(title="Payment Incident Commander", version="1.0.0", lifespan=lifespan)
# The integration API. Authenticated per merchant, and inert until one is configured.
app.include_router(v1_router)


@app.middleware("http")
async def bind_session(request: Request, call_next):
    """Resolve which visitor's simulation this request belongs to."""
    sid = request.query_params.get("session") or request.headers.get("x-session-id") or "public"
    sid = re.sub(r"[^A-Za-z0-9_-]", "", sid)[:64] or "public"
    with _sessions_lock:
        found = _sessions.get(sid)
        if found is not None:
            found.touch()
    if found is None:
        # Building one warms 45 minutes of traffic, so keep it off the event loop.
        found = await asyncio.to_thread(get_session, sid)
        found.hub.loop = asyncio.get_running_loop()
    token = _current.set(found)
    try:
        return await call_next(request)
    finally:
        _current.reset(token)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# REST
# --------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "reasoner": eng().reasoner.name,
        "events_generated": len(eng().store),
        "simulated_time": eng().now.isoformat(),
        "running": sess().running,
        "speedup": sess().speedup,
    }


@app.get("/api/metrics")
def metrics() -> dict[str, Any]:
    return eng().current_metrics()


@app.get("/api/series")
def series(window_seconds: int = 60, count: int = 60) -> dict[str, Any]:
    return {"points": eng().success_rate_series(window_seconds, count)}


@app.get("/api/incidents")
def incidents() -> dict[str, Any]:
    return {
        "incidents": [
            {
                "incident_id": i.incident_id,
                "state": i.state.value,
                "severity": i.severity.value,
                "title": i.title,
                "opened_at": i.opened_at.isoformat(),
                "closed_at": i.closed_at.isoformat() if i.closed_at else None,
                "outcome": i.outcome,
                "root_cause": i.root_cause.most_likely_root_cause if i.root_cause else None,
                "confidence": i.root_cause.confidence if i.root_cause else None,
                "revenue_at_risk_per_hour_paise": (
                    i.impact.revenue_at_risk_per_hour_paise if i.impact else 0
                ),
                "revenue_protected_per_hour_paise": i.revenue_protected_per_hour_paise,
                "awaiting_approval": i.state is IncidentState.AWAITING_HUMAN_APPROVAL,
            }
            for i in reversed(eng().incidents())
        ]
    }


@app.get("/api/incidents/{incident_id}")
def incident_detail(incident_id: str) -> dict[str, Any]:
    for i in eng().incidents():
        if i.incident_id == incident_id:
            return i.model_dump(mode="json")
    raise HTTPException(status_code=404, detail=f"unknown incident {incident_id}")


@app.get("/api/scenarios")
def scenarios() -> dict[str, Any]:
    return {
        "scenarios": [
            {
                "scenario_id": s.scenario_id,
                "name": s.name,
                "description": s.description,
                "root_cause_id": s.root_cause_id,
                "recommended_action": s.recommended_action,
                "duration_s": s.duration_s,
                # Lets the dashboard say what each scenario is *for* without hardcoding a list
                # that can drift away from the scenarios themselves. A scenario that injects no
                # effects is the restraint test; one whose fallback is unhealthy is the
                # failed-intervention case that forces a rollback.
                "injects_failure": bool(s.effects),
                "fallback_healthy": s.fallback_healthy,
                # So the dashboard can show what is already running, rather than letting a
                # visitor stack degradations without realising it.
                "active": any(
                    a.scenario.scenario_id == s.scenario_id for a in eng().simulator.active
                ),
            }
            for s in SCENARIOS.values()
        ]
    }


# One line per agent for the dashboard's fleet view. The name, state and timeout are read from
# the live agent objects; only the prose is written here, because the classes carry no docstring
# to lift it from.
_AGENT_SUMMARY = {
    "detection": "Watches the payment stream and opens an incident only when three independent tests agree.",
    "investigation": "Pulls evidence from read-only tools and separates real second faults from echoes of the first.",
    "impact": "Prices the degradation per hour over a disjoint traffic partition, showing every step of the derivation.",
    "root_cause": "Scores hypotheses from the evidence, then lets the reasoner re-rank - never invent - and cites findings.",
    "recovery_strategy": "Prices every way out and attaches what happened last time each one was tried.",
    "decision": "Chooses an action by expected value, and may propose doing nothing.",
    "action": "The only agent that can call a write tool, and only with a policy decision naming that exact action.",
    "verification": "Measures the action against a concurrent control group and reports honestly when it did not work.",
    "order_recovery": "Goes after the payments that already failed, under its own policy decision.",
    "escalation": "Hands the incident to a human with the evidence and the reason it stopped.",
}


@app.get("/api/health/segments")
def segment_health(minutes: int = 5) -> dict[str, Any]:
    """Live health per payment method, provider and issuer.

    Real measurements from the event store against each segment's own baseline - the dashboard
    shows where a degradation actually sits rather than a decorative status board.
    """
    from datetime import timedelta

    end = eng().simulator.now
    start = end - timedelta(minutes=minutes)
    base = eng().detector.baseline(end)

    def rows(dimension: str) -> list[dict[str, Any]]:
        out = []
        for stat in eng().store.segment_stats(
            start, end, dimension, base.start, base.end, min_volume=15
        ):
            deviation = stat.deviation
            # Bands mirror the detector's severity thinking, so the colour on screen and the
            # threshold that opens an incident cannot drift apart.
            if deviation is None:
                status = "unknown"
            elif deviation <= -settings.detection.severity_bands[1]:
                status = "critical"
            elif deviation <= -settings.detection.severity_bands[0]:
                status = "degraded"
            elif deviation <= -0.02:
                status = "watch"
            else:
                status = "healthy"
            out.append(
                {
                    "segment": stat.segment.dimensions.get(dimension),
                    "success_rate": round(stat.success_rate, 4),
                    "baseline_success_rate": (
                        round(stat.baseline_success_rate, 4)
                        if stat.baseline_success_rate is not None
                        else None
                    ),
                    "deviation": round(deviation, 4) if deviation is not None else None,
                    "transactions": stat.total,
                    "failure_share": round(stat.failure_share, 4),
                    "status": status,
                }
            )
        return sorted(out, key=lambda r: r["deviation"] if r["deviation"] is not None else 0)

    return {
        "window_minutes": minutes,
        "payment_method": rows("payment_method"),
        "psp": rows("psp"),
        "issuer": rows("issuer"),
    }


@app.get("/api/agents")
def agents() -> dict[str, Any]:
    """The agent pipeline, in the order the supervisor runs it."""
    s = eng().supervisor
    ordered = [
        s.detection_agent,
        s.investigation_agent,
        s.impact_agent,
        s.root_cause_agent,
        s.strategy_agent,
        s.decision_agent,
        s.action_agent,
        s.verification_agent,
        s.order_recovery_agent,
        s.escalation_agent,
    ]
    return {
        "agents": [
            {
                "name": a.name,
                "state": a.state.value,
                "timeout_s": a.timeout_s,
                # Two agents hold a write capability, and the tool registry refuses both of them
                # without an approving policy decision naming that exact action.
                "writes": a.name in ("action", "order_recovery"),
                "summary": _AGENT_SUMMARY.get(a.name, ""),
            }
            for a in ordered
        ]
    }


# What a custom incident may be built from: the dimensions the simulator actually generates, and
# the failure codes the agents know how to reason about. Anything outside this is rejected rather
# than quietly ignored, so a request cannot ask for a degradation the simulator will not produce.
def _simulation_options() -> dict[str, Any]:
    from ..simulation.generator import (
        BASELINE_ERRORS, GEO_MIX, ISSUER_MIX, METHOD_MIX, ROUTES,
    )

    psps = sorted({r["psp"] for r in ROUTES.values()})
    gateways = sorted({r["gateway"] for r in ROUTES.values()})
    return {
        "dimensions": {
            "payment_method": sorted(METHOD_MIX),
            "psp": psps,
            "issuer": sorted(ISSUER_MIX),
            "geography": sorted(GEO_MIX),
            "route_id": sorted(ROUTES),
            "gateway": gateways,
        },
        "error_codes": sorted(
            set(BASELINE_ERRORS)
            | {
                "PSP_UNAVAILABLE", "GATEWAY_TIMEOUT", "BANK_UNAVAILABLE", "ISSUER_DECLINE",
                "RISK_RULE_DECLINE", "CHECKOUT_CALLBACK_TIMEOUT",
            }
        ),
        "limits": {
            "severity_pct": [5, 90],
            "duration_minutes": [2, 60],
        },
    }


@app.get("/api/simulation/options")
def simulation_options() -> dict[str, Any]:
    """The segments and failure codes a custom incident can be built from."""
    return _simulation_options()


# The cause each dimension implies, so a custom incident carries a sensible label. It is never
# scored: the benchmark runs the nine designed scenarios, and a custom one is a live exercise.
_CAUSE_FOR = {
    "payment_method": "payment_method_degradation",
    "psp": "psp_degradation",
    "issuer": "issuer_degradation",
    "geography": "issuer_degradation",
    "route_id": "gateway_degradation",
    "gateway": "gateway_degradation",
}


class CustomIncident(BaseModel):
    """A degradation described by an operator rather than chosen from the shipped set."""

    dimension: str
    value: str
    severity_pct: float = Field(40, ge=5, le=90)
    duration_minutes: int = Field(12, ge=2, le=60)
    error_code: str = "PSP_UNAVAILABLE"
    payment_method: str | None = None


@app.post("/api/scenarios/custom")
def custom_scenario(body: CustomIncident) -> dict[str, Any]:
    """Build and run an incident from an operator's own parameters.

    It is injected into the same simulator and handled by the same detector, agents, policy
    gateway and tools as the shipped scenarios - there is no separate path for it, which is the
    point: the pipeline either copes with a degradation it was never designed around or it does
    not, in front of you.
    """
    options = _simulation_options()
    allowed = options["dimensions"]
    if body.dimension not in allowed:
        raise HTTPException(status_code=400, detail=f"unknown dimension {body.dimension!r}")
    if body.value not in allowed[body.dimension]:
        raise HTTPException(
            status_code=400,
            detail=f"unknown {body.dimension} {body.value!r}; expected one of {allowed[body.dimension]}",
        )
    if body.error_code not in options["error_codes"]:
        raise HTTPException(status_code=400, detail=f"unknown error code {body.error_code!r}")
    if body.payment_method and body.payment_method not in allowed["payment_method"]:
        raise HTTPException(status_code=400, detail=f"unknown payment method {body.payment_method!r}")

    match: dict[str, Any] = {body.dimension: body.value}
    # Narrowing by method is what makes a realistic incident: one issuer on cards, not that issuer
    # everywhere it appears.
    if body.payment_method and body.dimension != "payment_method":
        match["payment_method"] = body.payment_method

    where = " · ".join(f"{k}={v}" for k, v in match.items())
    scenario = Scenario(
        scenario_id="SCN-CUSTOM",
        name=f"Custom: {where}",
        description=(
            f"Operator-defined degradation: {where} loses {body.severity_pct:.0f}% of its success "
            f"rate for {body.duration_minutes} minutes, failing with {body.error_code}."
        ),
        root_cause_id=_CAUSE_FOR.get(body.dimension, "payment_method_degradation"),
        start_offset_s=0,
        duration_s=body.duration_minutes * 60,
        effects=[
            Effect(
                match=match,
                success_multiplier=max(0.05, 1.0 - body.severity_pct / 100.0),
                error_code=body.error_code,
            )
        ],
        recommended_action="escalate",
    )
    eng().trigger(scenario)
    return {"triggered": scenario.scenario_id, "name": scenario.name, "description": scenario.description}


@app.post("/api/scenarios/{scenario_id}/trigger")
def trigger(scenario_id: str) -> dict[str, Any]:
    if scenario_id not in SCENARIOS:
        raise HTTPException(status_code=404, detail=f"unknown scenario {scenario_id}")
    scenario = eng().trigger(scenario_id)
    return {"triggered": scenario.scenario_id, "name": scenario.name}


@app.post("/api/incidents/{incident_id}/approve")
async def approve(incident_id: str, approver: str = "operator") -> dict[str, Any]:
    incident = _find(incident_id)
    if incident.state is not IncidentState.AWAITING_HUMAN_APPROVAL:
        raise HTTPException(status_code=409, detail="incident is not awaiting approval")
    await asyncio.to_thread(eng().supervisor.approve, incident, approver)
    return {"incident_id": incident_id, "state": incident.state.value, "outcome": incident.outcome}


@app.post("/api/incidents/{incident_id}/reject")
async def reject(incident_id: str, approver: str = "operator") -> dict[str, Any]:
    incident = _find(incident_id)
    if incident.state is not IncidentState.AWAITING_HUMAN_APPROVAL:
        raise HTTPException(status_code=409, detail="incident is not awaiting approval")
    await asyncio.to_thread(eng().supervisor.reject, incident, approver)
    return {"incident_id": incident_id, "state": incident.state.value, "outcome": incident.outcome}


@app.post("/api/incidents/{incident_id}/acknowledge")
async def acknowledge(
    incident_id: str, who: str = "operator", note: str = ""
) -> dict[str, Any]:
    """A person has taken this on. Not a resolution — a statement that somebody owns it."""
    incident = _find(incident_id)
    try:
        await asyncio.to_thread(eng().supervisor.acknowledge, incident, who, note)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"incident_id": incident_id, "state": incident.state.value, "outcome": incident.outcome}


@app.post("/api/incidents/{incident_id}/override")
async def override(incident_id: str, who: str = "operator", reason: str = "") -> dict[str, Any]:
    """Run the action policy refused, on a named person's authority.

    Refused for rules that exist to prevent harm rather than to express uncertainty — the
    supervisor decides which, so this cannot be widened by calling a different endpoint.
    """
    incident = _find(incident_id)
    try:
        await asyncio.to_thread(eng().supervisor.override, incident, who, reason)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"incident_id": incident_id, "state": incident.state.value, "outcome": incident.outcome}


@app.post("/api/incidents/{incident_id}/retry")
async def retry(incident_id: str, who: str = "operator") -> dict[str, Any]:
    """Look again against the traffic that has arrived since."""
    incident = _find(incident_id)
    try:
        await asyncio.to_thread(eng().supervisor.retry, incident, who)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"incident_id": incident_id, "state": incident.state.value, "outcome": incident.outcome}


@app.post("/api/control/{command}")
def control(command: str, speedup: float | None = None) -> dict[str, Any]:
    if command == "pause":
        sess().running = False
    elif command == "resume":
        sess().running = True
    elif command == "speed" and speedup is not None:
        sess().speedup = max(1.0, min(600.0, speedup))
    elif command == "reset":
        # A shared demo needs a way back to a clean baseline. Without it, scenarios accumulate:
        # three overlapping degradations leave the merchant at 55% success, and every new
        # injection correlates into the incident already open instead of showing up as its own -
        # so the button looks broken when it is working exactly as designed.
        engine = eng()
        engine.simulator.deactivate_all()
        engine.supervisor.incidents.clear()
        engine.supervisor._incident_seq = 0
        engine.detector._degraded_periods.clear()
        # The gateway's rate limiter counts by simulated time, and Reset does not rewind the
        # clock. So actions from before a reset kept counting against the merchant's four-per-hour
        # cap, and a visitor who reset and immediately ran another scenario watched policy refuse
        # an action for a reason nothing on screen could explain any more.
        engine.gateway.history.clear()
        # Notifications and tickets are the audit surface of incidents that no longer exist.
        engine.tool_context.notifications.clear()
        engine.tool_context.tickets.clear()
        # What the system learned belongs to the incidents that are being cleared. Leaving it
        # would mean the next incident after a Reset is judged against history from a world that
        # no longer exists - and the closed-loop demo could never be run twice from a clean start.
        engine.memory.clear()
        engine.supervisor.prevention.clear()
        engine.supervisor._prevention_status.clear()
        engine.supervisor._recovery_attempted.clear()
    else:
        raise HTTPException(status_code=400, detail=f"unknown command {command}")
    return {"running": sess().running, "speedup": sess().speedup}


@app.get("/api/learning")
def learning() -> dict[str, Any]:
    """What the system has learned, and what it wants to prevent next.

    Every record here was written by the supervisor at the close of a real incident in this
    session. Nothing on this endpoint is generated for display.
    """
    return eng().learning_summary()


@app.get("/api/prevention")
def prevention() -> dict[str, Any]:
    """Recurring-failure patterns and the preventive changes they argue for.

    Advisory. Merchant policy is `pic/policies/merchant_policies.yaml`, and nothing reachable from
    this API can change it (ADR-011).
    """
    return {
        "recommendations": eng().prevention(),
        "policy_authority": (
            "Recommendations are documents. Applying one means a person editing the merchant "
            "policy file; approving here records the request and changes nothing."
        ),
    }


@app.post("/api/prevention/{recommendation_id}/{decision}")
def prevention_decision(
    recommendation_id: str, decision: str, who: str = "operator"
) -> dict[str, Any]:
    """Record a person's decision on a preventive recommendation. Never applies it."""
    if decision not in ("accept", "dismiss"):
        raise HTTPException(status_code=400, detail="decision must be accept or dismiss")
    try:
        recommendation = eng().supervisor.acknowledge_prevention(
            recommendation_id, who=who, accept=decision == "accept"
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "recommendation": recommendation.model_dump(mode="json"),
        "policy_changed": False,
        "note": (
            "Recorded. Merchant policy is unchanged: applying this means editing "
            "pic/policies/merchant_policies.yaml."
        ),
    }


@app.get("/api/incidents/{incident_id}/trace")
def incident_trace(incident_id: str) -> dict[str, Any]:
    """The decision trace: observation to learning, one step at a time.

    Each stage carries the structured evidence behind it, so every claim on screen can be opened
    and checked against the thing that produced it rather than taken on trust.
    """
    from .trace import build_trace

    for incident in eng().incidents():
        if incident.incident_id == incident_id:
            return build_trace(incident)
    raise HTTPException(status_code=404, detail=f"unknown incident {incident_id}")


@app.get("/api/notifications")
def notifications() -> dict[str, Any]:
    return {"notifications": eng().notifications(), "tickets": eng().tickets()}


@app.get("/api/policy")
def policy() -> dict[str, Any]:
    return eng().gateway.policy


@app.get("/api/evaluation")
def evaluation() -> Any:
    """The latest reproducible benchmark, or a clear instruction to generate it.

    The dashboard never renders numbers that were not produced by the harness.
    """
    path = settings.results_dir / "latest.json"
    if not path.exists():
        return JSONResponse(
            status_code=404,
            content={
                "detail": "No evaluation results yet.",
                "hint": "Run: python -m pic.evaluation.harness",
            },
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _find(incident_id: str):
    for i in eng().incidents():
        if i.incident_id == incident_id:
            return i
    raise HTTPException(status_code=404, detail=f"unknown incident {incident_id}")


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------


@app.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    await ws.accept()
    sid = re.sub(r"[^A-Za-z0-9_-]", "", ws.query_params.get("session") or "public")[:64] or "public"
    session = await asyncio.to_thread(get_session, sid)
    session.hub.loop = asyncio.get_running_loop()
    session.hub.clients.add(ws)
    try:
        # Replay recent history so a client joining mid-incident sees the lifecycle so far.
        await ws.send_text(json.dumps({"kind": "replay", "events": session.hub.buffer[-120:]}, default=str))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        session.hub.clients.discard(ws)


# --------------------------------------------------------------------------
# Static dashboard (built SPA), mounted last so it never shadows the API
# --------------------------------------------------------------------------

if WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB_DIST / "index.html")

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon() -> FileResponse:
        """Named explicitly rather than served by a root catch-all.

        A `/{path}` route here would sit in front of every API path added later, and would answer
        404s with the SPA. One file is served because one file is asked for.
        """
        icon = WEB_DIST / "favicon.svg"
        if not icon.is_file():
            raise HTTPException(status_code=404, detail="favicon not built")
        return FileResponse(icon, media_type="image/svg+xml")
