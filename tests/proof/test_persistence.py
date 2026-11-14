import json

from stopline.proof import summary
from stopline.proof.__main__ import main
from stopline.proof.persistence import persist_and_reload, write_json
from stopline.proof.runner import finalize, run_suite
from stopline.proof.store import MemoryDatabase


def test_evidence_is_reread_from_scratch_before_counting(tmp_path):
    result = run_suite(work_dir=tmp_path)
    db = MemoryDatabase()
    original = [r.get('counts') for r in result['rows']]
    persist_and_reload(result, db)
    finalize(result)
    assert [r.get('counts') for r in result['rows']] == original
    assert db['effects'].find()
    assert db['proof_target_logs'].find()
    write_json(tmp_path / 'results.json', result)
    loaded = json.loads((tmp_path / 'results.json').read_text())
    assert loaded['totals'] == result['totals']
    assert loaded['sources']['record_storage'].startswith('persisted and reread')


def test_cli_only_publishes_proof_results_and_drops_scratch(tmp_path, monkeypatch, capsys):
    from stopline import db as connection
    scratch, publication = MemoryDatabase(), MemoryDatabase()
    dropped = []
    monkeypatch.setattr(connection, 'test_database', lambda lane: scratch)
    monkeypatch.setattr(connection, 'database', lambda name: publication)
    monkeypatch.setattr(connection, 'drop_test_database', lambda db: dropped.append(db))
    output = tmp_path / 'out.json'
    assert main(['--backend', 'fake', '--out', str(output)]) == 0
    assert dropped == [scratch]
    assert set(publication.collections) == {'proof_results'}
    assert summary(publication) == []
    printed = capsys.readouterr().out
    for text in ('FAKE', 'paused_send', 'SKIPPED', 'FAILED: none', 'depth 2'):
        assert text in printed


def test_cli_drops_scratch_even_when_output_write_fails(tmp_path, monkeypatch, capsys):
    from stopline import db as connection
    scratch = MemoryDatabase()
    dropped = []
    monkeypatch.setattr(connection, 'test_database', lambda lane: scratch)
    monkeypatch.setattr(connection, 'database', lambda name: MemoryDatabase())
    monkeypatch.setattr(connection, 'drop_test_database', lambda db: dropped.append(db))
    assert main(['--backend', 'fake', '--out', str(tmp_path)]) == 2
    assert dropped == [scratch]
    assert 'IsADirectoryError' in capsys.readouterr().err


def test_cli_cleanup_failure_is_a_failure(tmp_path, monkeypatch, capsys):
    from stopline import db as connection
    monkeypatch.setattr(connection, 'test_database', lambda lane: MemoryDatabase())
    monkeypatch.setattr(connection, 'database', lambda name: MemoryDatabase())

    def cannot_drop(db):
        raise RuntimeError('sensitive connection details')

    monkeypatch.setattr(connection, 'drop_test_database', cannot_drop)
    assert main(['--backend', 'fake', '--out', str(tmp_path / 'out.json')]) == 2
    assert 'sensitive connection details' not in capsys.readouterr().err


def test_json_replay_recomputes_counts_instead_of_trusting_old_totals(tmp_path):
    result = run_suite(work_dir=tmp_path)
    original = [r.get('counts') for r in result['rows']]
    write_json(tmp_path / 'results.json', result)
    loaded = json.loads((tmp_path / 'results.json').read_text())
    loaded['totals'] = {'invented': 999}
    for row in loaded['rows']:
        if 'counts' in row:
            row['counts'] = {'invented': 999}
    finalize(loaded)
    assert [r.get('counts') for r in loaded['rows']] == original
    assert loaded['complete']
