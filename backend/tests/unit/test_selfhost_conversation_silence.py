"""Self-host silence boundaries; synthetic clocks, no providers or user fixtures."""

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path

import pytest

ENV = 'OMI_SELFHOST_TWO_MINUTE_SILENCE'
NOW = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)


def load_outline(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, value)
    source = Path(__file__).resolve().parents[2] / 'utils/conversations/v3/outline.py'
    spec = importlib.util.spec_from_file_location('_selfhost_outline_probe', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('value', [None, '', 'false', '1', 'invalid'])
def test_upstream_default_is_unchanged(monkeypatch, value):
    outline = load_outline(monkeypatch, value)
    assert outline.SILENCE == timedelta(minutes=5)
    assert 'Five minutes of recorded silence' in outline.SYSTEM_PROMPT


@pytest.mark.parametrize('value', ['true', ' TRUE '])
def test_selfhost_two_minute_threshold_and_prompt(monkeypatch, value):
    outline = load_outline(monkeypatch, value)
    assert outline.SILENCE == timedelta(minutes=2)
    assert 'Two minutes of recorded silence' in outline.SYSTEM_PROMPT
    assert 'Five minutes of recorded silence' not in outline.SYSTEM_PROMPT
    row = {
        'id': 'synthetic',
        'started_at': NOW,
        'recording': True,
        'transcript_segments': [{'text': 'A useful personal fact.', 'start': 0, 'end': 10}],
    }
    assert not outline.stretches([row], NOW + timedelta(seconds=129.999))[0]['finished']
    assert outline.stretches([row], NOW + timedelta(seconds=130))[0]['finished']
    row['transcript_segments'].append({'text': 'A separate subject.', 'start': 130, 'end': 140})
    assert len(outline.stretches([row], NOW + timedelta(hours=1))) == 2
    row['transcript_segments'][1]['start'] = 129.999
    assert len(outline.stretches([row], NOW + timedelta(hours=1))) == 1
