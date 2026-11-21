"""V3 successful processing converges search without forcing notes or memories."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from database import conversations_v3_processing as writers
from utils.conversations import lifecycle, process_conversation as process_pipeline
from utils.conversations.v3 import Conflict, service

AT = datetime(2026, 11, 21, tzinfo=timezone.utc)


def conversation(**updates):
    return {
        'id': 'window',
        'v3_derived': True,
        'kind': 'conversation',
        'status': 'processing',
        'created_at': AT,
        'started_at': AT,
        'finished_at': AT,
        'sync_content_revision': 7,
        'structured': {'title': 'Synthetic plan'},
        'transcript_segments': [{'text': 'A short synthetic plan.', 'start': 0, 'end': 10, 'is_user': False}],
        **updates,
    }


@pytest.fixture
def processing(monkeypatch):
    events = []
    begin = Mock(return_value=True)
    fail = Mock()
    complete = Mock(side_effect=lambda *args: events.append('complete'))
    # False is the successful no-created-delivery result, not a processing failure.
    settle = Mock(side_effect=lambda *args, **kwargs: events.append('settle') or False)
    sync = Mock(side_effect=lambda *args: events.append('sync'))

    def process(uid, language, row, **kwargs):
        events.append('notes')
        kwargs['persistence_observer'](True)
        return SimpleNamespace(status='completed')

    notes = Mock(side_effect=process)
    monkeypatch.setattr(writers, 'begin_processing', begin)
    monkeypatch.setattr(writers, 'fail_processing', fail)
    monkeypatch.setattr(writers, 'settle_processing', settle)
    monkeypatch.setattr(lifecycle, 'complete', complete)
    monkeypatch.setattr(process_pipeline, 'process_conversation', notes)
    monkeypatch.setattr(service.conversations_db, 'sync_conversation_search_index_public', sync)
    return SimpleNamespace(
        events=events, begin=begin, fail=fail, complete=complete, settle=settle, sync=sync, notes=notes
    )


def test_short_success_indexes_after_settlement_without_forcing_notes_or_memories(processing):
    row = conversation()
    assert sum(len(segment['text'].split()) for segment in row['transcript_segments']) < 20

    service._process('synthetic', row, 'token')

    assert processing.events == ['complete', 'settle', 'sync']
    processing.notes.assert_not_called()  # No full notes pipeline or its memory-extraction side effects.
    processing.fail.assert_not_called()
    processing.settle.assert_called_once_with(
        'synthetic',
        'window',
        'token',
        7,
        AT,
        notes=False,
        content_hash=writers.processing_hash(row),
    )
    processing.sync.assert_called_once_with('synthetic', 'window')
    assert 'v3_notes_written' not in row and 'v3_created_pending' not in row


def test_notes_success_still_indexes_after_settlement(processing):
    service._process('synthetic', conversation(manual_end=True), 'token')

    assert processing.events == ['notes', 'settle', 'sync']
    processing.complete.assert_not_called()
    assert processing.settle.call_args.kwargs['notes'] is True
    processing.sync.assert_called_once_with('synthetic', 'window')


def test_settlement_conflict_does_not_index(processing):
    processing.settle.side_effect = Conflict('processing_source_changed')

    with pytest.raises(Conflict, match='processing_source_changed'):
        service._process('synthetic', conversation(), 'token')

    processing.complete.assert_called_once()
    processing.sync.assert_not_called()


@pytest.mark.parametrize('state', ['deleted', 'is_locked'])
def test_skipped_terminal_row_does_not_add_a_search_sync(processing, state):
    service._process('synthetic', conversation(**{state: True}), 'token')

    processing.begin.assert_not_called()
    processing.notes.assert_not_called()
    processing.complete.assert_not_called()
    assert processing.settle.call_args.kwargs == {'skipped': True}
    processing.sync.assert_not_called()


def test_unclaimed_processing_does_not_index(processing):
    processing.begin.return_value = False

    service._process('synthetic', conversation(), 'token')

    processing.settle.assert_not_called()
    processing.sync.assert_not_called()


def test_transcription_failure_does_not_index(processing):
    service._process('synthetic', conversation(status='failed'), 'token')

    processing.fail.assert_called_once()
    processing.begin.assert_not_called()
    processing.settle.assert_not_called()
    processing.sync.assert_not_called()


@pytest.mark.parametrize('status,persisted', [('failed', True), ('completed', False)])
def test_unsuccessful_notes_processing_does_not_index(processing, status, persisted):
    def unsuccessful(uid, language, row, **kwargs):
        if persisted:
            kwargs['persistence_observer'](True)
        return SimpleNamespace(status=status)

    processing.notes.side_effect = unsuccessful
    if status == 'failed':
        service._process('synthetic', conversation(manual_end=True), 'token')
        processing.fail.assert_called_once()
    else:
        with pytest.raises(RuntimeError, match='v3_processing_not_completed'):
            service._process('synthetic', conversation(manual_end=True), 'token')
    processing.settle.assert_not_called()
    processing.sync.assert_not_called()
