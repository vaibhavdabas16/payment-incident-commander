"""Forward your payments to a Payment Incident Commander deployment.

Adapt `read_payments` to your source — a database cursor, a Kafka consumer, your gateway's webhook
handler — and leave the rest. The parts worth keeping are the batching, the retry, and the fact
that it never drops an event silently.

    python examples/forward_payments.py --url https://your-host --key pic_live_... --demo

`--demo` invents a minute of plausible traffic so you can see the round trip work before wiring
anything real to it.
"""

from __future__ import annotations

import argparse
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator

import httpx

BATCH_SIZE = 500
MAX_ATTEMPTS = 4


def read_payments() -> Iterator[dict]:
    """Yield payments as they settle. Replace this with your source.

    Send a payment once it has a final outcome. Sending it twice is harmless — duplicates are
    dropped by `payment_id` — so if you are unsure whether a batch landed, send it again.
    """
    raise NotImplementedError("point this at your payments")


def demo_payments(minutes: int = 1, per_minute: int = 120) -> Iterator[dict]:
    """Plausible traffic, so the round trip can be tested without touching real data."""
    rng = random.Random(7)
    now = datetime.now(timezone.utc)
    for index in range(minutes * per_minute):
        when = now - timedelta(seconds=(minutes * per_minute - index) * (60 / per_minute))
        ok = rng.random() < 0.93
        yield {
            "payment_id": f"demo_{int(now.timestamp())}_{index}",
            "timestamp": when.isoformat(),
            "amount_paise": rng.choice([49900, 129900, 249900, 899900]),
            "payment_method": rng.choice(["upi", "card", "netbanking", "wallet"]),
            "status": "captured" if ok else "failed",
            "psp": rng.choice(["psp_axis", "psp_hdfc", "psp_yes"]),
            "gateway": "gw_primary",
            "issuer": rng.choice(["hdfc", "icici", "sbi", "axis"]),
            "route_id": rng.choice(["route_upi_primary", "route_upi_backup"]),
            "error_code": None if ok else "PSP_UNAVAILABLE",
        }


def batched(events: Iterable[dict], size: int = BATCH_SIZE) -> Iterator[list[dict]]:
    batch: list[dict] = []
    for event in events:
        batch.append(event)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def send(client: httpx.Client, url: str, key: str, batch: list[dict]) -> dict:
    """POST one batch, retrying transient failures.

    Retries are safe: the receiving end de-duplicates by `payment_id`, so the failure mode of
    "send twice" is nothing at all, while the failure mode of "give up" is a gap in the baseline
    that later decisions rest on.
    """
    last_error: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = client.post(
                f"{url.rstrip('/')}/api/v1/events",
                json={"events": batch},
                headers={"Authorization": f"Bearer {key}"},
                timeout=15.0,
            )
            if response.status_code < 500:
                response.raise_for_status()
                return response.json()
            last_error = RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(2**attempt)
    raise RuntimeError(f"gave up after {MAX_ATTEMPTS} attempts: {last_error}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="base URL of the deployment")
    parser.add_argument("--key", required=True, help="your API key")
    parser.add_argument("--demo", action="store_true", help="send invented traffic instead")
    args = parser.parse_args()

    source = demo_payments() if args.demo else read_payments()
    totals = {"accepted": 0, "duplicates": 0, "rejected": 0}

    with httpx.Client() as client:
        for batch in batched(source):
            result = send(client, args.url, args.key, batch)
            totals["accepted"] += result["accepted"]
            totals["duplicates"] += result["duplicates"]
            totals["rejected"] += len(result["rejected"])
            # Rejections are printed, never counted and forgotten: every one is a payment the
            # system will not see, and a baseline built on a silent gap is worse than none.
            for rejection in result["rejected"]:
                print(f"  rejected {rejection.get('payment_id')}: {rejection['error']}")

    print(
        f"accepted {totals['accepted']}, duplicates {totals['duplicates']}, "
        f"rejected {totals['rejected']}"
    )
    print(f"check what it makes of them: {args.url.rstrip('/')}/api/v1/status")


if __name__ == "__main__":
    main()
