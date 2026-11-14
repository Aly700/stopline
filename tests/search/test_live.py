"""One isolated Atlas/Voyage check, limited to four embedding requests."""
import time
from datetime import datetime, timedelta, timezone

import pymongo
import pytest

from stopline import db as storage
from stopline.contract import OPERATOR, REPAIR_AGENT, WORKER_ONE, WORKER_TWO, args_hash
from stopline.gate.schema import AUDIT_VALIDATOR, POLICY_HASH
from stopline.search import AuditSearch
from stopline.search.voyage import SearchError, VoyageClient, load_key


def has_key():
    try:
        load_key()
        return True
    except SearchError:
        return False


def insert_run(db):
    now = datetime(2026, 11, 14, 19, 28, 18, tzinfo=timezone.utc)
    moment = lambda n: now + timedelta(seconds=n)
    run = 'search-example'
    db.create_collection('audit', validator=AUDIT_VALIDATOR, validationLevel='strict', validationAction='error')
    db['runs'].insert_one(dict(
        _id=run, mode='stopline', cluster='mock', status='done', root_mandate_id='root',
        agent_mandate_id='repair', stop_flag=True, stop_at=moment(8), stop_at_local=moment(8).timestamp(),
        holds=[], receipt_id='receipt', planner='scripted', created_at=now,
    ))
    for identity, parent, holder, grantor, depth in [
        ('root', None, OPERATOR, OPERATOR, 0), ('repair', 'root', REPAIR_AGENT, OPERATOR, 1),
        ('one', 'repair', WORKER_ONE, REPAIR_AGENT, 2), ('two', 'repair', WORKER_TWO, REPAIR_AGENT, 2),
    ]:
        stopped = parent is not None
        db['mandates'].insert_one(dict(
            _id=identity, run_id=run, root_id='root', parent_id=parent, holder=holder,
            grantor=grantor, depth=depth, tools=['rollback', 'restart', 'shift_traffic'],
            targets=['cluster/checkout-api', 'cluster/checkout-worker', 'router/region-b'],
            limits=dict(max_actions=12, max_shift_percent=50), expires_at=moment(3600),
            status='revoked' if stopped else 'active', revoked_by=OPERATOR if stopped else None,
            revoked_at_seq=8 if stopped else None, revoked_at=moment(8) if stopped else None, created_at=now,
        ))

    rollback = dict(tool='rollback', target='cluster/checkout-api', args={'to_version': 'v1'})
    shift = dict(tool='shift_traffic', target='router/region-b', args={'percent': 25})
    restart = dict(tool='restart', target='cluster/checkout-worker', args={})
    undo = dict(tool='shift_traffic', target='router/region-b', args={'percent': 0})
    for identity, principal, permission, spec, seq, state, reverses in [
        ('rollback', WORKER_ONE, 'one', rollback, 4, 'applied', None),
        ('shift', WORKER_TWO, 'two', shift, 5, 'applied', None),
        ('undo', OPERATOR, 'root', undo, 12, 'admitted', 'shift'),
    ]:
        evidence = dict(action_id=identity, system=spec['target'].split('/')[0], at_epoch=moment(11).timestamp())
        db['effects'].insert_one(dict(
            _id=identity, run_id=run, mode='stopline', root_id='root', mandate_id=permission,
            chain=['root'] if permission == 'root' else ['root', 'repair', permission],
            principal=principal, **spec, args_hash=args_hash(spec['args']), admitted_seq=seq, state=state,
            evidence=evidence if state == 'applied' else None, reverses=reverses, undo=None,
            policy_version=1, policy_hash=POLICY_HASH, claimed_by='sender' if state == 'applied' else None,
            admitted_at=moment(seq), sent_at=moment(6) if state == 'applied' else None,
            finished_at=moment(11) if state == 'applied' else None,
        ))

    events = []

    def add(kind, seq, principal, permission, action=None, detail=None, reason='ok'):
        events.append(dict(_id=f'audit-{len(events):02}', run_id=run, kind=kind, principal=principal,
                           mandate_id=permission, root_id='root', action_id=action, policy_version=1,
                           policy_hash=POLICY_HASH, reason=reason, seq=seq, at=moment(seq), detail=detail or {}))

    add('hand_down', 1, OPERATOR, 'repair', detail={'holder': REPAIR_AGENT, 'parent_id': 'root'})
    add('hand_down', 2, REPAIR_AGENT, 'one', detail={'holder': WORKER_ONE, 'parent_id': 'repair'})
    add('hand_down', 3, REPAIR_AGENT, 'two', detail={'holder': WORKER_TWO, 'parent_id': 'repair'})
    add('allow', 4, WORKER_ONE, 'one', 'rollback', dict(rollback, chain=['root', 'repair', 'one']))
    add('allow', 5, WORKER_TWO, 'two', 'shift', dict(shift, chain=['root', 'repair', 'two']))
    add('claim', 5, 'sender', 'two', 'shift')
    add('stop', 8, OPERATOR, 'repair', detail=dict(receipt_id='receipt', revoked=['repair', 'one', 'two'],
                                               covered=['rollback', 'shift']))
    stopped = dict(revoked_by=OPERATOR, revoked_at=moment(8), revoked_at_seq=8)
    add('deny', 9, WORKER_TWO, 'two', 'shift-50', dict(shift, args={'percent': 50}, **stopped), 'revoked')
    add('deny', 10, WORKER_ONE, 'one', 'restart', dict(restart, **stopped), 'revoked')
    add('outcome', 11, WORKER_TWO, 'two', 'shift', dict(state='applied', evidence={
        'action_id': 'shift', 'system': 'router', 'at_epoch': moment(11).timestamp()}))
    add('undo', 12, OPERATOR, 'root', 'undo', dict(shift, reverses='shift'))
    db['audit'].insert_many(events)
    db['receipts'].insert_one(dict(
        _id='receipt', run_id=run, root_id='root', mandate_id='repair', stopped_by=OPERATOR,
        stop_seq=8, stopped_at=moment(8), revoked=['repair', 'one', 'two'], covered=['rollback', 'shift'],
        status='closed', closed_at=moment(11),
    ))
    return run, events


@pytest.mark.skipif(not has_key(), reason='Voyage key is not available')
def test_live_retrieval():
    class CountedVoyage(VoyageClient):
        calls = 0
        last_started = 0.0

        def embed(self, texts, input_type, **kwargs):
            assert self.calls < 4, 'The live check must use at most four requests.'
            self.calls += 1
            self.last_started = time.monotonic()
            return super().embed(texts, input_type, **kwargs)

        def space_request(self):
            # Stay within three requests per minute without retrying a 429.
            delay = 21 - (time.monotonic() - self.last_started)
            if delay > 0:
                time.sleep(delay)

    db = None
    service = None
    try:
        db = storage.test_database('search')
        assert db.name.startswith('stopline_test_search_')
        started = time.monotonic()
        with pymongo.timeout(10):
            run, before = insert_run(db)
        print(f'\nseed: {time.monotonic() - started:.3f}s; entries={len(before)}', flush=True)
        voyage = CountedVoyage()
        service = AuditSearch(db, voyage=voyage)
        print(f'model={service.model}; dimensions={service.dimensions}', flush=True)
        started = time.monotonic()
        count = service.index_run(run)
        print(f'embed and store: {time.monotonic() - started:.3f}s; added={count}', flush=True)
        assert count == len(before)
        started = time.monotonic()
        ready = service.ensure_index()
        print(f'index: {time.monotonic() - started:.3f}s; queryable={ready}; {service.index_message}', flush=True)
        questions = ['what was refused after the stop', 'what did worker two do after the stop',
                     'who allowed the rollback of checkout-api']
        for number, question in enumerate(questions):
            voyage.space_request()
            started = time.monotonic()
            result = service.ask(run, question)
            elapsed = time.monotonic() - started
            print(f'question {number + 1}: {elapsed:.3f}s; engine={result["engine"]}; {result["message"]}', flush=True)
            assert result['ok'], result['message']
            assert elapsed < 12
            assert all(row['audit_id'] in {entry['_id'] for entry in before} for row in result['rows'])
            if number == 0:
                assert result['rows'][0]['kind'] == 'deny'
            print(f'top: {result["rows"][0]["kind"]}; {result["rows"][0]["text"]}', flush=True)
        assert voyage.calls == 4
        with pymongo.timeout(5):
            assert list(db['audit'].find({}).sort('_id', 1)) == before
        print(f'Voyage requests: {voyage.calls}', flush=True)
    except SearchError as error:
        pytest.fail(str(error), pytrace=False)
    except (pymongo.errors.PyMongoError, RuntimeError):
        pytest.fail('The live search database is unavailable.', pytrace=False)
    finally:
        if db is not None:
            if service is not None:
                try:
                    with pymongo.timeout(5):
                        db['audit_search'].drop_search_index(service.index_name)
                except Exception:
                    pass
            started = time.monotonic()
            try:
                with pymongo.timeout(10):
                    storage.drop_test_database(db)
                print(f'cleanup: {time.monotonic() - started:.3f}s; test database dropped', flush=True)
            except Exception:
                pytest.fail('The temporary search database could not be removed.', pytrace=False)
