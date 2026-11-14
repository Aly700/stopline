"""Opt in once with STOPLINE_VOICE_LIVE=1; no paid calls in normal tests."""

import os
import subprocess
import time

import pytest

from stopline.voice.client import MODEL, VOICE_ID, VOICE_NAME, SpeechClient, read_key


def test_live_sound_check():
    if os.environ.get('STOPLINE_VOICE_LIVE') != '1':
        pytest.skip('set STOPLINE_VOICE_LIVE=1 for the live sound check')
    if not read_key():
        pytest.skip('no ElevenLabs key present')
    if os.environ.get('STOPLINE_VOICE', '').lower() == 'off':
        pytest.skip('speech disabled')
    played = []

    def low_volume_player(path):
        subprocess.run(['afplay', '-v', '0.15', str(path)], check=True, timeout=6,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        played.append(path)

    client = SpeechClient(player=low_volume_player)
    started = time.monotonic()
    try:
        # Generate without playing, so generation time excludes playback.
        path = client._submit('The receipt is open.', False).result(10)
        elapsed = time.monotonic() - started
        assert path is not None, 'live generation unavailable (see sanitized speech log)'
        assert path.stat().st_size > 4000
        assert client.say('The receipt is open.').result(10) == path
        assert played == [path]
        voice = os.environ.get('STOPLINE_VOICE_ID', VOICE_ID)
        name = VOICE_NAME if voice == VOICE_ID else 'override'
        print(f'voice={name} ({voice}); model={MODEL}; generation={elapsed:.3f}s; bytes={path.stat().st_size}')
    finally:
        client.close()
