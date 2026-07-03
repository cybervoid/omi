"""Self-hosted Android app-update feed.

Serves the CI-published `Omi Dev` APK and its metadata to the mobile app,
gated by the standard Firebase auth dependency. This lets the app check for
and download updates using the ID token it already sends on every request —
no Basic Auth credential is embedded in the client. The Caddy `/app/*` path
(Basic Auth) remains only as a manual browser fallback.

Files are produced by `.github/workflows/build-app.yml`, published to the VM
by `deploy/publish-app-update.sh`, and mounted read-only into this container
(see `deploy/docker-compose*.yml`).
"""

import json
import logging
import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from utils.other import endpoints as auth

logger = logging.getLogger(__name__)

router = APIRouter()

# Directory holding the published `latest.json` + APK, mounted read-only.
# Matches the Caddy static root on the VM (`/srv/app-updates`).
APP_UPDATES_DIR = os.getenv('APP_UPDATES_DIR', '/srv/app-updates')
_METADATA_FILE = 'latest.json'
_APK_FILE = 'omi-dev-release.apk'


@router.get('/v2/app/android/latest', tags=['app_update'])
def get_latest_android_app(uid: str = Depends(auth.get_current_user_uid)):
    """Return update metadata for the self-hosted Omi Dev Android app.

    Reads the CI-published `latest.json` from the mounted app-updates dir and
    rewrites the download target to the authenticated backend route, so the
    client never needs the Caddy Basic Auth credential. The app compares the
    returned `versionCode` against its installed build to decide whether to
    prompt an update.
    """
    metadata_path = os.path.join(APP_UPDATES_DIR, _METADATA_FILE)
    if not os.path.isfile(metadata_path):
        raise HTTPException(status_code=404, detail="No app update published")

    try:
        with open(metadata_path, 'r') as f:
            metadata = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.error("Failed to read app update metadata: %s", e)
        raise HTTPException(status_code=500, detail="Could not read update metadata")

    # Point the client at the authenticated backend download route rather than
    # the Basic-Auth Caddy path, so no credential is embedded in the app.
    metadata['downloadUrl'] = '/v2/app/android/download'
    metadata.pop('apkUrl', None)
    return metadata


@router.get('/v2/app/android/download', tags=['app_update'])
def download_latest_android_app(uid: str = Depends(auth.get_current_user_uid)):
    """Serve the latest self-hosted Omi Dev APK. Firebase-authenticated.

    Declared `def` (not `async def`) so FastAPI runs it in the threadpool;
    `FileResponse` streams the file with a non-blocking sendfile, keeping the
    event loop free per the backend async rules.
    """
    apk_path = os.path.join(APP_UPDATES_DIR, _APK_FILE)
    if not os.path.isfile(apk_path):
        raise HTTPException(status_code=404, detail="No app update published")

    return FileResponse(
        apk_path,
        media_type='application/vnd.android.package-archive',
        filename=_APK_FILE,
    )
