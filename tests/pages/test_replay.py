import subprocess

from pages_helpers import ROOT


def test_replay_behavior():
    result = subprocess.run(["node", "--test", "tests/pages/test_replay.cjs"], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
