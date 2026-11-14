import pytest
from fastapi.testclient import TestClient

from stopline.view import create_app
from .fakes import FakeDB, ScriptedStage


@pytest.fixture
def app():
    db = FakeDB()
    return create_app(db, ScriptedStage(db, {}), {})


def test_optional_search_is_quiet_without_masking_other_missing_routes(app):
    with TestClient(app) as client:
        response = client.get('/api/ask', params={'q': 'what stopped?'})
        assert response.status_code == 200
        assert response.json() == {'ok': False, 'rows': [], 'message': 'Search is not enabled.'}
        assert client.get('/api/chain/missing').status_code == 404


@pytest.mark.parametrize('engine', ['atlas vector search', 'local ranking'])
def test_later_mounted_search_route_is_not_shadowed(app, engine):
    @app.get('/api/ask')
    def ask(q: str):
        return {'ok': True, 'engine': engine, 'rows': [], 'message': q}

    with TestClient(app) as client:
        response = client.get('/api/ask', params={'q': 'worker two & the stop?'})
        assert response.status_code == 200
        assert response.json() == {'ok': True, 'engine': engine, 'rows': [], 'message': 'worker two & the stop?'}


def test_search_failure_message_is_preserved(app):
    @app.get('/api/ask')
    def ask(q: str):
        return {'ok': False, 'engine': 'local ranking', 'rows': [], 'message': 'The audit index is not ready.'}

    with TestClient(app) as client:
        response = client.get('/api/ask', params={'q': 'what stopped?'})
        assert response.json()['message'] == 'The audit index is not ready.'
        assert response.json()['ok'] is False
