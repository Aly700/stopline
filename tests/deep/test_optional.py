import importlib
import sys
import threading

from tests.agents.fakes import Database


def test_missing_key_creates_nothing(monkeypatch):
    from stopline.deep import start_agent_run, model_configured
    from stopline.deep import config
    monkeypatch.setattr(config, 'read_setting', lambda name: '')
    db = Database({}, threading.RLock())
    assert not model_configured()
    result = start_agent_run(db, None, None, {}, cluster='mock')
    assert not result.ok
    assert result.message == 'No model API key is set.'
    assert not db.data


def test_import_does_not_need_optional_packages(monkeypatch):
    for name in ('deepagents', 'langchain', 'langsmith', 'langchain_google_genai', 'langchain_openai', 'langchain_anthropic'):
        monkeypatch.setitem(sys.modules, name, None)
    import stopline.deep
    importlib.reload(stopline.deep)
    assert callable(stopline.deep.start_agent_run)


def test_missing_packages_creates_nothing(monkeypatch):
    from stopline.deep import start_agent_run, config
    monkeypatch.setattr(config, 'read_setting', lambda name: 'test-key' if name == 'GEMINI_API_KEY' else '')
    monkeypatch.setattr(config, 'packages_available', lambda provider: False)
    db = Database({}, threading.RLock())
    result = start_agent_run(db, None, None, {}, cluster='mock')
    assert not result.ok
    assert result.message == 'Install the agent extra to run a language model.'
    assert not db.data


def test_start_returns_within_two_seconds_during_slow_database_setup(monkeypatch):
    import time
    from stopline.deep import config, runtime, start_agent_run
    monkeypatch.setattr(config, 'select_model', lambda: config.ModelConfig('google_genai', 'fake', 'test-key'))
    monkeypatch.setattr(config, 'packages_available', lambda _: True)
    monkeypatch.setattr(config, 'read_setting', lambda _: '')
    def slow_records(*args):
        time.sleep(2.2)
        raise RuntimeError('private connection details')
    monkeypatch.setattr(runtime, 'create_records', slow_records)
    db = Database({}, threading.RLock())
    started = time.monotonic()
    result = start_agent_run(db, None, None, {}, cluster='mock')
    assert time.monotonic() - started < 2
    assert result.ok and result.pending and result.run_id
