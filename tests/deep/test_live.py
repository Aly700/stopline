"""Run the isolated demonstration only when a model credential is available."""

import json
import subprocess
import sys

import pytest

from stopline.deep import model_configured


def test_live_stop_after_two_actions():
    if not model_configured():
        pytest.skip('No model API key is set.')
    pytest.importorskip('deepagents')
    result = subprocess.run([sys.executable, '-m', 'stopline.deep'], capture_output=True, text=True, timeout=160)
    # Only the demonstration's fixed messages and scrubbed summary are displayed.
    print(result.stdout)
    assert result.returncode == 0
    summary = json.loads(result.stdout.splitlines()[-1])
    assert summary['applied'] == 2 and summary['refused'] == 1
    assert summary['model_calls'] > 0
    assert summary['seconds'] <= 120
    from stopline.deep.config import read_setting
    if read_setting('LANGSMITH_API_KEY'):
        assert summary['trace_url_set'] and summary['trace_spans_verified']
