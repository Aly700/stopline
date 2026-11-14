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
