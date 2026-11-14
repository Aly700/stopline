import runpy
import sys

import pytest

from stopline import db as storage

from .records import RUN, moment


def invoke(monkeypatch, db, *args):
    monkeypatch.setattr(storage, "database", lambda: db)
    monkeypatch.setattr(sys, "argv", ["stopline.report", *args])
    with pytest.raises(SystemExit) as exited:
        runpy.run_module("stopline.report", run_name="__main__")
    return exited.value.code


def test_command_prints_requested_run(demo, monkeypatch, capsys):
    assert invoke(monkeypatch, demo, RUN) == 0
    assert "<!doctype html>" in capsys.readouterr().out


def test_command_writes_latest_run(demo, monkeypatch, tmp_path, capsys):
    newer = demo.runs.find_one({"_id": RUN})
    newer.update(_id="newest-run", created_at=moment(20))
    demo.runs.insert_one(newer)
    path = tmp_path / "run.html"
    assert invoke(monkeypatch, demo, "--out", str(path)) == 0
    assert "newest-run" in path.read_text()
    assert capsys.readouterr().out == ""


def test_command_missing_run_and_empty_database_fail_cleanly(db, monkeypatch, capsys):
    assert invoke(monkeypatch, db) == 1
    assert "No runs found" in capsys.readouterr().err
    assert invoke(monkeypatch, db, "missing") == 1
    assert "Run not found" in capsys.readouterr().err
