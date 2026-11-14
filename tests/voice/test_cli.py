import os
import subprocess
import sys


def test_disabled_sound_check_exits_cleanly():
    result = subprocess.run([sys.executable, '-m', 'stopline.voice', '--say', 'Stop confirmed.'],
                            env={**os.environ, 'STOPLINE_VOICE': 'off'},
                            capture_output=True, text=True, timeout=3)
    assert result.returncode == 0
    assert result.stdout.strip() == 'Speech unavailable or disabled.'
    assert not result.stderr
