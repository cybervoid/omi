# Self-hosted Omi — replication & upgrade runbook

How to (1) rebuild this whole setup from scratch, (2) pull upstream Omi updates (backend/app
stack + device firmware), and (3) where every setting/secret lives. Keep this file (the whole
`deploy/` bundle) backed up off-machine.

## Baseline currently deployed
- Upstream: **`github.com/BasedHardware/omi`**, branch `main`, commit **`a65987c`** (~**v0.11.525**, 2026-06-25).
- Clone is **partial + sparse**: `git clone --filter=blob:none`, sparse paths `app` + `backend`.
- Local changes on top of upstream: `deploy/patches/0001` + `0002` (see "Local modifications"),
  plus the standalone `deploy/diarizer/` service.
- Fork (active): **`github.com/cybervoid/omi`** (public), default branch `selfhost` = `a65987c` + patches 0001/0002 as commits + `selfhost/diarizer/`; `upstream` remote = `BasedHardware/omi`. CI builds the backend image to GHCR (§7).

## 1. Inventory — what to save to replicate (the "settings")
Secrets live in your **password manager**, never in this bundle.
- **`ENCRYPTION_SECRET`** — CRITICAL. If lost, all encrypted Firestore data is unrecoverable. Must be
  byte-identical on every backend/pusher instance forever.
- API keys (`.env`): `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `HUGGINGFACE_TOKEN`, `ANTHROPIC_API_KEY`,
  `GEMINI_API_KEY`, `PERPLEXITY_API_KEY`, `OPENROUTER_API_KEY`, `PINECONE_API_KEY`, `PINECONE_INDEX_NAME=omi`.
- **Batch/offline STT:** `STT_PRERECORDED_MODEL=dg-nova-3` (required on this self-host). Upstream defaults
  prerecorded STT to Parakeet then Modulate; this VM has neither `HOSTED_PARAKEET_API_URL` nor
  `MODULATE_API_KEY`. The fork admits cloud Deepgram on the PRERECORDED surface so offline
  `/v2/sync-local-files` backfill can reuse `DEEPGRAM_API_KEY`. Live `/v4/listen` already uses Deepgram
  via streaming policy. Without this env, backfill jobs fail with `stt_provider_configuration_error`
  / provider=`parakeet`.
- **`METRICS_SECRET`** — bearer token for `GET /metrics` (Prometheus text). Required for
  `python -m scripts.check_selfhost_health --metrics-secret …` runtime counters; generate with
  `openssl rand -hex 32` and keep in the password manager / VM `.env` only.
- **`MEMORY_ENABLED=on`** — required for post-sync conversation reprocess + canonical memory
  intake. Code fail-closes to `off` when unset; that surfaces as
  `503 Memory writes are globally paused` during merged-conversation summary regeneration
  (transcript segments still land). Set `on` (or `MEMORY_MODE=write`) on this self-host.
- This **`deploy/` bundle**: `docker-compose.yml` (+ `docker-compose.ghcr.yml`, `pull-deploy.sh`, `omi-deploy.cron` for pull-based CD), `Caddyfile`, `app-updates/`, `app-updates-basic-auth.txt`, `publish-app-update.sh`, `reconcile-app-update.sh` (autonomous app-feed pull, §7f), `patches/`, `diarizer/`, the
  READMEs, and this runbook. Tarball: `~/omi-deploy-bundle-2026-06-27.tar.gz`.

### Cloud / infra identifiers
- GCP project `project-cda24f5f-2bb9-457d-b5a`; billing account `016219-DE40C6-DE78D8` (USD 50/mo budget, alerts 50/90/100%).
- VM `omi-backend`, `us-central1-a`, `e2-medium`, Ubuntu 22.04, **static IP 35.223.15.33**. Public endpoint `https://35.223.15.33.sslip.io`.
- Service account `omi-backend@project-cda24f5f-2bb9-457d-b5a.iam.gserviceaccount.com`; project roles
  `roles/datastore.user`, `roles/storage.admin`, `roles/firebaseauth.viewer`, `roles/firebasecloudmessaging.admin`,
  `roles/cloudtasks.enqueuer`; on itself: `roles/iam.serviceAccountTokenCreator` **and** `roles/iam.serviceAccountUser`
  (actAs — required to create Cloud Tasks with an OIDC token naming this same SA; without it enqueue fails with
  `PermissionDenied: iam.serviceAccounts.actAs` while still returning HTTP 202/`enqueue_uncertain`);
  **keyless** (attached via metadata). APIs: `iamcredentials`, `iap`, `billingbudgets`, `monitoring`, `cloudtasks` enabled.
- Cloud Tasks: queue `sync-jobs` (`us-central1`, `maxConcurrentDispatches=4`, `maxAttempts=5`) dispatches both
  sync (`SYNC_TASKS_*`) and audio-merge (`AUDIO_MERGE_TASKS_*`) jobs back to `https://35.223.15.33.sslip.io/v2/sync-jobs/run`
  and `/v2/audio-merge-jobs/run` on the same backend container (no separate `backend-sync` service). The Cloud Tasks
  service agent (`service-341734430652@gcp-sa-cloudtasks.iam.gserviceaccount.com`) holds `roles/iam.serviceAccountTokenCreator`
  on the `omi-backend` SA so it can mint that SA's OIDC token for the callback (self-referential invoker). The enqueuer
  SA must also hold `roles/iam.serviceAccountUser` on the invoker SA (same SA here).
- GCS buckets (9): `omi-{speech-profiles,memories-recordings,private-cloud-sync,postprocessing,temporal-sync,chat-files,app-thumbnails,plugins-logos,backups}-cda24f5f`.
- Firestore composite indexes — **source of truth** is `firestore.indexes.json` at the **fork repo root** (`~/omi-fork/firestore.indexes.json`). The VM sparse-checkout does **not** include this file; never assume “code merged” means “indexes live in GCP.” High-traffic ones that have already bitten self-host if undeployed:
  - **Chat (required for app chat):** `chat_sessions(plugin_id asc, created_at desc)` [COLLECTION]. Missing this → `POST /v2/messages` **500** with `FailedPrecondition` / “query requires an index” and the app shows error / no reply. Also: `messages(plugin_id asc, created_at desc)`, `messages(chat_session_id asc, created_at desc)`.
  - **Conversations / memories / fair-use (baseline):** `memories(scoring desc, created_at desc)`, `conversations(status asc, created_at desc)`, `conversations(discarded asc, status asc, created_at desc)`, `conversations(created_at desc, finished_at desc, started_at desc)`, `conversations(discarded asc, folder_id asc, status asc, created_at desc)`, `fair_use_state(stage asc, updated_at desc)` [COLLECTION_GROUP].
  - **Calendar meetings (live gap closed 2026-08-27):** `meetings(start_time asc, end_time asc)` [COLLECTION_GROUP] — used by conversation processing meeting look-ups; was failing with missing-index until created manually. Prefer adding any new meeting query to `firestore.indexes.json` + registry before relying on it.
  - Field overrides (COLLECTION_GROUP scope, for `collection_group()` queries): `conversations.status`, `fcm_tokens.token`, `fair_use_events.case_ref`, `llm_usage.date`.
  - Full set: always prefer the JSON + `check_selfhost_health` over this bullet list (list is a reminder, not complete).
- Firestore backups: daily managed schedule, 7-day retention (+ manual exports to `gs://omi-backups-cda24f5f/firestore-exports/`).
- Firewall: `omi-allow-web` (`tcp:80,443` ← `0.0.0.0/0`, tag `omi-backend`), `omi-allow-ssh-iap` (`tcp:22` ← `35.235.240.0/20`, tag `omi-backend`). **SSH is IAP-only** — connect with `--tunnel-through-iap`.
- Monitoring: uptime check `omi-backend-docs` (`/docs`); email channel `notificationChannels/15642762445287003762`; alert policy `alertPolicies/4210500201617593320`.

### App build inputs
- Flavor `Omi Dev`, package `com.friend.ios.dev`, `API_BASE_URL=https://35.223.15.33.sslip.io/` (trailing slash).
- Firebase **dev** config files (`app/lib/firebase_options_dev.dart`, `app/android/app/src/dev/google-services.json`); `firebase_options_prod.dart` is a stub copy of dev so the app compiles.
- Release signing (ACTIVE): `~/Documents/omi/app-signing/omi-release.jks` + `app/android/key.properties` (backed up in password manager). Alias `omi-upload`; SHA-1 `FC:AF:4D:D4:26:A1:CC:6E:85:13:F4:A4:AF:EF:98:F5:E7:0F:C2:48`; SHA-256 `89:64:1C:C6:EC:1B:71:7C:29:CD:B1:D4:3F:B4:A0:B5:C8:FD:AA:81:B2:E0:27:77:93:05:69:BB:19:2A:DF:7E`. Both are registered on the Firebase dev Android app. Legacy debug SHA-1 `C8:18:48:85:14:8C:2C:6D:49:2F:C8:FE:FF:83:AC:19:8B:82:18:D1` is still registered but no longer used for release-signed installs. Google sign-in consent screen External + test users.
- `app/.dev.env` (PostHog/Intercom intentionally blank). Toolchain: Flutter 3.35.3, Dart 3.9.2, JDK 21, Android SDK 36, NDK 28.2.13676358, CMake 3.22.1.

### Cloud Run Diarizer
- Deployed as a serverless container to GCP Cloud Run (`omi-diarizer`). Built and pushed to GHCR (`ghcr.io/cybervoid/omi-diarizer`) via `.github/workflows/build-diarizer.yml`. HuggingFace token is set as a Cloud Run environment variable.

## 2. Local modifications (keep as re-appliable diffs)
- `patches/0001-storage-signed-url-iam-signblob.patch` — GCS V4 URL signing via IAM SignBlob (keyless GCE).
- `patches/0002-stale-conversation-finalizer.patch` — `backend/scripts/finalize_stale_conversations.py` (cron at `/etc/cron.d/omi-finalizer`).
- `patches/0003-app-update-endpoint.patch` — backend-mediated, Firebase-auth'd app-update feed (`backend/routers/app_update.py` + `backend/main.py`): `GET /v2/app/android/latest` + `/v2/app/android/download`, serving the APK + `latest.json` from `APP_UPDATES_DIR` (the `app-updates/` mount). See §7e.
- `diarizer/` — standalone; not a patch to upstream. Notable pins: pyannote.audio 3.x, `huggingface_hub<1.0`, `weights_only=False`, CPU-only (`CUDA_VISIBLE_DEVICES=""`).
Each is a candidate to **contribute upstream** (esp. 0001) so the fork stays thin.

## 3. Rebuild from scratch (order)
1. **GCP/Firebase**: recreate project resources from the Inventory (or reuse the existing project). If reusing, skip — only the VM/app are ephemeral.
2. **VM**: create `omi-backend` (e2-medium, Ubuntu 22.04, static IP, attached SA, the firewall rules above, Docker + compose)
3. **Backend**: `git clone --filter=blob:none --sparse https://github.com/BasedHardware/omi.git ~/omi && cd ~/omi && git sparse-checkout set backend app && git checkout a65987c` (or newer — see §4). Apply patches: `git apply ~/omi-deploy/patches/0001-*.patch ~/omi-deploy/patches/0002-*.patch ~/omi-deploy/patches/0003-*.patch`.
4. **Deploy files**: put `docker-compose.yml`, `Caddyfile`, `patches/`, and `.env` (with secrets from the password manager; `GOOGLE_APPLICATION_CREDENTIALS` UNSET) in `~/omi-deploy/`. `cd ~/omi-deploy && sudo docker compose up -d --build`. Re-add the finalizer cron (`/etc/cron.d/omi-finalizer`).
5. **Diarizer (Cloud Run)**: deploy the `omi-diarizer` container to GCP Cloud Run. Ensure `HOSTED_SPEAKER_EMBEDDING_API_URL` in the backend `.env` points to the Cloud Run service URL.
6. **App**: install the toolchain, set `app/.dev.env` `API_BASE_URL`, restore the dev Firebase config + release keystore/key.properties, `flutter build apk --flavor dev --release`, sideload to the Pixel. Preferred ongoing path: trigger `build-app.yml`, then publish with `./publish-app-update.sh <github-run-id>` and install from the VM endpoint.
7. Verify: `curl https://35.223.15.33.sslip.io/docs`; run §4e (deploy Firestore indexes from `firestore.indexes.json` + `check_selfhost_health`); pair the CV1, speak, confirm a conversation lands in Firestore; send **one app chat message** and confirm a normal reply (not a generic error / `POST /v2/messages` 500).

## 4. Getting upstream updates
Watch `BasedHardware/omi` (Watch → Releases on GitHub, and `backend/CHANGELOG`/release tags). Three independent tracks:

### a. Backend / stack updates (fork rebase — active flow)
```bash
# In your fork clone (e.g. ~/omi-fork), rebase the selfhost patch-commits onto newer upstream:
cd ~/omi-fork && git fetch upstream
git rebase upstream/main          # or onto a release tag; resolve conflicts in the 0001/0002 commits
git push --force-with-lease origin selfhost   # build-backend CI rebuilds + pushes the GHCR image
cd ~/omi-deploy && ./pull-deploy.sh           # VM pulls the new image (no local build)
```
- If a patch-commit conflicts during the rebase, upstream changed that file — resolve in place, `git rebase --continue`, and refresh the mirror in `deploy/patches/` (then re-tar the bundle).
- After upgrades, re-verify: `/docs` 200, audio playback (the 0001 path), the finalizer (`docker compose exec backend python -m scripts.finalize_stale_conversations --dry-run`), a speaker embedding round-trip, **and one real app chat send** (`POST /v2/messages` must not 500 — see §4e). Also run §4e index deploy + health check **before** calling the upgrade done.
- Patches-as-diffs fallback (no fork): fresh checkout at the new tag + `git apply --3way ~/omi-deploy/patches/0001-*.patch ~/omi-deploy/patches/0002-*.patch ~/omi-deploy/patches/0003-*.patch`, then `docker compose up -d --build`.

### b. App updates
Pull the same commit, rebuild the APK with **your** Firebase config + `API_BASE_URL` + release keystore (never run `flutterfire configure` — it overwrites prod creds). Preferred flow:
```bash
gh workflow run build-app.yml --repo cybervoid/omi --ref selfhost
gh run list --workflow build-app.yml --repo cybervoid/omi --limit 1
cd ~/Documents/omi/deploy && ./publish-app-update.sh <github-run-id>
```
Then install from the authenticated VM endpoint (`/app/omi-dev-release.apk`) or use `adb install -r` once release-signed app is installed.

### c. Device firmware updates
The CV1 runs **stock Omi firmware**, which is hardware-level and **independent of your self-hosted backend** — it talks BLE to the app, and the app flashes it. To update: in the Omi app, use the device/firmware-update screen (OTA pulls Omi's published firmware), or flash manually via nRF Connect DFU from a `BasedHardware/omi` firmware release. Track the `firmware/` dir + GitHub releases for new versions. (Verify the app's firmware-update source points at Omi's public firmware, not your backend.)

### d. Diarizer
Only revisit if upstream changes that contract or you want to provision GPUs for it in Cloud Run.

### e. Firestore indexes — declare ≠ deploy (do this every upgrade)
Indexes live in two places that **drift** unless you close the loop:
1. **Declared** in the fork: `firestore.indexes.json` (repo root) + serving queries registered in `backend/database/firestore_index_registry.py` (CI: `backend/tests/unit/test_firestore_index_coverage.py`).
2. **Deployed** in GCP project `project-cda24f5f-2bb9-457d-b5a` (composite indexes must be `READY`).

**Symptom class (2026-08-27 incident):** app chat “errors out / no reply” while `/docs` and `/v1/health` stay green. Backend log:
```text
POST /v2/messages HTTP/1.1" 500
google.api_core.exceptions.FailedPrecondition: The query requires an index
collection group: chat_sessions  (plugin_id ASC, created_at DESC)
```
Root cause: index was **declared in git** but **never created in the project**. Uptime on `/docs` cannot catch this.

**Required checklist after any backend image pull that may add queries/indexes:**
```bash
# 1) From full fork clone (Mac), deploy declared indexes (preferred — applies whole JSON)
cd ~/omi-fork
firebase deploy --only firestore:indexes --project project-cda24f5f-2bb9-457d-b5a
# If firebase CLI unavailable, create the specific missing composite with gcloud, e.g. chat:
# gcloud firestore indexes composite create --project=project-cda24f5f-2bb9-457d-b5a \
#   --collection-group=chat_sessions --query-scope=COLLECTION \
#   --field-config=field-path=plugin_id,order=ascending \
#   --field-config=field-path=created_at,order=descending

# 2) Wait until new composites are READY (CREATING can take minutes)
gcloud firestore indexes composite list --project=project-cda24f5f-2bb9-457d-b5a \
  --format='table(name.basename(),collectionGroup,queryScope,state)' | grep -E 'CREATING|chat_sessions|meetings' || true

# 3) Health script — declared vs deployed/READY (needs gcloud auth + full checkout)
cd ~/omi-fork/backend
python -m scripts.check_selfhost_health \
  --base-url https://35.223.15.33.sslip.io \
  --project project-cda24f5f-2bb9-457d-b5a \
  --metrics-secret "$METRICS_SECRET"   # optional but recommended

# 4) Functional smoke (do not skip)
# - App: send one chat message; expect a normal AI reply (not generic error)
# - Or from VM logs while sending: no `POST /v2/messages` 500 / FailedPrecondition
```

**First response if chat 500s again:**
1. VM: `sudo docker logs omi-deploy-backend-1 --since 30m 2>&1 | grep -E 'POST /v2/messages|FailedPrecondition|chat_sessions'`
2. If missing-index URL/link appears → create that composite (or re-run `firebase deploy --only firestore:indexes`), wait `READY`, retry chat.
3. Do **not** rebuild/redeploy the backend image first — image pull does not deploy Firestore indexes.

**Discipline:** treat “indexes READY + chat smoke” as part of upgrade Definition of Done, same as `/docs` 200. New env keys still need `.env` updates; new composites need this section, not only a code merge.

## 5. Maintenance strategy — pick one
- **Patches-as-diffs (current default):** simplest, no GitHub fork. Upgrade = fresh checkout at a new tag + `git apply --3way` the two patches. Risk: manual conflict resolution if upstream touches those files.
- **Fork + `selfhost` branch (ACTIVE):** `github.com/cybervoid/omi`, default branch `selfhost` carries the two patches as commits + `selfhost/diarizer/`; `upstream` remote = `BasedHardware/omi`. Upgrade = `git fetch upstream && git rebase upstream/<tag-or-main>` then push (CI rebuilds the image). The weekly `upstream-sync` workflow opens an issue when upstream moves ahead. See §7.

## 6. Backups / disaster recovery
- **Data**: daily Firestore backups (7-day) — extend retention or add scheduled GCS exports for longer history.
- **Config/code**: this `deploy/` bundle stored off-machine (private git or cloud). It is the source of truth that survives a VM/Mac loss.
- **Secret**: `ENCRYPTION_SECRET` in the password manager — without it, restored Firestore data is unreadable.

## 7. CI/CD (GitHub Actions on the fork)
The fork `cybervoid/omi` automates the backend image build + an upstream-drift notifier. Workflows live on the `selfhost` branch under `.github/workflows/` (in the fork only — not in this `deploy/` bundle).
### a. Image build — `.github/workflows/build-backend.yml`
- Trigger: push to `selfhost` touching `backend/**` (or the workflow), or manual `workflow_dispatch`.
- Builds `backend/Dockerfile` (context = repo root, `--build-arg PYTHON_BASE_IMAGE=python:3.11-slim`, linux/amd64) and pushes **`ghcr.io/cybervoid/omi-backend:latest`** + `:sha-<commit>` to GHCR via the built-in `GITHUB_TOKEN`.
- **Package visibility (DONE):** `omi-backend` is set **public** so the VM pulls without auth. To redo after a rebuild — API (user-owned pkg): `gh api --method PATCH /user/packages/container/omi-backend -f visibility=public` (needs `write:packages`); or UI: profile → Packages → `omi-backend` → Package settings → Change visibility → Public. Alternative: keep it private + `docker login ghcr.io` on the VM with a `read:packages` PAT.
- The same image runs both `backend` and `pusher` (different `command:`), matching `docker-compose.ghcr.yml`.
### b. Pull-based deploy (VM)
The VM **pulls** the image, so Actions never needs inbound access and the IAP SSH lockdown stays intact.
- Switch the VM to the prebuilt image by deploying with `docker-compose.ghcr.yml` (via `pull-deploy.sh`) instead of `docker-compose.yml`. Pin a build with `OMI_IMAGE=ghcr.io/cybervoid/omi-backend:sha-<commit>` in `.env`; omit for `:latest`.
- Manual deploy: `cd ~/omi-deploy && ./pull-deploy.sh`.
- Hands-off CD (ACTIVE): `/etc/cron.d/omi-deploy` runs `pull-deploy.sh` as root every 10 min, redeploying only when the image digest changes (logs to `/var/log/omi-deploy.log`). Disable with `sudo rm /etc/cron.d/omi-deploy`.
### c. Upstream drift + auto-rebase — `.github/workflows/upstream-sync.yml`
- Weekly (Mon 09:00 UTC) + manual (`workflow_dispatch`, input `upstream_ref`, default `main`). If `selfhost` is behind, it **attempts a rebase** of the self-host patch commits onto the upstream ref on a throwaway `upstream-sync/<sha>` branch — it **never touches `selfhost`** — runs a backend `compileall` gate, and writes a report (clean vs conflict, compile result, upstream commit list, and the exact land command) to the **run's job summary**.
- **Landing stays manual + human-reviewed** (per §4a): the workflow prepares + tests the rebase but never auto-ships upstream to prod. For a clean rebase, land locally: `git fetch upstream && git checkout selfhost && git rebase <target> && git push --force-with-lease origin selfhost` (your creds carry the `workflow` scope the CI `GITHUB_TOKEN` lacks). That push triggers build-backend + build-app; the VM image + app-feed reconcilers converge automatically.
- **Notes:** Issues are **disabled** on the fork, so the report lives in the Actions run summary (not an issue). To have CI push a ready-to-review candidate branch, add a repo secret `SYNC_PAT` = fine-grained PAT with **Contents + Workflows: write** (needed because `GITHUB_TOKEN` refuses to push commits touching `.github/workflows/`). Verified 2026-07-04: clean auto-rebase over 3470 upstream commits, `compileall` passed.
### d. Signed APK build + delivery — `.github/workflows/build-app.yml`
- Triggers: push to `selfhost` touching `app/**` (or the workflow), **or** manual `workflow_dispatch`. Builds a release-signed `Omi Dev` APK (`com.friend.ios.dev`) using Actions secrets: base64 release keystore, signing passwords/alias, dev `google-services.json`, `firebase_options_dev.dart`, and `.dev.env`.
- Version is derived dynamically: `versionName` from `app/pubspec.yaml`; `versionCode` = pubspec build number + `GITHUB_RUN_NUMBER`, passed to `flutter build --build-name/--build-number` and written into `latest.json` so the served metadata stays in lockstep with the APK and increments every run (the in-app updater compares `versionCode`).
- First green run: `28618556252` (19m27s). The artifact's cert SHA-1 was verified as `fcaf4dd4...`, matching the release key. Baseline installed app: `versionName=1.0.538`, `versionCode=900`.
- Delivery is VM-hosted rather than public GitHub releases: Caddy serves `/app/*` from `~/omi-deploy/app-updates/`, protected by Basic Auth (browser fallback only — the app uses the backend feed in §7e). Credentials are stored in `app-updates-basic-auth.txt` in this private bundle. Current files: `app-updates/omi-dev-release.apk` and `app-updates/latest.json`.
- To publish a future artifact: `cd ~/Documents/omi/deploy && ./publish-app-update.sh <github-run-id>`. This downloads the artifact and copies it to the VM over IAP; GitHub Actions does **not** receive SSH keys or inbound VM access.
### e. In-app updater — backend feed + Flutter (`patch 0003` + `app/lib/services/app_update_service.dart`)
- Backend feed (auth-gated, **no embedded secrets**): `GET /v2/app/android/latest` returns the `latest.json` metadata with `downloadUrl=/v2/app/android/download`; `GET /v2/app/android/download` streams the APK. Both require a Firebase ID token (`Depends(get_current_user_uid)`) — the same token the app already sends. Backed by the `app-updates/` dir mounted **read-only** into the `backend` service (`APP_UPDATES_DIR=/srv/app-updates` + volume in `docker-compose.ghcr.yml` and `docker-compose.yml`).
- App: Settings → About → **Check for updates** calls the feed, compares `versionCode` to the running build, and (if newer) downloads the APK to cache, verifies its SHA-256 against the metadata, then opens it via `open_filex` so Android's installer takes over (user still approves the sideload). Requires the `REQUEST_INSTALL_PACKAGES` manifest permission.
- **Activation:** the endpoints only exist after the backend image is rebuilt (push `backend/**` → `build-backend`) and the VM redeploys `docker-compose.ghcr.yml` with the new `app-updates` mount (`pull-deploy.sh`).
- Autonomous delivery (VM pull) is now available via `reconcile-app-update.sh` — see §7f. Still deferred: a passive launch-time nudge in the app (the manual About → Check for updates remains the trigger).
### f. Autonomous feed reconciliation (VM pull) — `reconcile-app-update.sh`
Closes the delivery gap so a new `build-app` build reaches the feed without the admin-run `publish-app-update.sh`. Runs **on the VM** (the app-feed counterpart to `pull-deploy.sh`): pulls the newest **successful** `build-app` artifact on `selfhost`, verifies the APK's SHA-256 against its `latest.json`, and **atomically** publishes into `~/omi-deploy/app-updates/` **only when the `versionCode` is newer** (idempotent; refuses downgrades; no-op when already current). VM→GitHub is outbound + **read-only**, so the IAP lockdown is preserved.
- **Cheap in steady state:** each tick first does one lightweight `gh run list` API call and records the last-handled `build-app` run id in `~/omi-deploy/.app-reconcile-state`; when that run id is unchanged (the common no-op case) it **skips the ~180MB artifact download entirely** and exits. State is written on every terminal outcome except `--dry-run`. Verified 2026-07-04: a no-op tick returns in ~1s with no download. To force a full re-check, `rm ~/omi-deploy/.app-reconcile-state`.
- **Prereqs (VM):** `gh`, `jq`, `flock` (`sudo apt-get install gh jq`), plus a **read-only** token at `~/omi-deploy/.gh-token` (`chmod 600`) — a fine-grained PAT scoped to `cybervoid/omi` with **Actions: read** + **Contents: read** + **Metadata: read**. The script also honours `$GH_TOKEN`.
- **Run manually first:** `~/omi-deploy/reconcile-app-update.sh --dry-run`, then without the flag; confirm the served `versionCode` bumped (see Smoke test / the `/v2/app/android/latest` check).
- **Schedule (pick one):**
  - *systemd timer (preferred):* `omi-app-reconcile.service` (`Type=oneshot`, `ExecStart=` the script) + `.timer` (`OnCalendar=*:0/15`, `Persistent=true`). Clean logging via `journalctl`, explicit `PATH`.
  - *cron:* `*/15 * * * * rafag /home/rafag/omi-deploy/reconcile-app-update.sh >> /home/rafag/omi-deploy/app-reconcile.log 2>&1` in `/etc/cron.d/omi-app-reconcile` (mirrors `/etc/cron.d/omi-deploy`). Set `PATH=/usr/local/bin:/usr/bin:/bin`.
- **vs `publish-app-update.sh`:** same end state (APK + `latest.json` in the feed). The reconciler is the hands-off VM-pull path; `publish-app-update.sh` remains the manual admin push for controlled/one-off publishes. Don't run both on a timer.
- **Durability caveat:** it reads GitHub **Actions artifacts** (ephemeral, 14-day retention). Because it always takes the *latest successful* run this is safe — old builds are never needed, and if none exists in-window it no-ops and keeps the current feed. For a durable source, upgrade `build-app.yml` to publish the APK to a **GitHub Release** (or a private GCS bucket) and repoint the reconciler.
