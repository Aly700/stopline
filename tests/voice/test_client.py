import logging
import threading
import time

import pytest

from stopline.voice.client import MODEL, OUTPUT_FORMAT, SpeechClient


@pytest.fixture
def client(speech_server, tmp_path, monkeypatch):
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'test-secret-never-log')
    monkeypatch.delenv('STOPLINE_VOICE', raising=False)
    monkeypatch.setenv('STOPLINE_VOICE_ID', 'test-voice')
    played = []
    client = SpeechClient(cache_dir=tmp_path, base_url=speech_server.url, player=played.append)
    client.played = played
    yield client
    client.close()


def test_request_and_cache(client, speech_server):
    first = client.say('Stop confirmed.').result(3)
    second = client.say('Stop confirmed.').result(3)
    assert first == second and first.stat().st_size > 4000
    assert client.played == [first, first]
    assert len(speech_server.requests) == 1
    path, headers, body = speech_server.requests[0]
    assert path == f'/v1/text-to-speech/test-voice?output_format={OUTPUT_FORMAT}'
    assert headers['xi-api-key'] == 'test-secret-never-log'
    assert headers['Content-Type'] == 'application/json'
    assert body == {'text': 'Stop confirmed.', 'model_id': MODEL}


def test_submission_does_not_wait_for_network_or_player(client, speech_server):
    speech_server.delay = .3
    entered, release = threading.Event(), threading.Event()

    def player(path):
        entered.set()
        release.wait(2)

    client.player = player
    start = time.monotonic()
    future = client.say('Stop confirmed.')
    assert time.monotonic() - start < .1
    assert entered.wait(2)
    assert not future.done()
    release.set()
    assert future.result(2)


@pytest.mark.parametrize('failure', ['api', 'timeout', 'missing_key', 'player', 'not_audio'])
def test_failures_are_safe_and_do_not_leak_keys(client, speech_server, monkeypatch, tmp_path, caplog, failure):
    caplog.set_level(logging.DEBUG)
    if failure == 'api':
        speech_server.status = 401
        speech_server.audio = b'test-secret-never-log'
    elif failure == 'timeout':
        speech_server.delay = .4
        client.timeout = .05
    elif failure == 'missing_key':
        monkeypatch.setenv('ELEVENLABS_API_KEY', '')
    elif failure == 'not_audio':
        speech_server.content_type = 'application/json'
    else:
        def missing_player(path):
            raise FileNotFoundError('test-secret-never-log')
        client.player = missing_player
    assert client.say('Stop confirmed.').result(3) is None
    assert 'test-secret-never-log' not in caplog.text
    records = [r for r in caplog.records if r.name == 'stopline.voice']
    assert len(records) == 1
    assert records[0].exc_info is None
    if failure != 'player':
        assert not client.played
        assert not list(tmp_path.glob('*.mp3'))


def test_off_does_no_work(client, speech_server, monkeypatch):
    monkeypatch.setenv('STOPLINE_VOICE', 'off')
    assert client.say('Stop confirmed.').result(1) is None
    assert not speech_server.requests and not client.played


def test_key_file_only_when_environment_is_unset(client, speech_server, tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.delenv('ELEVENLABS_API_KEY')
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    (tmp_path / '.elevenlabs_key').write_text('file-key\n')
    assert client.say('Stop confirmed.').result(3)
    assert speech_server.requests[0][1]['xi-api-key'] == 'file-key'


def test_cached_speech_works_without_key(client, monkeypatch):
    path = client.say('Stop confirmed.').result(3)
    monkeypatch.setenv('ELEVENLABS_API_KEY', '')
    assert client.say('Stop confirmed.').result(1) == path


def test_cache_survives_client_restart(client, speech_server, tmp_path):
    first = client.say('Stop confirmed.').result(3)
    other = SpeechClient(cache_dir=tmp_path, base_url=speech_server.url, player=lambda path: None)
    try:
        assert other.say('Stop confirmed.').result(3) == first
        assert len(speech_server.requests) == 1
    finally:
        other.close()
