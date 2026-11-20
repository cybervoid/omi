"""Self-host opt-in that re-admits cloud Deepgram (fork-only).

Upstream retired Deepgram everywhere (see test_deepgram_serving_disabled.py, which
must keep passing with the opt-in unset). A self-host that sets
OMI_SELFHOST_DEEPGRAM=true and lists dg-nova-3 serves live and prerecorded STT from
its own DEEPGRAM_API_KEY; PTT, held recovery, self-hosted engines, and BYOK stay
excluded, and a missing key fails as configuration rather than retryable work.
"""

from types import SimpleNamespace

import pytest

from config.prerecorded_stt import PrerecordedSTTConfigurationError, required_env_for_model_config
from config.stt_provider_policy import (
    DEEPGRAM_CLOUD_PROVIDER,
    DEEPGRAM_SELF_HOSTED_PROVIDER,
    SELFHOST_DEEPGRAM_ENV,
    STTServingSurface,
    provider_is_enabled,
    selfhost_deepgram_enabled,
)
from utils.stt import pre_recorded, streaming
from utils.stt.pre_recorded import (
    DeepgramPrerecordedProvider,
    PrerecordedSTTService,
    get_prerecorded_provider,
    get_prerecorded_service,
)


@pytest.fixture
def selfhost(monkeypatch):
    monkeypatch.setenv(SELFHOST_DEEPGRAM_ENV, 'true')
    monkeypatch.setenv('STT_PRERECORDED_MODEL', 'dg-nova-3')
    monkeypatch.setenv('DEEPGRAM_API_KEY', 'test-key-not-real')
    monkeypatch.setattr(pre_recorded, '_selfhost_deepgram_client', None)
    return monkeypatch


# --- policy ---------------------------------------------------------------------------


@pytest.mark.parametrize('raw, expected', [('', False), ('false', False), ('true', True), ('1', True), ('ON', True)])
def test_opt_in_parsing(raw, expected):
    assert selfhost_deepgram_enabled({SELFHOST_DEEPGRAM_ENV: raw}) is expected


def test_default_remains_retired(monkeypatch):
    monkeypatch.delenv(SELFHOST_DEEPGRAM_ENV, raising=False)
    for surface in STTServingSurface:
        assert provider_is_enabled(DEEPGRAM_CLOUD_PROVIDER, surface) is False


def test_opt_in_admits_only_cloud_streaming_and_prerecorded(selfhost):
    assert provider_is_enabled(DEEPGRAM_CLOUD_PROVIDER, STTServingSurface.STREAMING)
    assert provider_is_enabled(DEEPGRAM_CLOUD_PROVIDER, STTServingSurface.PRERECORDED)
    assert not provider_is_enabled(DEEPGRAM_CLOUD_PROVIDER, STTServingSurface.PTT)
    assert not provider_is_enabled(DEEPGRAM_CLOUD_PROVIDER, STTServingSurface.HELD_RECOVERY)
    for surface in STTServingSurface:
        assert not provider_is_enabled(DEEPGRAM_SELF_HOSTED_PROVIDER, surface)


# --- prerecorded selection --------------------------------------------------------------


def test_prerecorded_selects_deepgram_for_supported_language(selfhost):
    assert get_prerecorded_service('en') == (PrerecordedSTTService.DEEPGRAM, 'en', 'nova-3')
    assert get_prerecorded_service('ja') == (PrerecordedSTTService.DEEPGRAM, 'ja', 'nova-3')


def test_prerecorded_unsupported_language_uses_deepgram_multi(selfhost):
    assert get_prerecorded_service('xx') == (PrerecordedSTTService.DEEPGRAM, 'multi', 'nova-3')


def test_prerecorded_unknown_multilingual_uses_deepgram_not_unconfigured_modulate(selfhost):
    assert get_prerecorded_service('multi') == (PrerecordedSTTService.DEEPGRAM, 'multi', 'nova-3')


def test_opt_in_without_a_listed_deepgram_model_keeps_upstream_routing(selfhost):
    selfhost.setenv('STT_PRERECORDED_MODEL', 'modulate-velma-2')
    service, _, _ = get_prerecorded_service('ja')
    assert service == PrerecordedSTTService.MODULATE


def test_listed_deepgram_without_opt_in_keeps_upstream_routing(monkeypatch):
    monkeypatch.delenv(SELFHOST_DEEPGRAM_ENV, raising=False)
    monkeypatch.setenv('STT_PRERECORDED_MODEL', 'dg-nova-3')
    service, _, _ = get_prerecorded_service('ja')
    assert service == PrerecordedSTTService.MODULATE


def test_provider_constructed_for_selection(selfhost):
    provider = get_prerecorded_provider('en')
    assert isinstance(provider, DeepgramPrerecordedProvider)


def test_deployment_contract_requires_the_deepgram_key(selfhost):
    assert 'DEEPGRAM_API_KEY' in required_env_for_model_config('dg-nova-3')
    selfhost.delenv(SELFHOST_DEEPGRAM_ENV)
    assert 'DEEPGRAM_API_KEY' not in required_env_for_model_config('dg-nova-3')


# --- prerecorded client -----------------------------------------------------------------


def test_client_refuses_without_opt_in(monkeypatch):
    monkeypatch.delenv(SELFHOST_DEEPGRAM_ENV, raising=False)
    monkeypatch.setenv('DEEPGRAM_API_KEY', 'test-key-not-real')
    with pytest.raises(PrerecordedSTTConfigurationError) as error:
        pre_recorded._deepgram_client_for_request()
    assert error.value.missing_env == 'DEEPGRAM_RETIRED'


def test_client_requires_account_key_and_ignores_byok(selfhost):
    selfhost.delenv('DEEPGRAM_API_KEY')
    selfhost.setattr(pre_recorded, 'get_byok_key', lambda provider: 'user-key', raising=False)
    with pytest.raises(PrerecordedSTTConfigurationError) as error:
        pre_recorded._deepgram_client_for_request()
    assert error.value.missing_env == 'DEEPGRAM_API_KEY'


def test_client_is_reused(selfhost):
    assert pre_recorded._deepgram_client_for_request() is pre_recorded._deepgram_client_for_request()


def test_missing_key_is_a_configuration_error_not_a_retry(selfhost):
    selfhost.delenv('DEEPGRAM_API_KEY')
    with pytest.raises(PrerecordedSTTConfigurationError):
        pre_recorded.deepgram_prerecorded('https://audio.invalid/a.wav', language='en')


def test_prerecorded_maps_deepgram_words(selfhost):
    calls = []
    payload = {
        'results': {
            'channels': [
                {
                    'detected_language': 'en-US',
                    'alternatives': [
                        {
                            'words': [
                                {'word': 'hello', 'punctuated_word': 'Hello', 'start': 0.1, 'end': 0.4, 'speaker': 0},
                                {'word': 'there', 'punctuated_word': 'there.', 'start': 0.5, 'end': 0.9, 'speaker': 1},
                            ]
                        }
                    ],
                }
            ]
        }
    }

    def transcribe_url(source, options, timeout=None):
        calls.append((source, options))
        return SimpleNamespace(to_dict=lambda: payload)

    rest = SimpleNamespace(transcribe_url=transcribe_url)
    client = SimpleNamespace(listen=SimpleNamespace(rest=SimpleNamespace(v=lambda version: rest)))
    selfhost.setattr(pre_recorded, '_deepgram_client_for_request', lambda: client)

    words, language = DeepgramPrerecordedProvider().transcribe_url(
        'https://audio.invalid/a.wav', language='en', return_language=True
    )

    assert language == 'en'
    assert [w['text'] for w in words] == ['Hello', 'there.']
    assert [w['speaker'] for w in words] == ['SPEAKER_00', 'SPEAKER_01']
    assert calls[0][1]['model'] == 'nova-3' and calls[0][1]['language'] == 'en'


# --- streaming --------------------------------------------------------------------------


def _stream_with_client(monkeypatch, models):
    monkeypatch.setattr(streaming, 'stt_service_models', models)
    monkeypatch.setattr(streaming, 'is_dg_self_hosted', False)
    monkeypatch.setattr(streaming, 'deepgram', object())
    monkeypatch.setattr(streaming, '_managed_deepgram_ready', True)


def test_streaming_selects_deepgram_when_opted_in(selfhost):
    _stream_with_client(selfhost, ['dg-nova-3'])
    service, language, model = streaming.get_stt_service_for_language('en', multi_lang_enabled=False)
    assert service == streaming.STTService.deepgram
    assert language == 'en'
    assert model == 'nova-3'


def test_streaming_ptt_still_excludes_deepgram(selfhost):
    _stream_with_client(selfhost, ['dg-nova-3', 'modulate-velma-2'])
    service, _, _ = streaming.get_stt_service_for_language('en', surface=STTServingSurface.PTT)
    assert service != streaming.STTService.deepgram


def test_startup_requires_key_when_deepgram_is_the_opted_in_chain():
    with pytest.raises(RuntimeError, match='DEEPGRAM_API_KEY'):
        streaming.validate_streaming_stt_env({SELFHOST_DEEPGRAM_ENV: 'true', 'STT_SERVICE_MODELS': 'dg-nova-3'})
    streaming.validate_streaming_stt_env(
        {SELFHOST_DEEPGRAM_ENV: 'true', 'STT_SERVICE_MODELS': 'dg-nova-3', 'DEEPGRAM_API_KEY': 'k'}
    )
