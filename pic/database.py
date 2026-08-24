"""Persistence layer.

SQLAlchemy models (SQLite by default, Postgres compatible — ADR-004). This is the durable
audit record: incidents, every agent step, every tool call, every policy decision, every
executed action.

`ground_truth` lives here too but is deliberately unreachable from the tool registry (ADR-007).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Generator

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


class JSONText(Text):
    """JSON stored as text — portable across SQLite and Postgres without dialect types."""


def dumps(value: Any) -> str:
    return json.dumps(value, default=str)


def loads(raw: str | None) -> Any:
    if not raw:
        return None
    return json.loads(raw)


class PaymentEventRow(Base):
    __tablename__ = "payment_events"

    payment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(64))
    timestamp: Mapped[datetime] = mapped_column(DateTime)
    merchant_id: Mapped[str] = mapped_column(String(64))
    amount_paise: Mapped[int] = mapped_column(Integer)
    payment_method: Mapped[str] = mapped_column(String(32))
    gateway: Mapped[str] = mapped_column(String(32))
    psp: Mapped[str] = mapped_column(String(32))
    issuer: Mapped[str] = mapped_column(String(32))
    network: Mapped[str | None] = mapped_column(String(32), nullable=True)
    geography: Mapped[str] = mapped_column(String(16))
    device: Mapped[str] = mapped_column(String(16))
    os: Mapped[str] = mapped_column(String(16))
    app_version: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    is_retry: Mapped[bool] = mapped_column(Boolean, default=False)
    route_id: Mapped[str] = mapped_column(String(32))


Index("ix_events_ts", PaymentEventRow.timestamp)
Index("ix_events_method_ts", PaymentEventRow.payment_method, PaymentEventRow.timestamp)


class ConfigChangeRow(Base):
    __tablename__ = "config_changes"

    change_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime)
    merchant_id: Mapped[str] = mapped_column(String(64))
    component: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text)
    changed_by: Mapped[str] = mapped_column(String(64))
    reversible: Mapped[bool] = mapped_column(Boolean, default=True)


class IncidentRow(Base):
    __tablename__ = "incidents"

    incident_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(Text, default="")
    opened_at: Mapped[datetime] = mapped_column(DateTime)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    root_cause_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    revenue_at_risk_per_hour_paise: Mapped[int] = mapped_column(Integer, default=0)
    revenue_protected_per_hour_paise: Mapped[int] = mapped_column(Integer, default=0)
    time_to_detect_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    time_to_mitigate_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")


class AgentStepRow(Base):
    __tablename__ = "agent_steps"

    step_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    incident_id: Mapped[str] = mapped_column(String(32), index=True)
    agent: Mapped[str] = mapped_column(String(48))
    state: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime)
    ended_at: Mapped[datetime] = mapped_column(DateTime)
    latency_ms: Mapped[float] = mapped_column(Float)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    output: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasoner: Mapped[str | None] = mapped_column(String(32), nullable=True)


class ToolCallRow(Base):
    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[str] = mapped_column(String(32), index=True)
    step_id: Mapped[str] = mapped_column(String(48))
    tool: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[str] = mapped_column(Text, default="{}")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class PolicyDecisionRow(Base):
    __tablename__ = "policy_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(48))
    outcome: Mapped[str] = mapped_column(String(32))
    approved: Mapped[bool] = mapped_column(Boolean)
    requires_human: Mapped[bool] = mapped_column(Boolean, default=False)
    requested_parameters: Mapped[str] = mapped_column(Text, default="{}")
    granted_parameters: Mapped[str] = mapped_column(Text, default="{}")
    bound_by: Mapped[str] = mapped_column(Text, default="[]")
    reason: Mapped[str] = mapped_column(Text, default="")
    decided_at: Mapped[datetime] = mapped_column(DateTime)


class AuditRow(Base):
    __tablename__ = "audit_records"

    audit_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    incident_id: Mapped[str] = mapped_column(String(32), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime)
    action: Mapped[str] = mapped_column(String(48))
    parameters: Mapped[str] = mapped_column(Text, default="{}")
    reason: Mapped[str] = mapped_column(Text, default="")
    approved_by: Mapped[str] = mapped_column(String(48))
    policy_outcome: Mapped[str] = mapped_column(String(32))
    execution_result: Mapped[str] = mapped_column(String(32))
    adapter: Mapped[str] = mapped_column(String(32))
    reversible: Mapped[bool] = mapped_column(Boolean, default=True)
    inverse_action: Mapped[str | None] = mapped_column(Text, nullable=True)


class IncidentMemoryRow(Base):
    __tablename__ = "incident_memory"

    incident_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    summary: Mapped[str] = mapped_column(Text, default="")
    features: Mapped[str] = mapped_column(Text, default="{}")
    root_cause_id: Mapped[str] = mapped_column(String(64), default="")
    root_cause: Mapped[str] = mapped_column(Text, default="")
    action_taken: Mapped[str] = mapped_column(String(48), default="")
    action_parameters: Mapped[str] = mapped_column(Text, default="{}")
    outcome: Mapped[str] = mapped_column(String(32), default="")
    recovery_time_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_protected_per_hour_paise: Mapped[int] = mapped_column(Integer, default=0)
    human_override: Mapped[bool] = mapped_column(Boolean, default=False)
    false_positive: Mapped[bool] = mapped_column(Boolean, default=False)


class GroundTruthRow(Base):
    """Evaluation-only. Never exposed through the tool registry (ADR-007)."""

    __tablename__ = "ground_truth"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scenario_id: Mapped[str] = mapped_column(String(64))
    scenario_name: Mapped[str] = mapped_column(String(128))
    root_cause_id: Mapped[str] = mapped_column(String(64))
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    affected_segment: Mapped[str] = mapped_column(Text, default="{}")
    true_revenue_at_risk_per_hour_paise: Mapped[int] = mapped_column(Integer, default=0)
    recommended_action: Mapped[str] = mapped_column(String(48), default="")
    fallback_healthy: Mapped[bool] = mapped_column(Boolean, default=True)


_engine = None
_SessionFactory = None


def get_engine(url: str | None = None):
    global _engine, _SessionFactory
    if _engine is None or url is not None:
        _engine = create_engine(url or settings.database_url, future=True)
        _SessionFactory = sessionmaker(bind=_engine, future=True, expire_on_commit=False)
    return _engine


def init_db(url: str | None = None, drop: bool = False) -> None:
    engine = get_engine(url)
    if drop:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    if _SessionFactory is None:
        get_engine()
    assert _SessionFactory is not None
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
