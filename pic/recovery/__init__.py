"""The second recovery layer: payments that already failed.

Fixing the infrastructure stops the bleeding. It does not recover the payments that failed while
it was broken, and those are real orders belonging to real customers. This package identifies
which of them are still completable and by what means; executing a recovery is a policy-gated
write like any other and lives in `pic/agents/recovery.py`.

Kept as its own package rather than folded into investigation, because it answers a different
question about a different subject: investigation asks *what is broken*, this asks *who did we
lose, and can we still get them back*.
"""
