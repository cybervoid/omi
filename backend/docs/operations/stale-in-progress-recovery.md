# Abandoned in-progress recovery

The legacy `scripts.finalize_stale_conversations` cron is retired; it must not
be scheduled. The backend's startup and periodic reconciliation can recover
abandoned in-progress recordings when `STALE_IN_PROGRESS_RECOVERY_ENABLED=true`.
It is off by default and independent of `LISTEN_FINALIZATION_DISPATCH_MODE`.
It needs the existing `SYNC_TASKS_PROJECT`, `SYNC_TASKS_LOCATION`,
`LISTEN_FINALIZATION_TASKS_QUEUE`, `LISTEN_FINALIZATION_TASKS_HANDLER_URL`, and
invoker service-account bindings. Without these it does not admit jobs.

`STALE_IN_PROGRESS_RECOVERY_THRESHOLD_SECONDS` defaults to 900 and has a 900
second minimum. A Firestore transaction rechecks the timestamp and content,
creates the exact-conversation outbox job, and blocks enrolled BYOK users until
their live session supplies keys. It never deletes an empty or unknown-age row
or modifies the Redis pointer. Exact-ID REST dispatch and stale-recovery jobs
carry a `dispatch_scope` and a separate due index. Their scoped reconciler runs
whenever worker bindings exist, even when stale admission and global live-listen
dispatch are off; it never claims ordinary live jobs. Normal Cloud Tasks retries,
leases, finalization and dead-letter behavior still apply. An exact-ID request
does not adopt an existing unscoped live job; it returns unavailable instead.
Provision the single-field Firestore index for `scoped_reconcile_after_at`
if single-field indexing has been disabled on the jobs collection.
