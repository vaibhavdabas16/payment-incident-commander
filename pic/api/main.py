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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import settings
from ..engine import Engine, EngineConfig
from ..schemas import IncidentState
from ..simulation.scenarios import SCENARIOS

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


hub = Hub()
engine = Engine(EngineConfig(reasoner=None), emit=hub.publish)
_sim_task: asyncio.Task | None = None
_state = {"running": False, "speedup": settings.live_speedup}


async def _simulation_loop() -> None:
    """Advance the world and run a detection cycle every monitoring interval."""
    interval = 1.0
    while True:
        try:
            await asyncio.sleep(interval)
            if not _state["running"]:
                continue
            seconds = interval * float(_state["speedup"])
            # Run the blocking simulation step off the event loop so the WebSocket stays responsive.
            incident = await asyncio.to_thread(_advance_and_detect, seconds)
            if incident is not None:
                await asyncio.to_thread(engine.supervisor.run_incident, incident)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let one bad cycle kill the live demo
            hub.publish("engine_error", {"error": f"{type(exc).__name__}: {exc}"})


def _advance_and_detect(seconds: float):
    engine.advance(seconds)
    return engine.supervisor.observe()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sim_task
    hub.loop = asyncio.get_running_loop()
    await asyncio.to_thread(engine.warmup, 45)
    _state["running"] = True
    _sim_task = asyncio.create_task(_simulation_loop())
    try:
        yield
    finally:
        if _sim_task:
            _sim_task.cancel()


app = FastAPI(title="Payment Incident Commander", version="1.0.0", lifespan=lifespan)
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
        "reasoner": engine.reasoner.name,
        "events_generated": len(engine.store),
        "simulated_time": engine.now.isoformat(),
        "running": _state["running"],
        "speedup": _state["speedup"],
    }


@app.get("/api/metrics")
def metrics() -> dict[str, Any]:
    return engine.current_metrics()


@app.get("/api/series")
def series(window_seconds: int = 60, count: int = 60) -> dict[str, Any]:
    return {"points": engine.success_rate_series(window_seconds, count)}


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
            for i in reversed(engine.incidents())
        ]
    }


@app.get("/api/incidents/{incident_id}")
def incident_detail(incident_id: str) -> dict[str, Any]:
    for i in engine.incidents():
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
                    a.scenario.scenario_id == s.scenario_id for a in engine.simulator.active
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
    "decision": "Chooses an action by expected value, and may propose doing nothing.",
    "action": "The only agent that can call a write tool, and only with a policy decision naming that exact action.",
    "verification": "Measures the action against a concurrent control group and reports honestly when it did not work.",
    "escalation": "Hands the incident to a human with the evidence and the reason it stopped.",
}


@app.get("/api/agents")
def agents() -> dict[str, Any]:
    """The agent pipeline, in the order the supervisor runs it."""
    s = engine.supervisor
    ordered = [
        s.detection_agent,
        s.investigation_agent,
        s.impact_agent,
        s.root_cause_agent,
        s.decision_agent,
        s.action_agent,
        s.verification_agent,
        s.escalation_agent,
    ]
    return {
        "agents": [
            {
                "name": a.name,
                "state": a.state.value,
                "timeout_s": a.timeout_s,
                # Only the Action Agent holds a write capability, and the tool registry refuses it
                # without an approving policy decision.
                "writes": a.name == "action",
                "summary": _AGENT_SUMMARY.get(a.name, ""),
            }
            for a in ordered
        ]
    }


@app.post("/api/scenarios/{scenario_id}/trigger")
def trigger(scenario_id: str) -> dict[str, Any]:
    if scenario_id not in SCENARIOS:
        raise HTTPException(status_code=404, detail=f"unknown scenario {scenario_id}")
    scenario = engine.trigger(scenario_id)
    return {"triggered": scenario.scenario_id, "name": scenario.name}


@app.post("/api/incidents/{incident_id}/approve")
async def approve(incident_id: str, approver: str = "operator") -> dict[str, Any]:
    incident = _find(incident_id)
    if incident.state is not IncidentState.AWAITING_HUMAN_APPROVAL:
        raise HTTPException(status_code=409, detail="incident is not awaiting approval")
    await asyncio.to_thread(engine.supervisor.approve, incident, approver)
    return {"incident_id": incident_id, "state": incident.state.value, "outcome": incident.outcome}


@app.post("/api/incidents/{incident_id}/reject")
async def reject(incident_id: str, approver: str = "operator") -> dict[str, Any]:
    incident = _find(incident_id)
    if incident.state is not IncidentState.AWAITING_HUMAN_APPROVAL:
        raise HTTPException(status_code=409, detail="incident is not awaiting approval")
    await asyncio.to_thread(engine.supervisor.reject, incident, approver)
    return {"incident_id": incident_id, "state": incident.state.value, "outcome": incident.outcome}


@app.post("/api/control/{command}")
def control(command: str, speedup: float | None = None) -> dict[str, Any]:
    if command == "pause":
        _state["running"] = False
    elif command == "resume":
        _state["running"] = True
    elif command == "speed" and speedup is not None:
        _state["speedup"] = max(1.0, min(600.0, speedup))
    elif command == "reset":
        # A shared demo needs a way back to a clean baseline. Without it, scenarios accumulate:
        # three overlapping degradations leave the merchant at 55% success, and every new
        # injection correlates into the incident already open instead of showing up as its own -
        # so the button looks broken when it is working exactly as designed.
        engine.simulator.deactivate_all()
        engine.supervisor.incidents.clear()
        engine.supervisor._incident_seq = 0
        engine.detector._degraded_periods.clear()
    else:
        raise HTTPException(status_code=400, detail=f"unknown command {command}")
    return {"running": _state["running"], "speedup": _state["speedup"]}


@app.get("/api/notifications")
def notifications() -> dict[str, Any]:
    return {"notifications": engine.notifications(), "tickets": engine.tickets()}


@app.get("/api/policy")
def policy() -> dict[str, Any]:
    return engine.gateway.policy


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
    for i in engine.incidents():
        if i.incident_id == incident_id:
            return i
    raise HTTPException(status_code=404, detail=f"unknown incident {incident_id}")


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------


@app.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    await ws.accept()
    hub.clients.add(ws)
    try:
        # Replay recent history so a client joining mid-incident sees the lifecycle so far.
        await ws.send_text(json.dumps({"kind": "replay", "events": hub.buffer[-120:]}, default=str))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        hub.clients.discard(ws)


# --------------------------------------------------------------------------
# Static dashboard (built SPA), mounted last so it never shadows the API
# --------------------------------------------------------------------------

if WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB_DIST / "index.html")
