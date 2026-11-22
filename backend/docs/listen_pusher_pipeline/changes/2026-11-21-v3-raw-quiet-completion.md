# Nonbatch v3 raw quiet completion

V3 raw recordings carry `recording=true` and `terminal_no_derived_effects=true`
from intake. The latter must continue suppressing raw-source derived effects;
it cannot be widened into ordinary SERVER_RECOVERY admission. Previously quiet
replay excluded these sources in inline finalization mode, and ordinary quiet
evaluation would refuse their terminal marker even with durable dispatch.

Quiet replay now permits a separate lifecycle-owned, effects-free close for an
exact nonbatch live v3 raw source. One Firestore transaction rechecks the quiet
generation, source identity and terminal/lock/deferred guards, bound session
identity/phase/expired lease, upload producers, batch/producer metadata, privacy,
deletion and account cutover. It atomically completes the source and session,
increments the session lifecycle sequence, retires the quiet check, and retains
the normal model-free terminal relevance metadata. The terminal suppression
marker is preserved. No processing job, summary, memory extraction or webhook
is invoked for the source.

The same transaction persists `v3_pending` and its pending age. A best-effort
postcommit v3 builder enqueue finishes tail-window work through the existing
builder owner; its durable sweep retains that obligation if dispatch fails.
Ordinary nonraw quiet jobs remain untouched in inline mode, including when quiet
finalization is disabled. Cloud Tasks' missing-binding recovery is unchanged.

No flags/defaults, payloads, audio custody seals, broad finalization dispatch,
or generic terminal-no-derived admission change. Existing quiet/replay gates and
task-binding requirements remain in force. Canary rollout should retain its
previous image for rollback and reconcile only the explicitly approved stale
source; no historical reprocessing is required.

Verification: `backend/tests/unit/test_conversation_quiet_finalization.py`
covers raw/nonraw inline replay, fresh lease/speech/producers, status/privacy
fences, batch exclusion, duplicate generation, durable builder wake and failure.
The real Firestore emulator additionally verifies contention/renewal races;
unit transaction fakes intentionally do not claim those semantics.

Commands from the repository root (the emulator script requires a running
loopback Firestore emulator and rejects non-demo projects):

```bash
(cd backend && PATH="$PWD/.venv/bin:$PATH" bash test.sh \
  tests/unit/test_conversation_quiet_finalization.py \
  tests/unit/test_finalization_replay_exhaustion.py \
  tests/unit/test_finalization_replay_pagination.py \
  tests/unit/test_check_conversation_lifecycle_writes.py)
python3 backend/scripts/check_conversation_lifecycle_writes.py
FIRESTORE_EMULATOR_HOST=127.0.0.1:8787 GOOGLE_CLOUD_PROJECT=demo-v3-raw \
  backend/.venv/bin/python backend/tests/integration/v3_raw_quiet_emulator.py
```
