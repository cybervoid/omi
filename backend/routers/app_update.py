"""Firebase-authenticated Android update feed for self-host deployments."""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from utils.other import endpoints as auth

logger = logging.getLogger(__name__)

router = APIRouter()

APP_UPDATES_DIR = Path(os.getenv('APP_UPDATES_DIR', '/srv/app-updates'))
_METADATA_FILE = 'latest.json'
_APK_FILE = 'omi-dev-release.apk'
_SHA256_RE = re.compile(r'^[0-9a-fA-F]{64}$')


def _published_path(filename: str) -> Path:
    return APP_UPDATES_DIR / filename


def _invalid_metadata(reason: str) -> HTTPException:
    logger.error('Invalid Android app-update metadata: %s', reason)
    return HTTPException(status_code=500, detail='Could not read update metadata')


def _load_metadata() -> dict[str, Any]:
    metadata_path = _published_path(_METADATA_FILE)
    apk_path = _published_path(_APK_FILE)
    if not metadata_path.is_file() or not apk_path.is_file():
        raise HTTPException(status_code=404, detail='No app update published')

    try:
        raw = json.loads(metadata_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _invalid_metadata(type(exc).__name__) from exc

    if not isinstance(raw, dict):
        raise _invalid_metadata('root is not an object')
    for field in ('app', 'package', 'versionName'):
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            raise _invalid_metadata(f'{field} is missing or invalid')
    for field in ('versionCode', 'sizeBytes'):
        if isinstance(raw.get(field), bool) or not isinstance(raw.get(field), int) or raw[field] <= 0:
            raise _invalid_metadata(f'{field} is missing or invalid')
    if not isinstance(raw.get('sha256'), str) or _SHA256_RE.fullmatch(raw['sha256']) is None:
        raise _invalid_metadata('sha256 is missing or invalid')

    try:
        actual_size = apk_path.stat().st_size
    except OSError as exc:
        raise _invalid_metadata(type(exc).__name__) from exc
    if actual_size != raw['sizeBytes']:
        raise _invalid_metadata('sizeBytes does not match the published APK')

    metadata = dict(raw)
    metadata['downloadUrl'] = '/v2/app/android/download'
    metadata.pop('apkUrl', None)
    return metadata


@router.get('/v2/app/android/latest', tags=['app_update'])
def get_latest_android_app(_uid: str = Depends(auth.get_current_user_uid)) -> dict[str, Any]:
    """Return validated metadata for the latest release-signed self-host APK."""

    return _load_metadata()


@router.get('/v2/app/android/download', tags=['app_update'])
def download_latest_android_app(_uid: str = Depends(auth.get_current_user_uid)) -> FileResponse:
    """Stream the latest release-signed self-host APK."""

    apk_path = _published_path(_APK_FILE)
    if not apk_path.is_file():
        raise HTTPException(status_code=404, detail='No app update published')
    return FileResponse(
        apk_path,
        media_type='application/vnd.android.package-archive',
        filename=_APK_FILE,
    )
