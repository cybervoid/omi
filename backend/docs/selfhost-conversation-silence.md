# Self-host automatic conversation completion

Set `OMI_SELFHOST_TWO_MINUTE_SILENCE=true` on the backend and pusher to use
two minutes of recorded silence instead of upstream's five-minute boundary.
Restart the services after changing it: the shared outline/planner boundary and
model instructions are resolved at process start. Any other value leaves the
upstream five-minute behavior unchanged.

This also separates stretches across two-minute recorded gaps, so grouping and
automatic completion stay consistent. It does not change the 300-word Home
filter, background/media policy, manual-end behavior, transcript-repair waits,
or retry timers. Existing completed conversation boundaries remain protected
by upstream's preserve-finished-window logic; do not bulk-reprocess history.

Cloud Tasks dispatch, transcript repair and model execution add latency after
the silence boundary. This is a two-minute eligibility threshold, not a promise
that the completed summary appears at exactly 120 seconds.

Rollback: remove the flag (or set false) and recreate affected services. Keep
the previous image digest and restricted compose/env copies before deployment.
