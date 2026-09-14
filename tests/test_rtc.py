"""The RTC check: hardware behavior, not clock synchronization.

The bug these pin down: comparing the RTC against the system clock failed on
AlmaLinux live media, which runs no NTP and often ships no /etc/adjtime, so
the two legitimately disagree by hours on hardware that is working fine.
"""

import datetime

import pytest

from alma_certify.config import Config
from alma_certify.procutil import CmdResult
from alma_certify.registry import RunContext
from alma_certify.validate.platform import RtcCheck, _parse_hwclock


def _iso(dt):
    return dt.isoformat(sep=" ")


class FakeCtx(RunContext):
    """Feeds canned hwclock output and records the commands issued."""

    def __init__(self, tmp_path, reads, set_ok=True, read_fails_after=None):
        super().__init__(Config.load("/nonexistent"), str(tmp_path))
        self._reads = list(reads)
        self._set_ok = set_ok
        self._read_fails_after = read_fails_after
        self.commands = []

    def cmd(self, argv, timeout=None, artifact=None, check=False):
        self.commands.append(argv)
        if argv[:2] == ["hwclock", "-r"]:
            reads_done = sum(1 for c in self.commands if c[:2] == ["hwclock", "-r"])
            if (self._read_fails_after is not None
                    and reads_done > self._read_fails_after):
                return CmdResult(argv, 1, "", "cannot read")
            value = self._reads.pop(0) if self._reads else self._reads
            return CmdResult(argv, 0, value + "\n", "")
        if argv[:2] == ["hwclock", "--set"]:
            return CmdResult(argv, 0 if self._set_ok else 1, "",
                             "" if self._set_ok else "hwclock: cannot set")
        return CmdResult(argv, 0, "", "")

    def log(self, msg):
        pass


BASE = datetime.datetime(2026, 7, 29, 14, 3, 12, tzinfo=datetime.timezone.utc)


def _sequence(advance=3, write_offset=120):
    """read1, read2 (advanced), readback-after-set, in hwclock's ISO form."""
    return [
        _iso(BASE),
        _iso(BASE + datetime.timedelta(seconds=advance)),
        _iso(BASE + datetime.timedelta(seconds=advance + write_offset)),
    ]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("alma_certify.validate.platform.time.sleep", lambda s: None)


# --- parsing across util-linux versions ---------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "2026-07-29 14:03:12.123456+00:00",     # modern util-linux
        "2026-07-29 14:03:12+00:00",
        "2026-07-29 14:03:12",                  # no offset: assume local
        "Wed 29 Jul 2026 02:03:12 PM UTC  -0.123456 seconds",  # legacy
    ],
)
def test_parse_hwclock_handles_known_formats(text):
    parsed = _parse_hwclock(text)
    assert parsed is not None
    assert parsed.tzinfo is not None      # always comparable
    assert parsed.year == 2026 and parsed.month == 7 and parsed.day == 29


def test_parse_hwclock_rejects_junk():
    assert _parse_hwclock("") is None
    assert _parse_hwclock("hwclock: no such device") is None


# --- the live-media regression ------------------------------------------------


def test_system_clock_disagreement_does_not_fail(tmp_path):
    """The whole point: an unsynced system clock is a software matter."""
    far_off = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    reads = [
        _iso(far_off),
        _iso(far_off + datetime.timedelta(seconds=3)),
        _iso(far_off + datetime.timedelta(seconds=123)),
    ]
    ctx = FakeCtx(tmp_path, reads)
    result = RtcCheck().run(ctx)
    assert result.status == "pass"
    # the offset is still reported, just not gating
    assert abs(result.details["system_offset_s"]) > 60 * 60 * 24


def test_offset_is_recorded_for_the_reviewer(tmp_path):
    ctx = FakeCtx(tmp_path, _sequence())
    result = RtcCheck().run(ctx)
    assert "system_offset_s" in result.details
    assert result.details["rtc_time"].startswith("2026-07-29")


# --- is it running? -----------------------------------------------------------


def test_advancing_clock_passes(tmp_path):
    ctx = FakeCtx(tmp_path, _sequence())
    result = RtcCheck().run(ctx)
    assert result.status == "pass"
    assert result.details["advanced_s"] == 3.0


def test_stopped_clock_fails(tmp_path):
    """A dead oscillator reads the same value twice - real hardware fault."""
    same = _iso(BASE)
    ctx = FakeCtx(tmp_path, [same, same, same])
    result = RtcCheck().run(ctx)
    assert result.status == "fail"
    assert "not running" in result.reason


def test_unreadable_clock_fails(tmp_path):
    ctx = FakeCtx(tmp_path, _sequence(), read_fails_after=0)
    result = RtcCheck().run(ctx)
    assert result.status == "fail"
    assert "could not be read" in result.reason


# --- is it writable? ----------------------------------------------------------


def test_write_is_verified_and_restored(tmp_path):
    ctx = FakeCtx(tmp_path, _sequence())
    result = RtcCheck().run(ctx)

    assert result.status == "pass"
    assert result.details["writable"] is True
    assert result.details["restored"] is True
    sets = [c for c in ctx.commands if c[:2] == ["hwclock", "--set"]]
    assert len(sets) == 2                      # one to test, one to put back
    # the restore targets the original time, not the test value
    assert "14:03" in sets[1][-1]


def test_unwritable_clock_fails(tmp_path):
    ctx = FakeCtx(tmp_path, _sequence(), set_ok=False)
    result = RtcCheck().run(ctx)
    assert result.status == "fail"
    assert "could not be set" in result.reason
    assert result.details["writable"] is False


def test_clock_that_ignores_the_write_fails(tmp_path):
    """Set succeeds but the value never lands - a silently broken RTC."""
    reads = [
        _iso(BASE),
        _iso(BASE + datetime.timedelta(seconds=3)),
        _iso(BASE + datetime.timedelta(seconds=3)),   # unchanged after set
    ]
    ctx = FakeCtx(tmp_path, reads)
    result = RtcCheck().run(ctx)
    assert result.status == "fail"
    assert "did not hold the value" in result.reason


def test_restore_is_attempted_even_when_verification_fails(tmp_path):
    reads = [_iso(BASE), _iso(BASE + datetime.timedelta(seconds=3)),
             _iso(BASE + datetime.timedelta(seconds=3))]
    ctx = FakeCtx(tmp_path, reads)
    RtcCheck().run(ctx)
    sets = [c for c in ctx.commands if c[:2] == ["hwclock", "--set"]]
    assert len(sets) == 2      # the finally block still put it back
