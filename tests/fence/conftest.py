"""Isolated targets; no external cluster or admission database is needed."""

import pytest

from stopline.targets.mock import MockCluster
from .fence_helpers import RouterProcess


@pytest.fixture
def router_process(tmp_path):
    process = RouterProcess(tmp_path / 'router.sqlite')
    try:
        process.start()
        yield process
    finally:
        process.stop()


@pytest.fixture(params=['router', 'cluster'])
def target(request):
    if request.param == 'router':
        return request.getfixturevalue('router_process').adapter
    return MockCluster()
