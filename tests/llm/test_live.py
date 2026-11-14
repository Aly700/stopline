"""One live check when credentials exist; fake-server tests never need a key."""

import json
import os

import pytest

from stopline.llm import DEMO_INCIDENT, ModelPlanner, PlannerUnavailable, _api_key


def test_live_demo_incident():
    if os.environ.get("STOPLINE_LIVE") != "1":
        pytest.skip("Set STOPLINE_LIVE=1 to run against a live model.")
    try:
        _api_key()
    except PlannerUnavailable:
        pytest.skip("Gemini API key is unavailable")
    result = ModelPlanner().plan(DEMO_INCIDENT)
    assert 0 < result.seconds <= 10
    print(json.dumps({"model": result.model, "seconds": result.seconds,
                      "steps": result.steps}, indent=2))
