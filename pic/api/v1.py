"""The integration API: what a merchant's systems talk to.

Separate from the demo API on purpose. `/api/...` serves the simulated dashboard, where a session
is a browser tab and anyone may poke it; `/api/v1/...` serves a real merchant, where a request is
authenticated, a payment is real money, and the shape of the response is a promise that has to keep
working. The version is in the path so that promise can be kept while the demo keeps changing.

Authentication is a bearer token per merchant:

    Authorization: Bearer pic_live_...

Every route resolves that to one merchant's engine and touches nothing else. There is no route
here that can read or affect another merchant's data.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from ..integration.tenant import Tenant, registry
from ..integration.wire import IngestBatch
from ..schemas import IncidentState

router = APIRouter(prefix="/api/v1", tags=["integration"])


def _tenant(authorization: str | None) -> Tenant:
    """Resolve the bearer token, or refuse.

    The same 401 for a missing, malformed and wrong key: distinguishing them tells an attacker
    which half of the problem to work on. If no merchant is configured at all the message says so,
    because that is a deployment mistake rather than an authentication one and the operator
    reading it is the person who can fix it.
    """
    if not registry.configured:
        raise HTTPException(
            status_code=503,
            detail=(
                "no merchant is configured on this deployment; set PIC_MERCHANTS (see "
                "docs/INTEGRATION.md)"
            ),
        )
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    tenant = registry.resolve(token)
    if tenant is None:
        raise HTTPException(status_code=401, detail="invalid or missing API key")
    return tenant


def _incident_summary(incident: Any) -> dict[str, Any]:
    return {
        "incident_id": incident.incident_id,
        "state": incident.state.value,
        "severity": incident.severity.value,
        "title": incident.title,
        "opened_at": incident.opened_at.isoformat(),
        "closed_at": incident.closed_at.isoformat() if incident.closed_at else None,
        "outcome": incident.outcome,
        "root_cause": incident.root_cause.most_likely_root_cause if incident.root_cause else None,
        "confidence": incident.root_cause.confidence if incident.root_cause else None,
        "revenue_at_risk_per_hour_paise": (
            incident.impact.revenue_at_risk_per_hour_paise if incident.impact else 0
        ),
        "awaiting_approval": incident.state is IncidentState.AWAITING_HUMAN_APPROVAL,
        "handover": (
            {
                "reason_code": incident.escalation.reason_code,
                "reason": incident.escalation.reason,
                "because": incident.escalation.because,
                "recommended_human_action": incident.escalation.recommended_human_action,
            }
            if incident.escalation
            else None
        ),
    }


@router.post("/events")
async def ingest_events(
    batch: IngestBatch, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    """Report payment attempts.

    Send them as they settle, or in batches on a short timer — anything up to a thousand per call.
    Duplicates are ignored by `payment_id`, so retrying a failed POST is safe and is the correct
    thing to do; the counts in the response always sum to the number of events sent.
    """
    tenant = _tenant(authorization)
    with tenant.lock:
        result = tenant.ingestor.ingest(batch.events)
    return result.as_dict()


@router.get("/incidents")
async def list_incidents(
    authorization: str | None = Header(default=None), limit: int = 50
) -> dict[str, Any]:
    tenant = _tenant(authorization)
    incidents = tenant.engine.incidents()[: max(1, min(limit, 200))]
    return {"incidents": [_incident_summary(i) for i in incidents]}


@router.get("/incidents/{incident_id}")
async def get_incident(
    incident_id: str, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    tenant = _tenant(authorization)
    for incident in tenant.engine.incidents():
        if incident.incident_id == incident_id:
            return incident.model_dump(mode="json")
    raise HTTPException(status_code=404, detail=f"no incident {incident_id}")


@router.post("/incidents/{incident_id}/approve")
async def approve_incident(
    incident_id: str, request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    """Authorise the action the policy gateway held for a human.

    The approver is recorded on the decision and appears in the audit trail. Approving does not
    skip verification: the action still has to prove it worked, and is still reverted if it did not.
    """
    tenant = _tenant(authorization)
    body = await _json(request)
    approver = str(body.get("approver") or "api")
    incident = _find_awaiting(tenant, incident_id)
    tenant.engine.supervisor.approve(incident, approver=approver)
    tenant.engine.supervisor.run_incident(incident)
    return _incident_summary(incident)


@router.post("/incidents/{incident_id}/reject")
async def reject_incident(
    incident_id: str, request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    tenant = _tenant(authorization)
    body = await _json(request)
    incident = _find_awaiting(tenant, incident_id)
    tenant.engine.supervisor.reject(incident, approver=str(body.get("approver") or "api"))
    return _incident_summary(incident)


@router.get("/status")
async def status(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """What this deployment currently knows about your traffic.

    The first thing to call after wiring up ingestion: it says how many events arrived, how many
    were rejected and why that matters, and whether the system is allowed to act or is watching
    read-only.
    """
    tenant = _tenant(authorization)
    payload = tenant.status()
    payload["metrics"] = tenant.engine.current_metrics()
    return payload


def _find_awaiting(tenant: Tenant, incident_id: str):
    for incident in tenant.engine.incidents():
        if incident.incident_id == incident_id:
            if incident.state is not IncidentState.AWAITING_HUMAN_APPROVAL:
                raise HTTPException(
                    status_code=409,
                    detail=f"incident {incident_id} is {incident.state.value}, not awaiting approval",
                )
            return incident
    raise HTTPException(status_code=404, detail=f"no incident {incident_id}")


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}
