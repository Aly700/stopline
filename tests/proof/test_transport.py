import pytest

from stopline.contract import Action, TargetUnreachable
from stopline.proof.backend import ObservedTarget
from stopline.proof.fakes import FakeTarget
from stopline.proof.store import MemoryDatabase


def test_unreachable_target_marks_dispatch_attempt_as_unsent():
    db = MemoryDatabase()
    target = FakeTarget('cluster')
    observed = ObservedTarget(target, db)
    target.reachable = False
    try:
        with pytest.raises(TargetUnreachable):
            observed.send(Action('a', 'r', 'm', 'worker', 'restart', 'cluster/checkout-api', {}))
        assert db['proof_sends'].find()[0]['dispatched'] is False
    finally:
        target.close()


def test_real_cleanup_restores_a_fault_even_if_capture_aborted():
    from stopline.proof.backend import Runtime
    db = MemoryDatabase()
    target = FakeTarget('router')
    observed = ObservedTarget(target, db)
    runtime = Runtime(db, None, {'router': observed}, None, 'real')
    observed.cut()
    target.reachable = False
    try:
        runtime.close()
        assert target.reachable
        assert not target.lose_next
    finally:
        target.close()


def test_real_factory_rejects_the_demo_database_before_imports(tmp_path):
    from stopline.proof.backend import build_runtime
    db = MemoryDatabase()
    db.name = 'stopline'
    with pytest.raises(ValueError, match='scratch database'):
        build_runtime(backend='real', work_dir=tmp_path, db=db)
    assert not db.collections


def test_real_factory_rejects_nonlocal_target_clocks(tmp_path):
    from stopline.proof.backend import build_runtime
    db = MemoryDatabase()
    db.name = 'stopline_test_proof_fixture'
    with pytest.raises(ValueError, match='loopback'):
        build_runtime(backend='real', work_dir=tmp_path, db=db, router_url='https://example.com:8766')
