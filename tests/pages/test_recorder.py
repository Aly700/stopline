import pytest

from pages_helpers import FORBIDDEN, script

recorder = script("record_pages")


@pytest.mark.parametrize("token", FORBIDDEN)
def test_secret_scan_fails_without_echoing_the_value(token):
    with pytest.raises(recorder.CaptureError) as caught:
        recorder.check_public('some captured data ' + token + 'private-value')
    assert token not in str(caught.value)
    assert "private-value" not in str(caught.value)


def test_worker_resume_must_finish_before_the_recorder_restores_the_router():
    state = {"ready_key":"R", "workers":[
        {"principal":"agent:worker-1", "status":"stopped"},
        {"principal":"agent:worker-2", "status":"running"},
    ]}
    assert not recorder.Recorder.settled(6, state)
    state["workers"][1]["status"] = "stopped"
    assert recorder.Recorder.settled(6, state)


def test_collects_all_action_ids_without_assuming_their_spelling():
    class Console(recorder.Recorder):
        def sample(self):
            return {"state":{"run":None}, "targets":{}}

        def press(self, index):
            return {"key":recorder.KEYS[index], "answer":{"ok":True}, "frames":[
                {"ms":1, "state":{"run":{"_id":"first" if index < 2 else "second"}}, "targets":{}}
            ]}

        def request(self, path, **kwargs):
            if path == "/api/runs":
                return [{"id":"second"}, {"id":"first"}, {"id":"old"}]
            if path.startswith("/api/state?"):
                return {}
            if path.startswith("/api/report/"):
                return {"actions":[{"action_id":"action-" + path.split("/")[-1]}]}
            if path.startswith("/api/chain/"):
                return {"action_id":path.split("/")[-1]}
            if path.startswith("/api/ask?"):
                raise recorder.CaptureError("Unavailable")
            if path.startswith("/report/"):
                return "<html>Recorded receipt</html>"
            raise AssertionError("Unexpected request")

    console = Console("http://localhost")
    try:
        data, _ = console.capture()
        assert set(data["chains"]) == {"action-first", "action-second"}
        assert "ask" not in data
        assert len(data["runs"]) == 2
    finally:
        console.pool.shutdown()
