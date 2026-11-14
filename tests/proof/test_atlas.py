"""Opt-in storage verification; behavior is still from protocol fakes."""

import os
import time

import pytest

from stopline import db as connection
from stopline.proof import summary
from stopline.proof.persistence import persist_and_reload
from stopline.proof.runner import finalize, run_suite


@pytest.mark.skipif(os.environ.get('STOPLINE_PROOF_ATLAS') != '1', reason='set STOPLINE_PROOF_ATLAS=1 for bounded Atlas storage verification')
def test_atlas_evidence_round_trip_and_cleanup(tmp_path):
    started = time.monotonic()
    scratch = connection.test_database('proof')
    try:
        result = run_suite(work_dir=tmp_path)
        original = [(r['mode'], r['name'], r.get('counts')) for r in result['rows']]
        persist_and_reload(result, scratch)
        finalize(result)
        assert [(r['mode'], r['name'], r.get('counts')) for r in result['rows']] == original
        scratch['proof_results'].insert_one(result)
        assert len(summary(scratch)) == 3
        assert result['complete']
    finally:
        connection.drop_test_database(scratch)
    assert scratch.list_collection_names() == []
    assert time.monotonic() - started < 120
