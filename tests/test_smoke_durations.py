"""Validation smoke durations, and the three places each one is written.

Validation answers *does it work*, not *how long does it survive load*, so these
are short verified passes rather than endurance runs. CPU and memory are 60
seconds each.

The reason this file exists is that every duration is spelled **three** times:

1. ``config.DEFAULTS``, which is what a checkout actually uses,
2. a fallback argument in the test itself (``getint(..., 60)``), and
3. ``alma-certify.conf`` in the repository root, which is what an RPM install drops on
   disk.

Any two of those disagreeing is invisible at runtime and produces a different
result depending on how the suite was installed - the shipped conf silently
overriding the code default is the worst case, because it only shows up on real
machines and never in a checkout.
"""

import configparser
import os
import re

import pytest

from alma_certify import config as config_mod
from alma_certify.config import Config

SHIPPED_CONF = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "alma-certify.conf",
)

# The durations this project has settled on, in seconds.
EXPECTED = {
    "cpu_smoke_seconds": 60,
    "memory_smoke_seconds": 60,
    "io_smoke_seconds": 60,
    "network_smoke_seconds": 30,
}


@pytest.fixture
def defaults() -> Config:
    """A Config with nothing loaded, so it answers from DEFAULTS alone."""
    parser = configparser.ConfigParser()
    for section, values in config_mod.DEFAULTS.items():
        parser[section] = dict(values)
    return Config(parser)


@pytest.mark.parametrize("key,seconds", sorted(EXPECTED.items()))
def test_the_default_duration(defaults, key, seconds):
    assert defaults.getint("validate", key, 0) == seconds


@pytest.mark.parametrize("key,seconds", sorted(EXPECTED.items()))
def test_the_shipped_conf_agrees_with_the_code(key, seconds):
    """An RPM install must not behave differently from a checkout."""
    parser = configparser.ConfigParser()
    parser.read(SHIPPED_CONF)

    assert parser.getint("validate", key) == seconds, (
        f"alma-certify.conf sets {key} to "
        f"{parser.get('validate', key)}, the code default is {seconds}"
    )


@pytest.mark.parametrize("key,seconds", sorted(EXPECTED.items()))
def test_the_inline_fallback_agrees_with_the_code(key, seconds):
    """The ``getint("validate", key, N)`` fallback in each test.

    Dead code in practice, since the key is always present in DEFAULTS - which is
    exactly why a stale value there would go unnoticed until someone removed the
    key and the suite quietly reverted to an old duration.
    """
    sources = {
        "cpu_smoke_seconds": "alma_certify/validate/cpu.py",
        "memory_smoke_seconds": "alma_certify/validate/memory.py",
        "io_smoke_seconds": "alma_certify/validate/storage.py",
        "network_smoke_seconds": "alma_certify/validate/network.py",
    }
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, sources[key])).read()

    found = set(re.findall(rf'"{key}",\s*(\d+)\)', text))

    assert found, f"no getint fallback for {key} in {sources[key]}"
    assert found == {str(seconds)}, (
        f"{sources[key]} falls back to {sorted(found)}, expected {seconds}"
    )


def test_validation_stays_a_smoke_test(defaults):
    """The standing constraint: validation is functional, not endurance.

    A required test that grew into a multi-minute soak would change what
    certification means, so the ceiling is asserted rather than assumed.
    """
    for key in EXPECTED:
        assert defaults.getint("validate", key, 0) <= 120, key


def test_a_config_file_can_still_override(tmp_path):
    """Shortening the default must not make it fixed - someone chasing an
    intermittent fault needs to be able to run it for longer."""
    path = tmp_path / "alma-certify.conf"
    path.write_text("[validate]\ncpu_smoke_seconds = 300\n", encoding="utf-8")

    config = Config.load(str(path))

    assert config.getint("validate", "cpu_smoke_seconds", 60) == 300
