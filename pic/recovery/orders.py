"""Identifying failed payments that can still be completed.

Deterministic, read-only analysis of the event store. Nothing here writes, nothing here calls a
model, and nothing here estimates a rupee it cannot point at: every figure is the sum of amounts
on payments that actually exist in the store.

Three rules decide whether a failed payment is recoverable, and each one exists to stop the system
promising money it will not get:

1. **The failure has to be the kind a second attempt can clear.** A gateway timeout is worth
   retrying. `BANK_DECLINE` and `ISSUER_DECLINE` are the bank's answer, not a glitch, and retrying
   them adds load and annoys a customer for nothing.

2. **The order must not have already succeeded.** Customers retry by themselves. Counting an order
   that the customer completed thirty seconds later would let the system take credit for revenue
   it never touched — the single easiest way to make these numbers a lie.

3. **The recovery probability is measured, not assumed.** It comes from the retry outcomes already
   in the store for that error code. Where there is not enough of that, a documented per-class
   prior is used and the estimate is marked as such rather than being quietly dressed up.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from ..schemas import RecoverableOrder

# How a failure class is recovered, and whether it is recoverable at all.
#
#   retry            - re-present the same payment; the failure was transient
#   alternate_route  - re-present through a different provider; the original route was the problem
#   payment_link     - the customer has to act, so send them a link to pay another way
#
# Anything absent from this map is treated as a hard decline and is never attempted.
RECOVERY_METHOD = {
    "AUTH_TIMEOUT": "retry",
    "GATEWAY_TIMEOUT": "retry",
    "CHECKOUT_CALLBACK_TIMEOUT": "retry",
    "PSP_UNAVAILABLE": "alternate_route",
    "BANK_UNAVAILABLE": "alternate_route",
    "USER_DROPPED": "payment_link",
    "INSUFFICIENT_FUNDS": "payment_link",
    "CARD_EXPIRED": "payment_link",
    "INVALID_VPA": "payment_link",
}

# Hard declines: an answer from the issuer or a risk rule, not a fault. Listed explicitly so the
# omission is a decision rather than an oversight.
HARD_DECLINES = {"BANK_DECLINE", "ISSUER_DECLINE", "RISK_RULE_DECLINE"}

# Fallback probabilities, used only when the store holds too few retries of that error code to
# measure one. Deliberately pessimistic: a payment the customer has to be chased for mostly does
# not come back.
PRIOR_RECOVERY_PROBABILITY = {
    "retry": 0.55,
    "alternate_route": 0.62,
    "payment_link": 0.18,
}

# Minimum observed retries of an error code before its measured rate is trusted over the prior.
MIN_OBSERVED_RETRIES = 20


def measured_recovery_rates(
    store: Any, start: datetime, end: datetime
) -> dict[str, tuple[float, int]]:
    """Observed retry success rate per error code, as {error_code: (rate, sample)}.

    Measured over the retries the merchant's own traffic already contains, so the probability the
    system quotes for recovering a `GATEWAY_TIMEOUT` is the rate at which this merchant's
    `GATEWAY_TIMEOUT` retries have in fact succeeded.
    """
    attempts: dict[str, int] = defaultdict(int)
    successes: dict[str, int] = defaultdict(int)
    # A retry event carries the outcome of the retry; the error code it was retrying is the one on
    # the original attempt for that order, so retries are matched back to their order.
    original_error: dict[str, str] = {}
    for event in store.slice(start, end):
        if event.status == "failed" and not event.is_retry and event.error_code:
            original_error.setdefault(event.order_id, event.error_code)
    for event in store.slice(start, end):
        if not event.is_retry:
            continue
        code = original_error.get(event.order_id)
        if not code:
            continue
        attempts[code] += 1
        if event.status == "success":
            successes[code] += 1
    return {
        code: (successes[code] / attempts[code], attempts[code])
        for code in attempts
        if attempts[code] > 0
    }


def find_recoverable_orders(
    store: Any,
    start: datetime,
    end: datetime,
    *,
    horizon_end: datetime | None = None,
    limit: int = 20_000,
) -> tuple[list[RecoverableOrder], dict[str, Any]]:
    """Failed payments in [start, end) that are still worth attempting.

    `horizon_end` is how far forward to look for the customer having already succeeded on their
    own; it defaults to `end`. Returns the orders and a small summary of the population they came
    from, so a caller can report "8,900 of 12,430" rather than only the numerator.
    """
    horizon_end = horizon_end or end
    rows = store.slice(start, end)

    failed: dict[str, Any] = {}
    for event in rows:
        if event.status != "failed" or not event.error_code:
            continue
        # One entry per order: a customer who failed three times is one recoverable order, not
        # three, and counting attempts here would inflate both the count and the money.
        current = failed.get(event.order_id)
        if current is None or event.timestamp > current.timestamp:
            failed[event.order_id] = event

    succeeded: set[str] = {
        e.order_id for e in store.slice(start, horizon_end) if e.status == "success"
    }

    rates = measured_recovery_rates(store, start, horizon_end)

    out: list[RecoverableOrder] = []
    hard = 0
    already = 0
    for order_id, event in failed.items():
        if order_id in succeeded:
            already += 1
            continue
        code = event.error_code or ""
        if code in HARD_DECLINES or code not in RECOVERY_METHOD:
            hard += 1
            continue
        method = RECOVERY_METHOD[code]
        measured = rates.get(code)
        if measured is not None and measured[1] >= MIN_OBSERVED_RETRIES:
            probability = round(measured[0], 4)
        else:
            probability = PRIOR_RECOVERY_PROBABILITY[method]
        out.append(
            RecoverableOrder(
                order_id=order_id,
                payment_id=event.payment_id,
                amount_paise=event.amount_paise,
                payment_method=event.payment_method,
                error_code=code,
                failed_at=event.timestamp,
                recovery_method=method,
                recovery_probability=probability,
            )
        )

    # Highest value first: if a cap binds, the merchant should get the expensive orders back.
    out.sort(key=lambda o: o.amount_paise, reverse=True)
    summary = {
        "failed_payments": len(failed),
        "already_completed_by_customer": already,
        "hard_declines": hard,
        "recoverable": len(out),
        "recoverable_value_paise": sum(o.amount_paise for o in out),
        "by_method": _count_by(out, "recovery_method"),
        "measured_error_codes": sorted(c for c, (_, n) in rates.items() if n >= MIN_OBSERVED_RETRIES),
    }
    return out[:limit], summary


def _count_by(orders: list[RecoverableOrder], attribute: str) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for order in orders:
        counts[getattr(order, attribute)] += 1
    return dict(counts)


def expected_value_paise(orders: list[RecoverableOrder]) -> int:
    """Sum of amount x probability. An expectation, and reported as one — never as revenue."""
    return int(sum(o.amount_paise * o.recovery_probability for o in orders))
