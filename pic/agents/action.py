"""Action Agent.

Executes exactly what the policy gateway authorised — never the original proposal. When the gateway
clamps a 30% traffic shift to 20%, this agent executes 20%, because the granted parameters are the
authority and the requested ones are only history.

Every execution writes an `AuditRecord` containing the recorded inverse, so rollback replays a
known-good undo rather than reconstructing prior state from assumption.
"""

from __future__ import annotations

import uuid
from typing import Any

from ..schemas import (
    ActionResult,
    ActionType,
    AgentResult,
    AuditRecord,
    IncidentState,
    utcnow,
)
from ..tools.registry import PolicyViolation
from .base import Agent, IncidentContext

# Actions with no side effect on the payment control plane.
INERT_ACTIONS = {ActionType.NO_ACTION}


class ActionAgent(Agent):
    name = "action"
    state = IncidentState.EXECUTING

    def run(self, ctx: IncidentContext) -> AgentResult:
        incident = ctx.incident
        decision = incident.policy_decision
        proposal = incident.proposal
        if decision is None or proposal is None:
            return AgentResult(ok=False, summary="nothing authorised to execute", error="missing_decision")

        # Defence in depth. The supervisor already refuses this transition without approval, and
        # the registry refuses the tool call, but an execution path should never rely on a single
        # check when the side effects are financial.
        if not decision.approved:
            return AgentResult(
                ok=False,
                summary="refused: action was not approved by the policy gateway",
                error="not_approved",
            )

        if proposal.action in INERT_ACTIONS:
            result = ActionResult(
                incident_id=incident.incident_id,
                action=proposal.action,
                parameters={},
                executed=False,
                success=True,
                adapter="none",
                result_detail={"note": "No intervention required."},
            )
            return AgentResult(ok=True, summary="no action taken (by decision)", output=result)

        parameters = dict(decision.granted_parameters)
        try:
            payload, record = ctx.registry.call(
                proposal.action.value, parameters, approval=decision
            )
        except PolicyViolation as exc:
            return AgentResult(ok=False, summary=f"policy violation: {exc}", error=str(exc))

        if payload is None:
            error = record.error or "tool returned no result"
            result = ActionResult(
                incident_id=incident.incident_id,
                action=proposal.action,
                parameters=parameters,
                executed=True,
                success=False,
                error=error,
            )
            self._audit(ctx, result, decision, proposal.rationale, "failed")
            return AgentResult(
                ok=False, summary=f"execution failed: {error}", output=result, error=error
            )

        result = ActionResult(
            incident_id=incident.incident_id,
            action=proposal.action,
            parameters=parameters,
            executed=True,
            success=True,
            adapter=payload.get("adapter", "simulator"),
            result_detail=payload.get("detail", {}),
            inverse_action=payload.get("inverse_action"),
        )
        self._audit(ctx, result, decision, proposal.rationale, "success")

        # Rate limits and cumulative-shift ceilings are enforced against what actually executed.
        ctx.gateway.history.record_execution(ctx.now, proposal.action, parameters)

        clamped = decision.granted_parameters != decision.requested_parameters
        summary = f"executed {proposal.action.value} with {parameters}"
        if clamped:
            summary += " (clamped by policy)"
        return AgentResult(ok=True, summary=summary, output=result)

    def rollback(self, ctx: IncidentContext) -> AgentResult:
        """Replay the recorded inverse of the last executed action."""
        incident = ctx.incident
        result = incident.action_result
        if result is None or not result.executed:
            return AgentResult(ok=True, summary="nothing to roll back")
        inverse = result.inverse_action
        if not inverse:
            return AgentResult(
                ok=False,
                summary="action has no automatic inverse; human intervention required",
                error="rollback_unavailable",
            )

        from ..schemas import PolicyDecision, PolicyOutcome

        tool_name = inverse["tool"]
        # Rollback is authorised by construction: undoing our own change restores the state the
        # merchant already consented to, and blocking it would strand the system in a worse
        # position than before it acted.
        approval = PolicyDecision(
            incident_id=incident.incident_id,
            action=ActionType(tool_name),
            requested_parameters=dict(inverse.get("arguments", {})),
            granted_parameters=dict(inverse.get("arguments", {})),
            outcome=PolicyOutcome.APPROVE,
            approved=True,
            approved_by="policy_engine:rollback",
            reason="reverting a previously executed intervention to its prior state",
            decided_at=ctx.now,
        )
        try:
            payload, record = ctx.registry.call(
                tool_name, dict(inverse.get("arguments", {})), approval=approval
            )
        except PolicyViolation as exc:
            return AgentResult(ok=False, summary=f"rollback blocked: {exc}", error=str(exc))

        if payload is None:
            error = record.error or "rollback tool returned no result"
            self._audit_raw(
                ctx,
                action=tool_name,
                parameters=dict(inverse.get("arguments", {})),
                reason="rollback of failed intervention",
                approved_by="policy_engine:rollback",
                policy_outcome="APPROVE",
                execution_result="failed",
                adapter="simulator",
                reversible=False,
                inverse=None,
            )
            return AgentResult(ok=False, summary=f"rollback failed: {error}", error="rollback_failed")

        self._audit_raw(
            ctx,
            action=tool_name,
            parameters=dict(inverse.get("arguments", {})),
            reason="rollback of failed intervention",
            approved_by="policy_engine:rollback",
            policy_outcome="APPROVE",
            execution_result="success",
            adapter=payload.get("adapter", "simulator"),
            reversible=False,
            inverse=None,
        )
        return AgentResult(
            ok=True,
            summary=f"rolled back {result.action.value}",
            output={"rolled_back": result.action.value, "detail": payload.get("detail", {})},
        )

    # ---------------------------------------------------------------- audit

    def _audit(
        self,
        ctx: IncidentContext,
        result: ActionResult,
        decision: Any,
        reason: str,
        execution_result: str,
    ) -> None:
        self._audit_raw(
            ctx,
            action=result.action.value,
            parameters=result.parameters,
            reason=reason,
            approved_by=decision.approved_by,
            policy_outcome=decision.outcome.value,
            execution_result=execution_result,
            adapter=result.adapter,
            reversible=result.inverse_action is not None,
            inverse=result.inverse_action,
        )

    def _audit_raw(
        self,
        ctx: IncidentContext,
        *,
        action: str,
        parameters: dict[str, Any],
        reason: str,
        approved_by: str,
        policy_outcome: str,
        execution_result: str,
        adapter: str,
        reversible: bool,
        inverse: dict[str, Any] | None,
    ) -> None:
        record = AuditRecord(
            audit_id=f"aud_{uuid.uuid4().hex[:12]}",
            timestamp=utcnow(),
            incident_id=ctx.incident.incident_id,
            action=action,
            parameters=parameters,
            reason=reason,
            approved_by=approved_by,
            policy_outcome=policy_outcome,
            execution_result=execution_result,
            adapter=adapter,
            reversible=reversible,
            inverse_action=inverse,
        )
        ctx.incident.audit.append(record)
        ctx.publish("audit", record.model_dump(mode="json"))
