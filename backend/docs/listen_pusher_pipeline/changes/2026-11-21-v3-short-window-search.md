# V3 short-window search convergence

After a normal v3 processing attempt settles successfully, converge its durable
conversation into the existing first-party search projection. Direct-complete
windows below 20 words skip notes processing, so they previously also missed
its ordinary index-write hooks when lifecycle-v2 was not serving the owner.

Use the existing fail-open public conversation search-sync helper after the
settlement transaction returns. A false return means no created-delivery
obligation, not failed settlement: notes-free windows still need indexing.
Source/lease conflicts and failed attempts do not reach this hook. The existing
helper re-reads durable state and retains deletion, raw-record and E2EE policy.

No new flag, route, schema, note/memory generation, or lifecycle-v2 rollout is
introduced. Existing hidden/media classification and the self-host short-Home
and two-minute-silence policies are unchanged. No bulk backfill is performed.

Verification: focused v3 search-projection boundary regressions plus existing
v3 processing/created/transaction and Typesense privacy/deletion contracts.
Roll back by restoring the exact previous image digests; the projection hook
adds no authoritative Firestore fields. Targeted historical index repair is
an explicit operator action, not startup behavior.
