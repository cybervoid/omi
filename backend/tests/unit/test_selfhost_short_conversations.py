"""Narrow self-host Home policy contracts, no user data or providers."""

from datetime import timedelta
import importlib.util
from pathlib import Path

import pytest

ENV = 'OMI_SELFHOST_KEEP_SHORT_CONVERSATIONS'


def load_visibility(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, value)
    source = Path(__file__).resolve().parents[2] / 'utils/conversations/v3/visibility.py'
    spec = importlib.util.spec_from_file_location('_selfhost_visibility_probe', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('value', [None, '', 'false', '1', 'invalid'])
@pytest.mark.parametrize('kind', ['conversation', 'voice_work'])
def test_default_keeps_upstream_word_minimum(monkeypatch, value, kind):
    v = load_visibility(monkeypatch, value)
    assert v.project(kind, {}, [], words=29, finished=True, manual_end=False) == (kind, True, 'below_word_minimum')
    assert not v.project(kind, {}, [], words=300, finished=True, manual_end=False)[1]


@pytest.mark.parametrize('value', ['true', ' TRUE '])
@pytest.mark.parametrize('kind', ['conversation', 'voice_work'])
@pytest.mark.parametrize('words', [1, 29, 299, 300])
def test_opt_in_retains_short_speech(monkeypatch, value, kind, words):
    v = load_visibility(monkeypatch, value)
    assert v.project(kind, {}, [], words=words, finished=True, manual_end=False) == (kind, False, 'conversation')


@pytest.mark.parametrize('value', [None, 'true'])
@pytest.mark.parametrize('kind,reason', [('media', 'media'), ('background', 'background_wearer_recognized')])
@pytest.mark.parametrize('manual', [False, True])
def test_filter_guards_still_precede_short_policy(monkeypatch, value, kind, reason, manual):
    v = load_visibility(monkeypatch, value)
    rows = {'r': {'transcript_segments': [{'is_user': True}]}}
    assert v.project(kind, rows, ['r'], words=29, finished=True, manual_end=manual) == (kind, True, reason)


@pytest.mark.parametrize(
    'value,hidden,reason', [(None, True, 'below_word_minimum'), ('true', False, 'wearer_recognition_uncertain')]
)
def test_uncertain_background_retains_upstream_reclassification(monkeypatch, value, hidden, reason):
    v = load_visibility(monkeypatch, value)
    rows = {'r': {'transcript_segments': [{'is_user': False}]}}
    assert v.project('background', rows, ['r'], words=29, finished=True, manual_end=False) == (
        'conversation',
        hidden,
        reason,
    )


@pytest.mark.parametrize('value', [None, 'true'])
def test_manual_end_and_live_visibility_unchanged(monkeypatch, value):
    v = load_visibility(monkeypatch, value)
    assert v.project('voice_work', {}, [], words=29, finished=True, manual_end=True) == (
        'voice_work',
        False,
        'manual_end',
    )
    assert v.project('voice_work', {}, [], words=29, finished=False, manual_end=False) == (
        'voice_work',
        False,
        'conversation',
    )


@pytest.mark.parametrize('kind', ['conversation', 'voice_work', 'media', 'background'])
def test_real_planner_uses_opt_in_for_short_finished_speech(monkeypatch, kind):
    from utils.conversations.v3 import outline, planner, visibility
    from tests.unit.test_conversations_v3_engine import NOW, entry, recording

    monkeypatch.setattr(visibility, 'MIN_HOME_WORDS', 0)
    row = recording(segments=[{'text': 'a useful short note', 'start': 0, 'end': 10, 'is_user': True}])
    group = outline.stretches([row], NOW + timedelta(hours=1))[0]
    built = planner.build('synthetic-owner', planner.windows([group], [[entry(kind=kind)]])[0], {'r': row})
    assert built['home_hidden'] is (kind in ('media', 'background'))
    assert built['manual_end'] is False


def test_empty_audio_still_creates_no_window(monkeypatch):
    from utils.conversations.v3 import outline, planner, visibility
    from tests.unit.test_conversations_v3_engine import NOW, recording

    monkeypatch.setattr(visibility, 'MIN_HOME_WORDS', 0)
    row = recording()
    row['transcript_segments'] = []
    assert planner.windows(outline.stretches([row], NOW + timedelta(hours=1)), []) == []
