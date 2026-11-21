import types

import pytest

from utils import selfhost_access


class FakeInvalid(Exception):
    pass


def _fake_auth(uid='owner'):
    calls = []

    def verify_id_token(token, check_revoked=False):
        calls.append((token, check_revoked))
        return {'uid': uid if token != 'other' else 'intruder'}

    return types.SimpleNamespace(verify_id_token=verify_id_token, InvalidIdTokenError=FakeInvalid), calls


def test_unset_keeps_upstream_behaviour(monkeypatch):
    monkeypatch.delenv(selfhost_access.ENV_VAR, raising=False)
    module, _ = _fake_auth()
    original = module.verify_id_token
    assert selfhost_access.install(module) is False
    assert module.verify_id_token is original
    assert module.verify_id_token('other')['uid'] == 'intruder'


def test_blank_value_is_unset(monkeypatch):
    monkeypatch.setenv(selfhost_access.ENV_VAR, ' , ')
    module, _ = _fake_auth()
    assert selfhost_access.install(module) is False


def test_owner_allowed_and_kwargs_forwarded(monkeypatch):
    monkeypatch.setenv(selfhost_access.ENV_VAR, 'owner')
    module, calls = _fake_auth()
    assert selfhost_access.install(module) is True
    assert module.verify_id_token('tok', check_revoked=True) == {'uid': 'owner'}
    assert calls == [('tok', True)]


def test_other_uid_rejected(monkeypatch):
    monkeypatch.setenv(selfhost_access.ENV_VAR, 'owner, second')
    module, _ = _fake_auth()
    selfhost_access.install(module)
    with pytest.raises(FakeInvalid):
        module.verify_id_token('other')


def test_install_is_idempotent(monkeypatch):
    monkeypatch.setenv(selfhost_access.ENV_VAR, 'owner')
    module, calls = _fake_auth()
    selfhost_access.install(module)
    wrapped = module.verify_id_token
    selfhost_access.install(module)
    assert module.verify_id_token is wrapped
    module.verify_id_token('tok')
    assert len(calls) == 1


def test_admin_key_impersonation_forced_off(monkeypatch):
    monkeypatch.setenv(selfhost_access.ENV_VAR, 'owner')
    monkeypatch.setenv('ADMIN_KEY', 'x' * 32)
    monkeypatch.setenv('ADMIN_KEY_AUTH_ENABLED', 'true')
    module, _ = _fake_auth()
    selfhost_access.install(module)
    import os

    assert os.environ['ADMIN_KEY_AUTH_ENABLED'] == 'false'


def test_endpoints_verify_token_uses_gate(monkeypatch):
    """The real verify_token resolves firebase auth at call time, so the gate applies."""
    from firebase_admin import auth as firebase_auth
    from utils.other import endpoints

    monkeypatch.setenv(selfhost_access.ENV_VAR, 'owner')
    monkeypatch.delenv('ADMIN_KEY', raising=False)
    monkeypatch.setattr(firebase_auth, 'verify_id_token', lambda token, **_: {'uid': token})
    assert selfhost_access.install(firebase_auth) is True
    assert endpoints.verify_token('owner') == 'owner'
    with pytest.raises(firebase_auth.InvalidIdTokenError):
        endpoints.verify_token('intruder')
