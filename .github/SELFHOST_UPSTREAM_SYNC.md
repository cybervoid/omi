# Self-host upstream sync

The fork has one long-lived source branch: `selfhost`. It is the exact source
revision accepted for deployment. Routine upstream work happens on a disposable
`sync/<upstream-sha>` branch and never mutates a running service by itself.

## Cadence

- Batch routine upstream changes once per week.
- Pull an urgent security or reliability fix out of band when delaying it is
  riskier than the update.
- The weekly `selfhost-upstream-drift` workflow reports only. It never rebases,
  pushes, builds, deploys, or promotes.

## Candidate

1. Fetch `BasedHardware/omi` and record the exact upstream SHA.
2. Branch `sync/<upstream-sha>` from `selfhost`.
3. Merge that exact upstream SHA into the candidate. Resolve against current
   upstream behavior and drop a local patch when upstream now owns the same
   contract.
4. Run affected deterministic tests and `make preflight`.
5. Push the candidate. `selfhost-build` produces SHA-tagged backend/pusher
   images and a release-signed dev APK; it does not deploy them.

After the initial migration, use merges rather than rebasing the stable branch.
That keeps `selfhost` as an ancestor of every candidate, so an accepted candidate
can promote by fast-forwarding `selfhost` to the exact tested commit without a
force push or a second merge commit.

## Canary gate

Deploy only the candidate's immutable digests to the isolated canary. Keep the
previous image digests, environment snapshot, and APK feed metadata until the
candidate is accepted or rolled back.

The minimum real journey is:

1. authenticated capture reaches the canary;
2. the recording finalizes into a visible memory;
3. that memory is retrievable through the owner-visible product path.

Also require public/authenticated health, no new error burst, and any focused
contract check selected by the changed paths. Run heavier integration suites
only for capture/sync, storage/schema, authentication, or client/backend
compatibility changes.

## Promotion and rollback

When the gate passes, fast-forward `selfhost` to the exact candidate commit and
promote the already-tested image/APK digests. Do not rebuild between canary and
live. Delete the candidate branch after promotion.

If the gate fails, restore the previous digest/config and leave `selfhost`
unchanged. Record the failure against the candidate; do not weaken the gate to
make the update pass.

## Local self-host contracts

Keep local commits small and capability-scoped. Current self-host behavior uses
upstream's native conversation timing controls:

- `CONVERSATIONS_V3_SILENCE_GAP_SECONDS=120`
- `CONVERSATIONS_V3_LOCK_SECONDS=120`

`OMI_SELFHOST_KEEP_SHORT_CONVERSATIONS=true` remains a local Home-visibility
contract. Remove local contracts when upstream provides an equivalent and the
canary proves it.
