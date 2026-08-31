"""Accept real payment events and put them where the detector can see them.

The simulator writes straight into the store because it is the only writer and its output is
trusted by construction. Real traffic is neither: the same payment arrives twice because a webhook
was retried, timestamps arrive skewed because a sender's clock is wrong, and a bad deploy at the
merchant can start sending garbage mid-stream. Each of those is handled here, per event, with a
reason the sender can act on — a batch is never accepted or refused as a whole, because one
malformed row out of five hundred should not cost the other four hundred and ninety-nine.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

from ..store import EventStore
from .wire import MAX_AGE, MAX_CLOCK_SKEW, IngestEvent, IngestResult, Rejection

# Payment ids remembered for duplicate suppression. Sized well past a busy merchant's throughput
# over the retention window, and bounded so a long-running process cannot grow without limit.
DEDUPE_CAPACITY = 200_000


class EventIngestor:
    """Validates, de-duplicates and stores incoming payment events for one merchant."""

    def __init__(self, store: EventStore, merchant_id: str) -> None:
        self.store = store
        self.merchant_id = merchant_id
        self._seen: set[str] = set()
        self._order: deque[str] = deque()
        self.total_accepted = 0
        self.total_rejected = 0
        self.total_duplicates = 0
        self.last_event_at: datetime | None = None

    def _remember(self, payment_id: str) -> None:
        self._seen.add(payment_id)
        self._order.append(payment_id)
        while len(self._order) > DEDUPE_CAPACITY:
            self._seen.discard(self._order.popleft())

    def ingest(self, events: list[IngestEvent], now: datetime | None = None) -> IngestResult:
        """Store what is valid and report precisely what was not."""
        now = now or datetime.now(timezone.utc)
        result = IngestResult()

        for index, incoming in enumerate(events):
            when = incoming.timestamp.astimezone(timezone.utc)

            if when > now + MAX_CLOCK_SKEW:
                result.rejected.append(
                    Rejection(
                        index=index,
                        payment_id=incoming.payment_id,
                        error=(
                            f"timestamp is {when.isoformat()}, more than "
                            f"{int(MAX_CLOCK_SKEW.total_seconds())}s in the future — check the "
                            f"sending host's clock"
                        ),
                    )
                )
                continue

            if when < now - MAX_AGE:
                result.rejected.append(
                    Rejection(
                        index=index,
                        payment_id=incoming.payment_id,
                        error=(
                            f"timestamp is older than the {int(MAX_AGE.total_seconds() // 3600)}h "
                            f"retention window, so it cannot inform a live baseline"
                        ),
                    )
                )
                continue

            # A retried webhook must not count the same payment twice: a duplicated failure is a
            # degradation that never happened.
            if incoming.payment_id in self._seen:
                result.duplicates += 1
                continue

            try:
                event = incoming.to_event(self.merchant_id)
            except (ValueError, TypeError) as exc:
                result.rejected.append(
                    Rejection(index=index, payment_id=incoming.payment_id, error=str(exc))
                )
                continue

            self.store.add(event)
            self._remember(incoming.payment_id)
            result.accepted += 1
            result.oldest = when if result.oldest is None else min(result.oldest, when)
            result.newest = when if result.newest is None else max(result.newest, when)

        self.total_accepted += result.accepted
        self.total_duplicates += result.duplicates
        self.total_rejected += len(result.rejected)
        if result.newest is not None:
            self.last_event_at = max(self.last_event_at or result.newest, result.newest)
        return result
