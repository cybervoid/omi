import json

import pytest
from fastapi import HTTPException

from routers import app_update
from utils.other import endpoints as auth


def _publish(tmp_path, *, metadata_overrides=None, apk=b'signed-apk-bytes'):
    metadata = {
        'app': 'Omi Dev',
        'package': 'com.friend.ios.dev',
        'versionName': '1.0.543',
        'versionCode': 1015,
        'sha256': 'a' * 64,
        'sizeBytes': len(apk),
        'apkUrl': '/app/omi-dev-release.apk',
    }
    metadata.update(metadata_overrides or {})
    (tmp_path / 'omi-dev-release.apk').write_bytes(apk)
    (tmp_path / 'latest.json').write_text(json.dumps(metadata), encoding='utf-8')
    return metadata


def test_latest_metadata_is_validated_and_rewrites_download_target(tmp_path, monkeypatch):
    published = _publish(tmp_path)
    monkeypatch.setattr(app_update, 'APP_UPDATES_DIR', tmp_path)

    result = app_update.get_latest_android_app('uid')

    assert result['versionCode'] == published['versionCode']
    assert result['sizeBytes'] == published['sizeBytes']
    assert result['sha256'] == published['sha256']
    assert result['downloadUrl'] == '/v2/app/android/download'
    assert 'apkUrl' not in result


@pytest.mark.parametrize(
    'overrides',
    [
        {'versionCode': 0},
        {'sizeBytes': -1},
        {'sha256': 'not-a-sha256'},
        {'package': ''},
    ],
)
def test_latest_rejects_invalid_metadata(tmp_path, monkeypatch, overrides):
    _publish(tmp_path, metadata_overrides=overrides)
    monkeypatch.setattr(app_update, 'APP_UPDATES_DIR', tmp_path)

    with pytest.raises(HTTPException) as exc:
        app_update.get_latest_android_app('uid')

    assert exc.value.status_code == 500
    assert exc.value.detail == 'Could not read update metadata'


def test_latest_rejects_metadata_when_apk_size_does_not_match(tmp_path, monkeypatch):
    _publish(tmp_path, metadata_overrides={'sizeBytes': 999})
    monkeypatch.setattr(app_update, 'APP_UPDATES_DIR', tmp_path)

    with pytest.raises(HTTPException) as exc:
        app_update.get_latest_android_app('uid')

    assert exc.value.status_code == 500


def test_feed_is_absent_until_both_metadata_and_apk_are_published(tmp_path, monkeypatch):
    monkeypatch.setattr(app_update, 'APP_UPDATES_DIR', tmp_path)

    with pytest.raises(HTTPException) as exc:
        app_update.get_latest_android_app('uid')

    assert exc.value.status_code == 404


def test_download_streams_only_the_fixed_published_apk(tmp_path, monkeypatch):
    _publish(tmp_path)
    monkeypatch.setattr(app_update, 'APP_UPDATES_DIR', tmp_path)

    response = app_update.download_latest_android_app('uid')

    assert response.path == tmp_path / 'omi-dev-release.apk'
    assert response.media_type == 'application/vnd.android.package-archive'
    assert 'omi-dev-release.apk' in response.headers['content-disposition']


def test_both_routes_require_standard_firebase_auth():
    for path in ('/v2/app/android/latest', '/v2/app/android/download'):
        route = next(route for route in app_update.router.routes if route.path == path)
        assert auth.get_current_user_uid in [dependency.call for dependency in route.dependant.dependencies]
