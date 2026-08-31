"""One merchant's live deployment: their engine, their ingestion, their credentials.

Configured entirely from the environment, so no key or webhook secret is ever written to disk in
this repository:

    PIC_MERCHANTS='[{"merchant_id":"acme","api_key":"pic_live_...",
                     "action_endpoint":"https://acme.example.com/pic/actions",
                     "action_secret":"whsec_..."}]'

or `PIC_MERCHANTS_FILE=/run/secrets/merchants.json` with the same JSON, for platforms that mount
secrets as files. A merchant with no `action_endpoint` runs read-only: it detects, investigates,
prices and diagnoses, and every attempt to act fails closed and is handed to a human. That is the
sensible way to start, and it is the default for exactly that reason.

Each merchant gets their own engine, and therefore their own event store, detector baseline, policy
gateway and incident history. Nothing is shared between merchants except the process.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pathlib import Path

from ..config import settings
from ..engine import Engine, EngineConfig
from .control import ReadOnlyControlPlane, WebhookControlPlane
from .ingest import EventIngestor

# How often a live merchant's traffic is swept for anomalies. The detector reads a two-minute
# window, so sweeping faster cannot see anything new; the default matches the monitoring interval
# a merchant would configure in their own alerting.
DEFAULT_MONITORING_INTERVAL_S = 60.0
MIN_API_KEY_LENGTH = 24


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass
class Tenant:
    """One merchant, live."""

    merchant_id: str
    api_key: str
    engine: Engine
    ingestor: EventIngestor
    monitoring_interval_s: float = DEFAULT_MONITORING_INTERVAL_S
    can_act: bool = False
    has_own_policy: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_detected_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def status(self) -> dict[str, Any]:
        store = self.engine.store
        return {
            "merchant_id": self.merchant_id,
            "mode": "live",
            "can_act": self.can_act,
            "acting": self.engine.control.snapshot(),
            "events_stored": len(store),
            "events_accepted": self.ingestor.total_accepted,
            "events_duplicate": self.ingestor.total_duplicates,
            "events_rejected": self.ingestor.total_rejected,
            "first_event_at": _iso(store.first_timestamp()),
            "last_event_at": _iso(store.last_timestamp()),
            "policy": {
                "source": "merchant" if self.has_own_policy else "bundled default",
                "eligible_routes": sorted(
                    self.engine.gateway.policy.get("routing", {}).get("eligible_routes", [])
                ),
                "warning": (
                    None
                    if self.has_own_policy
                    else (
                        "using the bundled example policy, whose approved routes are the demo's. "
                        "Traffic-shifting actions will be denied until policy_file names your own "
                        "routes. See docs/INTEGRATION.md."
                    )
                ),
            },
            "monitoring_interval_s": self.monitoring_interval_s,
            "open_incidents": len(self.engine.open_incidents()),
            "incidents": len(self.engine.incidents()),
            "connected_since": self.created_at.isoformat(),
        }


class TenantRegistry:
    """Resolves an API key to a merchant. Empty unless the deployment configures one."""

    def __init__(self) -> None:
        self._tenants: dict[str, Tenant] = {}
        self._lock = threading.Lock()
        self._loaded = False

    # ------------------------------------------------------------------ load

    def load(self) -> list[str]:
        """Build a tenant per configured merchant. Returns the merchant ids created."""
        with self._lock:
            if self._loaded:
                return list(self._tenants and {t.merchant_id for t in self._tenants.values()})
            self._loaded = True
            for entry in _configured_merchants():
                tenant = self._build(entry)
                if tenant is not None:
                    self._tenants[tenant.api_key] = tenant
            return [t.merchant_id for t in self._tenants.values()]

    def _build(self, entry: dict[str, Any]) -> Tenant | None:
        merchant_id = str(entry.get("merchant_id") or "").strip()
        api_key = str(entry.get("api_key") or "").strip()
        if not merchant_id or not api_key:
            return None
        if len(api_key) < MIN_API_KEY_LENGTH:
            # Refusing is safer than warning. A short key on a public endpoint is not a
            # configuration preference, it is an open door.
            raise ValueError(
                f"api_key for {merchant_id!r} is shorter than {MIN_API_KEY_LENGTH} characters"
            )

        endpoint = str(entry.get("action_endpoint") or "").strip()
        secret = str(entry.get("action_secret") or "").strip()
        if endpoint and not secret:
            raise ValueError(
                f"{merchant_id!r} has an action_endpoint but no action_secret; an unsigned "
                f"webhook that moves payment traffic is not something to ship"
            )

        control: Any
        if endpoint:
            control = WebhookControlPlane(endpoint=endpoint, secret=secret)
        else:
            control = ReadOnlyControlPlane()

        # Deterministic by default, not "auto". A live payment system must not have its decisions
        # gated on a third-party API being reachable: when the model is slow the agent hits its
        # timeout and the incident escalates unactioned, even though a perfectly good proposal was
        # available locally the whole time. It is also the path the benchmark measures. A merchant
        # who wants the model can ask for it with "reasoner": "auto".
        reasoner = str(entry.get("reasoner") or "deterministic")
        policy_file = str(entry.get("policy_file") or "").strip()
        policy_path = Path(policy_file) if policy_file else None
        if policy_path is not None and not policy_path.is_file():
            raise ValueError(f"policy_file for {merchant_id!r} does not exist: {policy_path}")
        engine = Engine(
            EngineConfig(
                live=True, control=control, reasoner=reasoner, policy_path=policy_path
            )
        )
        return Tenant(
            merchant_id=merchant_id,
            api_key=api_key,
            engine=engine,
            ingestor=EventIngestor(engine.store, merchant_id),
            monitoring_interval_s=float(
                entry.get("monitoring_interval_s") or DEFAULT_MONITORING_INTERVAL_S
            ),
            can_act=bool(endpoint),
            has_own_policy=policy_path is not None,
        )

    # --------------------------------------------------------------- lookup

    def resolve(self, api_key: str | None) -> Tenant | None:
        """Constant-time lookup, so a wrong key cannot be found by timing."""
        if not api_key:
            return None
        for key, tenant in self._tenants.items():
            if hmac.compare_digest(key, api_key):
                return tenant
        return None

    def all(self) -> list[Tenant]:
        return list(self._tenants.values())

    @property
    def configured(self) -> bool:
        return bool(self._tenants)


def _configured_merchants() -> list[dict[str, Any]]:
    raw = os.getenv("PIC_MERCHANTS")
    path = os.getenv("PIC_MERCHANTS_FILE")
    if not raw and path:
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read()
        except OSError as exc:
            raise ValueError(f"PIC_MERCHANTS_FILE could not be read: {exc}") from exc
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"merchant configuration is not valid JSON: {exc}") from exc
    if isinstance(parsed, dict):
        parsed = parsed.get("merchants", [])
    if not isinstance(parsed, list):
        raise ValueError("merchant configuration must be a JSON list, or an object with 'merchants'")
    return [entry for entry in parsed if isinstance(entry, dict)]


registry = TenantRegistry()


def observation_seconds() -> float:
    """How long verification stands back before measuring. Real seconds in a live deployment."""
    return float(settings.verification.observation_seconds)
