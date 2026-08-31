"""The wire format a merchant sends, and how it becomes a `PaymentEvent`.

This is a trust boundary — the only one in the system where data arrives from outside — so unlike
`PaymentEvent`, which the simulator constructs and nothing validates twice, every field here is
checked before it reaches the store. A malformed batch is rejected event by event with a reason,
not silently dropped and not accepted with a plausible-looking default, because a payment quietly
recorded under the wrong method or timestamp corrupts a baseline that later decisions rest on.

The required set is deliberately small. Detection needs a clock, an outcome, and enough dimensions
to slice on; everything else improves diagnosis when present and is `unknown` when it is not. A
dimension that is always `unknown` simply never becomes a hypothesis, so a merchant can start by
sending five fields and add the rest later without changing anything here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..schemas import PaymentEvent

UNKNOWN = "unknown"

# Payment processors do not agree on what a successful payment is called. These are the spellings
# seen in practice; anything else is rejected rather than guessed at, because mapping an unknown
# status to "failed" would invent an outage and mapping it to "success" would hide one.
SUCCESS_STATUSES = {"success", "succeeded", "captured", "authorized", "authorised", "paid"}
FAILURE_STATUSES = {"failed", "failure", "error", "declined", "rejected"}

# Bands must match the simulator's, or a live deployment would slice order value differently from
# the system the benchmark describes.
HIGH_VALUE_PAISE = 50_000_00
LOW_VALUE_PAISE = 500_00

MAX_BATCH = 1_000
# A payment stamped in the future is a clock-skew bug at the sender, and one stamped days ago
# cannot inform a live baseline. Both are refused loudly.
MAX_CLOCK_SKEW = timedelta(minutes=5)
MAX_AGE = timedelta(hours=6)


def amount_band(amount_paise: int) -> str:
    if amount_paise >= HIGH_VALUE_PAISE:
        return "high"
    return "low" if amount_paise < LOW_VALUE_PAISE else "mid"


class IngestEvent(BaseModel):
    """One payment attempt as a merchant reports it.

    Money is integer paise throughout, never a float: a rupee amount that survives a round trip
    through binary floating point is not the amount that was charged.
    """

    payment_id: str = Field(min_length=1, max_length=128)
    timestamp: datetime
    amount_paise: int = Field(gt=0)
    payment_method: str = Field(min_length=1, max_length=64)
    status: str

    order_id: str | None = Field(default=None, max_length=128)
    gateway: str | None = Field(default=None, max_length=64)
    psp: str | None = Field(default=None, max_length=64)
    issuer: str | None = Field(default=None, max_length=64)
    route_id: str | None = Field(default=None, max_length=64)
    geography: str | None = Field(default=None, max_length=64)
    device: str | None = Field(default=None, max_length=64)
    os: str | None = Field(default=None, max_length=64)
    app_version: str | None = Field(default=None, max_length=64)
    network: str | None = Field(default=None, max_length=64)
    error_code: str | None = Field(default=None, max_length=128)
    latency_ms: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    is_retry: bool = False

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str) -> str:
        lowered = value.strip().lower()
        if lowered not in SUCCESS_STATUSES and lowered not in FAILURE_STATUSES:
            raise ValueError(
                f"unknown status {value!r}; send one of "
                f"{sorted(SUCCESS_STATUSES | FAILURE_STATUSES)}"
            )
        return lowered

    @field_validator("timestamp")
    @classmethod
    def _tz_aware(cls, value: datetime) -> datetime:
        # A naive timestamp is read as UTC. Documented, and the alternative — rejecting it — would
        # turn away most senders for a convention they can only discover by being refused.
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    def to_event(self, merchant_id: str) -> PaymentEvent:
        return PaymentEvent(
            payment_id=self.payment_id,
            order_id=self.order_id or self.payment_id,
            timestamp=self.timestamp.astimezone(timezone.utc),
            merchant_id=merchant_id,
            amount_paise=self.amount_paise,
            payment_method=self.payment_method,
            gateway=self.gateway or UNKNOWN,
            psp=self.psp or UNKNOWN,
            issuer=self.issuer or UNKNOWN,
            geography=self.geography or UNKNOWN,
            device=self.device or UNKNOWN,
            os=self.os or UNKNOWN,
            app_version=self.app_version or UNKNOWN,
            status="success" if self.status in SUCCESS_STATUSES else "failed",
            latency_ms=self.latency_ms,
            # Without a route the system can still detect and diagnose, but it cannot shift
            # traffic, because there is nothing named to shift it between. The policy gateway
            # refuses the action rather than inventing a destination.
            route_id=self.route_id or UNKNOWN,
            network=self.network,
            error_code=self.error_code,
            retry_count=self.retry_count,
            is_retry=self.is_retry,
            amount_band=amount_band(self.amount_paise),
        )


class IngestBatch(BaseModel):
    events: list[IngestEvent] = Field(min_length=1, max_length=MAX_BATCH)


class Rejection(BaseModel):
    index: int
    payment_id: str | None = None
    error: str


class IngestResult(BaseModel):
    """What happened to a batch. Counts always sum to the number of events sent."""

    accepted: int = 0
    duplicates: int = 0
    rejected: list[Rejection] = Field(default_factory=list)
    oldest: datetime | None = None
    newest: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
