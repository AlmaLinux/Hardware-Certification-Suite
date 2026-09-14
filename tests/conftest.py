"""Fixtures every test file gets.

Two, and both exist because of state that is deliberately outside any one test: the GPU driver
provenance record, which is module-level because the offer that fills it in runs before the run
exists, and the process's own uid, which every command is now gated on.
"""

import pytest

from alma_certify import elevate, gpusetup


@pytest.fixture(autouse=True)
def clean_gpu_record():
    gpusetup._reset_record_for_tests()
    yield
    gpusetup._reset_record_for_tests()


@pytest.fixture(autouse=True)
def assume_root(request, monkeypatch):
    """Run as though root, because the suite's tests are about the commands and not the gate.

    Every command is gated on being root or being able to become root through sudo, and a test
    session is an ordinary user, so without this every ``cli.main`` test would be answering the
    elevation question instead of testing what it came to test. The gate's own tests carry the
    ``gate`` marker and see the real uid.
    """
    if request.node.get_closest_marker("gate"):
        return
    monkeypatch.setattr(elevate, "is_root", lambda: True)
