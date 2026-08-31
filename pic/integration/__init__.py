"""Running this against a real payment stream instead of the simulator.

The demo generates its own traffic and applies its own fixes, which is what makes it a closed loop
you can watch end to end. A deployment replaces both ends and nothing in between: events arrive by
API instead of being generated, approved actions leave as signed webhooks instead of mutating a
routing table in memory, and the detector, the eight agents, the policy gateway and the evaluation
harness are the same code either way.

  wire.py     the format a merchant sends, and how it becomes a PaymentEvent
  ingest.py   validation, de-duplication and storage of incoming events
  signing.py  HMAC signing and verification for the outbound action webhook
  control.py  the control plane that dispatches actions to a merchant endpoint
  tenant.py   one merchant's live engine, keyed by API key
"""
