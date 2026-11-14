from stopline.deep.config import ModelConfig


def test_trace_operations_contain_no_payloads_or_errors():
    from stopline.deep.tracing import scrub_operations
    secret = 'private-model-credential'
    uri = 'mongodb+srv://private-connection'
    operations = [{
        'id': '123', 'trace_id': '123', 'name': secret, 'run_type': 'tool',
        'inputs': {'name': secret}, 'outputs': {'answer': uri},
        'error': f'{secret} {uri}', 'extra': {'metadata': {'database': uri}},
        'serialized': {'api_key': secret}, 'tags': [secret], 'session_name': uri,
    }]
    scrubbed = scrub_operations(operations, 'rtest')
    assert secret not in repr(scrubbed)
    assert uri not in repr(scrubbed)
    assert scrubbed[0]['session_name'] == 'stopline'
    assert scrubbed[0]['tags'] == ['rtest']


def test_key_reader_selects_only_named_assignments(monkeypatch, tmp_path):
    from stopline.deep import config
    monkeypatch.setattr(config.Path, 'home', lambda: tmp_path)
    for _, names, _, _ in config.PROVIDERS:
        for name in names:
            monkeypatch.delenv(name, raising=False)
    (tmp_path / '.stopline_env').write_text(
        "MONGODB_URI='private-database-value'\nexport GOOGLE_API_KEY = 'google-value'\nANTHROPIC_API_KEY='anthropic-value'\n"
    )
    chosen = config.select_model()
    assert chosen.provider == 'google_genai'
    assert chosen.key == 'google-value'
    assert 'google-value' not in repr(chosen)
    monkeypatch.setenv('GEMINI_API_KEY', 'gemini-value')
    assert config.select_model().key == 'gemini-value'


def test_tracing_is_disabled_without_key_even_if_ambient_enabled(monkeypatch):
    import pytest
    pytest.importorskip('langsmith')
    from langsmith.run_helpers import get_tracing_context
    from stopline.deep.tracing import trace_run
    monkeypatch.setenv('LANGSMITH_TRACING', 'true')
    with trace_run('', 'rtest') as trace:
        assert get_tracing_context()['enabled'] is False
        assert trace is None


def test_tracing_setup_failure_does_not_prevent_repair(monkeypatch):
    import pytest
    ls = pytest.importorskip('langsmith')
    from stopline.deep.tracing import trace_run
    def unavailable(**kwargs):
        raise RuntimeError('private tracing setup response')
    monkeypatch.setattr(ls, 'Client', unavailable)
    with trace_run('test-value', 'rtest') as trace:
        assert trace is None
