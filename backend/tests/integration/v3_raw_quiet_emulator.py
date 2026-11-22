"""Real Firestore contention proof for effects-free v3 raw closure; demo emulator only."""


def main():

    import os, sys, uuid, json, threading
    from pathlib import Path
    from datetime import datetime, timedelta, timezone
    from concurrent.futures import ThreadPoolExecutor

    assert os.environ.get('FIRESTORE_EMULATOR_HOST', '').startswith('127.0.0.1:')
    assert os.environ.get('GOOGLE_CLOUD_PROJECT', '').startswith('demo-')
    os.environ.setdefault('ENCRYPTION_SECRET', 'omi_raw_emulator_test_secret_32_bytes')
    os.environ['LISTEN_QUIET_FINALIZATION_ENABLED'] = 'true'
    os.environ['LISTEN_FINALIZATION_DISPATCH_MODE'] = 'inline'
    sys.path.insert(0, str(Path.cwd() / 'backend'))
    from google.cloud import firestore
    from google.api_core.exceptions import Aborted
    from unittest.mock import patch
    from database import conversation_quiet_checks as checks, recording_sessions as sessions
    from database.quiet_check_retirement import quiet_check_id
    from utils.conversations import lifecycle
    from services import conversation_finalization as replay

    client = firestore.Client(project=os.environ['GOOGLE_CLOUD_PROJECT'])
    aborted_exhaustions = 0

    def bounded_close(jid, generation):
        """Expose only the SDK's precise, aborted-without-commit retry exhaustion.

        An arbitrary ValueError, deadline, transport error or failed assertion is
        still a test failure. A retryable abort is not a successful completion.
        """
        try:
            return lifecycle.complete_raw_quiet_check(jid, generation, firestore_client=client)
        except ValueError as error:
            if str(error) != 'Failed to commit transaction in 5 attempts.' or not isinstance(error.__cause__, Aborted):
                raise
            return 'aborted_exhausted'

    def assert_closed(r, s, q):
        row, session, check = r.get().to_dict(), s.get().to_dict(), q.get().to_dict()
        assert row['status'] == 'completed' and row['terminal_no_derived_effects'] is True
        assert row['v3_pending'] is True and row.get('v3_pending_since') is not None
        assert session['lifecycle_phase'] == 'completed' and session['lifecycle_sequence'] == 2
        assert check['status'] == 'quiet_done' and 'reconcile_after_at' not in check

    def assert_atomic(r, s, q, before):
        """After contention settles, no failed transaction may leave a split close."""
        current = tuple(ref.get().to_dict() for ref in (r, s, q))
        if current[0]['status'] == 'completed':
            assert_closed(r, s, q)
            return True
        assert current == before, current
        return False

    def replay_current(jid, r, s, q):
        candidate = {**q.get().to_dict(), 'job_id': jid}
        outcome = replay._reconcile_quiet_candidate(candidate, firestore_client=client)
        assert outcome in {'retired', 'stale'}, outcome
        assert_closed(r, s, q)
        return outcome

    def seed():
        uid = 'raw-emulator-' + uuid.uuid4().hex
        cid = 'raw'
        sid = 'session'
        now = datetime.now(timezone.utc)
        u = client.collection('users').document(uid)
        r = u.collection('conversations').document(cid)
        s = u.collection('recording_sessions').document(sid)
        u.set({'data_protection_level': 'enhanced', 'private_cloud_sync_enabled': True})
        r.set(
            {
                'id': cid,
                'source': 'omi',
                'recording': True,
                'terminal_no_derived_effects': True,
                'status': 'in_progress',
                'external_data': {'recording_session_id': sid},
                'finished_at': now - timedelta(minutes=10),
                'last_speech_at': now - timedelta(minutes=10),
                'listen_quiet_timeout_seconds': 120,
                'data_protection_level': 'enhanced',
                'private_cloud_sync_enabled': True,
            }
        )
        s.set(
            {
                'uid': uid,
                'recording_session_id': sid,
                'conversation_id': cid,
                'lifecycle_phase': 'in_progress',
                'lifecycle_sequence': 1,
                'lease_expires_at': now - timedelta(minutes=5),
            }
        )
        jid = quiet_check_id(uid, cid)
        q = client.collection('conversation_finalization_jobs').document(jid)
        q.set(
            {
                'kind': 'listen_quiet_check',
                'status': 'waiting_quiet',
                'dispatch_generation': 1,
                'uid': uid,
                'conversation_id': cid,
                'quiet_timeout_seconds': 120,
                'due_at': now - timedelta(minutes=4),
                'reconcile_after_at': now - timedelta(minutes=4),
            }
        )
        return uid, cid, sid, jid, r, s, q

    with patch.object(lifecycle.v3_triggers, 'enqueue', return_value=None):
        uid, cid, sid, jid, r, s, q = seed()
        before = tuple(ref.get().to_dict() for ref in (r, s, q))
        barrier = threading.Barrier(4)

        def close():
            barrier.wait(timeout=20)
            return bounded_close(jid, 1)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: close(), range(4)))
        assert all(result in {'raw_completed', 'stale', 'aborted_exhausted'} for result in results), results
        aborted_exhaustions += results.count('aborted_exhausted')
        closed = assert_atomic(r, s, q, before)
        assert results.count('raw_completed') == int(closed), results
        assert replay_current(jid, r, s, q) == ('stale' if closed else 'retired')
        assert not sessions.renew_recording_session_lease(uid, sid, cid, firestore_client=client)
        print(
            'parallel duplicate close atomic; actual replay converged exactly once; '
            f'postcompletion renewal refused; aborted_exhaustions={aborted_exhaustions}',
            flush=True,
        )
        # Always exercise real service replay of an untouched, overdue obligation,
        # even if this emulator run did not exhaust any concurrent SDK attempts.
        uid, cid, sid, jid, r, s, q = seed()
        assert replay_current(jid, r, s, q) == 'retired'
        assert replay_current(jid, r, s, q) == 'stale'
        print('actual service replay of overdue obligation converges and duplicate replay is immutable', flush=True)
        for i in range(5):
            uid, cid, sid, jid, r, s, q = seed()
            barrier = threading.Barrier(2)

            def renew():
                barrier.wait(timeout=20)
                return sessions.renew_recording_session_lease(uid, sid, cid, firestore_client=client)

            def finish():
                barrier.wait(timeout=20)
                return lifecycle.complete_raw_quiet_check(jid, 1, firestore_client=client)

            with ThreadPoolExecutor(max_workers=2) as pool:
                a = pool.submit(renew)
                b = pool.submit(finish)
                renewed = a.result(timeout=90)
                outcome = b.result(timeout=90)
            status = r.get().to_dict()['status']
            assert (renewed and outcome == 'deferred' and status == 'in_progress') or (
                not renewed and outcome == 'raw_completed' and status == 'completed'
            ), (renewed, outcome, status)
        print('five real renew/close races serializable; never closes renewed live owner', flush=True)
        uid, cid, sid, jid, r, s, q = seed()
        client.collection('account_deletions').document(uid).set({'wipe_status': 'pending'})
        assert lifecycle.complete_raw_quiet_check(jid, 1, firestore_client=client) == 'raw_fenced'
        assert r.get().to_dict()['status'] == 'in_progress' and q.get().to_dict()['status'] == 'waiting_quiet'
        print('real transactional account-deletion fence; no row/check terminal write', flush=True)
    print(
        json.dumps(
            {'emulator_cases': 9, 'passed': 9, 'aborted_exhaustions': aborted_exhaustions, 'external_services': False}
        )
    )


if __name__ == '__main__':
    main()
