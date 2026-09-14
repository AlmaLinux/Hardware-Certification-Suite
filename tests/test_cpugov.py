"""Pinning the CPU governor to performance for a benchmark, and putting it back after.

A benchmark under ``powersave`` measures the governor as much as the chip: frequency ramps up
lazily, so a pass opens below the real clock and the figure understates the hardware. So the suite
forces ``performance`` for the benchmark and restores each CPU's prior governor when it finishes.

The tests that matter most are the ones about *not* overreaching: it restores what it changed, it
leaves a machine with no cpufreq alone rather than failing, and it never touches the governor on a
validate-only run.
"""
from __future__ import annotations

import glob
import os

import pytest

from alma_certify import cpugov


def _fake_cpus(root, governors, available="performance powersave schedutil"):
    """A stand-in /sys tree: one cpuN/cpufreq per entry, with the given current governor."""
    for i, gov in enumerate(governors):
        cpufreq = os.path.join(root, "cpu%d" % i, "cpufreq")
        os.makedirs(cpufreq)
        with open(os.path.join(cpufreq, "scaling_governor"), "w") as handle:
            handle.write(gov + "\n")
        if available is not None:
            with open(os.path.join(cpufreq, "scaling_available_governors"), "w") as handle:
                handle.write(available + "\n")
    return os.path.join(root, "cpu[0-9]*", "cpufreq", "scaling_governor")


@pytest.fixture
def sysfs(tmp_path, monkeypatch):
    """Point cpugov at a fake governor tree under tmp, so no test touches the real /sys.

    Also stubs out the power-profiles daemon (``_ppd_get`` -> None), because ``performance`` now
    prefers it where one answers: without this the governor-path tests would take the PPD branch on
    any host that happens to run power-profiles-daemon (a KDE laptop, for one) and never touch the
    governor tree they set up.
    """
    monkeypatch.setattr(cpugov, "_ppd_get", lambda: None)

    def build(governors, available="performance powersave schedutil"):
        pattern = _fake_cpus(tmp_path, governors, available)
        monkeypatch.setattr(cpugov, "GOVERNOR_GLOB", pattern)
        return pattern
    return build


def _current(pattern):
    return sorted(open(p).read().strip() for p in glob.glob(pattern))


# --- setting and restoring -------------------------------------------------------


def test_it_forces_performance_then_restores_the_prior_governor(sysfs):
    pattern = sysfs(["powersave", "powersave", "ondemand"])

    with cpugov.performance(log=lambda m: None) as changed:
        assert _current(pattern) == ["performance", "performance", "performance"]
        assert len(changed) == 3

    # Each CPU is back to exactly what it was, not blanket-reset to one value.
    assert _current(pattern) == ["ondemand", "powersave", "powersave"]


def test_a_cpu_already_at_performance_is_left_untouched(sysfs):
    pattern = sysfs(["performance", "powersave"])

    with cpugov.performance(log=lambda m: None) as changed:
        assert _current(pattern) == ["performance", "performance"]

    # Only the one that was changed is restored; the already-performance CPU stays performance.
    assert _current(pattern) == ["performance", "powersave"]
    assert len(changed) == 1


def test_the_governor_is_restored_even_when_the_block_raises(sysfs):
    pattern = sysfs(["powersave"])

    with pytest.raises(RuntimeError):
        with cpugov.performance(log=lambda m: None):
            raise RuntimeError("a benchmark blew up")

    assert _current(pattern) == ["powersave"]


# --- when it cannot, it carries on -----------------------------------------------


def test_a_machine_with_no_cpufreq_is_a_note_not_a_failure(sysfs):
    sysfs([])  # no cpuN/cpufreq dirs at all
    said = []

    with cpugov.performance(log=said.append) as changed:
        assert changed == frozenset()

    assert any("no CPU frequency governor" in line for line in said)


def test_a_driver_that_does_not_offer_performance_is_left_alone(sysfs):
    """An intel_pstate mode that exposes only powersave, say. Writing performance would error per
    CPU, so it is not attempted and the run goes on."""
    pattern = sysfs(["powersave"], available="powersave")

    with cpugov.performance(log=lambda m: None) as changed:
        assert changed == frozenset()
        assert _current(pattern) == ["powersave"], "nothing was written"

    assert _current(pattern) == ["powersave"]


def test_a_cpu_whose_write_is_refused_does_not_crash_or_get_recorded(sysfs, monkeypatch):
    """An offlined CPU or a locked-down driver refuses the write. It must not fail the run, and it
    must not be 'restored' to a value that was never actually set."""
    pattern = sysfs(["powersave", "powersave"])
    real_write = cpugov._write
    calls = {"n": 0}

    def flaky(path, value):
        calls["n"] += 1
        if calls["n"] == 1 and value == cpugov.TARGET:
            return False  # first CPU refuses performance
        return real_write(path, value)

    monkeypatch.setattr(cpugov, "_write", flaky)

    with cpugov.performance(log=lambda m: None) as changed:
        assert len(changed) == 1  # only the CPU whose write took

    # The refused CPU keeps its original; nothing was written to it, so nothing to restore.
    assert _current(pattern) == ["powersave", "powersave"]


def test_forced_reports_whether_anything_changed():
    assert cpugov.forced(frozenset()) is False
    assert cpugov.forced(frozenset({"/sys/.../scaling_governor"})) is True


# --- the power-profiles-daemon path ----------------------------------------------
#
# Where a power-profiles daemon answers, performance comes from its profile rather than the raw
# governor: that is what the desktop widget shows and what sets the clock on an active-mode pstate
# driver, so a governor write underneath it would leave the widget on "balanced" and might be
# reverted on the next power event.


def _ppd(monkeypatch, current, *, offers=True, set_ok=True):
    """Stub the power-profiles-daemon helpers; return the list that records set() calls."""
    calls = []
    monkeypatch.setattr(cpugov, "_ppd_get", lambda: current)
    monkeypatch.setattr(cpugov, "_ppd_offers", lambda profile: offers)

    def _set(profile):
        calls.append(profile)
        return set_ok

    monkeypatch.setattr(cpugov, "_ppd_set", _set)
    return calls


def test_a_ppd_host_holds_performance_and_restores_the_prior_profile(monkeypatch):
    calls = _ppd(monkeypatch, "balanced")

    with cpugov.performance(log=lambda m: None) as changed:
        assert cpugov.forced(changed)
        assert calls == ["performance"]           # set to performance inside the block

    assert calls == ["performance", "balanced"]   # restored to the prior profile after


def test_a_ppd_host_already_on_performance_is_left_untouched(monkeypatch):
    calls = _ppd(monkeypatch, "performance")

    with cpugov.performance(log=lambda m: None) as changed:
        assert not cpugov.forced(changed)

    assert calls == [], "nothing to set or restore when already on performance"


def test_a_ppd_host_that_will_not_offer_performance_runs_under_the_current_profile(monkeypatch):
    calls = _ppd(monkeypatch, "balanced", offers=False)
    said = []

    with cpugov.performance(log=said.append) as changed:
        assert not cpugov.forced(changed)

    assert calls == [], "performance was never set, so there is nothing to restore"
    assert any("would not switch to performance" in m for m in said)


def test_the_profile_is_restored_even_when_the_block_raises(monkeypatch):
    calls = _ppd(monkeypatch, "balanced")

    with pytest.raises(RuntimeError):
        with cpugov.performance(log=lambda m: None):
            raise RuntimeError("boom")

    assert calls == ["performance", "balanced"]


def test_a_ppd_host_does_not_write_the_raw_governor(monkeypatch):
    """The whole point of the change: on a PPD host the governor is left to the daemon, never
    written underneath it."""
    _ppd(monkeypatch, "balanced")
    wrote = []
    monkeypatch.setattr(cpugov, "_write", lambda path, value: wrote.append((path, value)) or True)

    with cpugov.performance(log=lambda m: None):
        pass

    assert wrote == [], "a PPD host must not write scaling_governor directly"


def test_ppd_get_is_none_without_the_cli(monkeypatch):
    monkeypatch.setattr(cpugov.procutil, "find_tool", lambda name, extra_dirs=(): None)
    assert cpugov._ppd_get() is None


def test_ppd_get_returns_the_profile_when_the_daemon_answers(monkeypatch):
    monkeypatch.setattr(cpugov.procutil, "find_tool",
                        lambda name, extra_dirs=(): "/usr/bin/powerprofilesctl")

    class R:
        ok = True
        stdout = "performance\n"
        stderr = ""

    monkeypatch.setattr(cpugov.procutil, "run_cmd", lambda argv, timeout=None: R())
    assert cpugov._ppd_get() == "performance"


def test_ppd_get_is_none_when_the_daemon_is_not_running(monkeypatch):
    monkeypatch.setattr(cpugov.procutil, "find_tool",
                        lambda name, extra_dirs=(): "/usr/bin/powerprofilesctl")

    class R:
        ok = False
        stdout = ""
        stderr = "could not connect to power-profiles-daemon"

    monkeypatch.setattr(cpugov.procutil, "run_cmd", lambda argv, timeout=None: R())
    assert cpugov._ppd_get() is None


# --- reaching the daemon over D-Bus where the client is not installed ------------
#
# AlmaLinux 10 has no power-profiles-daemon package: tuned-ppd serves the net.hadess.PowerProfiles
# D-Bus name that KDE reads but ships no powerprofilesctl. Detecting the daemon by the client alone
# missed it there, wrote the raw governor underneath a live daemon, and left the widget on
# "balanced" for the run. So where the client is absent the profile is read and set over D-Bus.


class _Res:
    def __init__(self, ok=True, stdout="", stderr=""):
        self.ok, self.stdout, self.stderr = ok, stdout, stderr


def test_parse_busctl_string_reads_the_quoted_value():
    assert cpugov._parse_busctl_string('s "balanced"\n') == "balanced"
    assert cpugov._parse_busctl_string('s "performance"') == "performance"
    # A daemon that answered but gave nothing usable is not a profile.
    assert cpugov._parse_busctl_string("s \"\"") is None
    assert cpugov._parse_busctl_string("b true") is None


def _dbus_host(monkeypatch, *, active, settable=True):
    """A machine with no powerprofilesctl but a working net.hadess.PowerProfiles over busctl.

    Returns the mutable state so a test can read the profile after the block. ``settable`` False
    models a polkit refusal that still exits zero: the set appears to work and the read-back does
    not move, which is the case the verify exists to catch.
    """
    state = {"profile": active}

    def find_tool(name, extra_dirs=()):
        return None if name == cpugov.POWERPROFILESCTL else "/usr/bin/" + name

    def run_cmd(argv, timeout=None):
        verb = argv[1]
        if verb == "get-property":
            return _Res(ok=True, stdout='s "%s"\n' % state["profile"])
        if verb == "set-property":
            wanted = argv[-1]
            if settable:
                state["profile"] = wanted
            return _Res(ok=True)
        return _Res(ok=False)

    monkeypatch.setattr(cpugov.procutil, "find_tool", find_tool)
    monkeypatch.setattr(cpugov.procutil, "run_cmd", run_cmd)
    return state


def test_ppd_get_reads_the_profile_over_dbus_when_the_client_is_absent(monkeypatch):
    _dbus_host(monkeypatch, active="balanced")
    assert cpugov._ppd_get() == "balanced"


def test_busctl_set_confirms_by_reading_it_back(monkeypatch):
    state = _dbus_host(monkeypatch, active="balanced")
    assert cpugov._ppd_set("performance") is True
    assert state["profile"] == "performance"


def test_a_set_that_exits_zero_but_does_not_take_is_reported_as_failure(monkeypatch):
    """A polkit refusal on a machine with no active session can still exit zero, so a bare success
    is not proof: the read-back is what decides."""
    state = _dbus_host(monkeypatch, active="balanced", settable=False)
    assert cpugov._ppd_set("performance") is False
    assert state["profile"] == "balanced"


def test_el10_holds_performance_over_dbus_and_restores(monkeypatch):
    """The reported case, end to end: KDE on AlmaLinux 10, no powerprofilesctl, tuned-ppd on the
    bus. The profile is switched to performance for the benchmark and put back after, so the widget
    follows the run instead of sitting on balanced."""
    state = _dbus_host(monkeypatch, active="balanced")
    wrote = []
    monkeypatch.setattr(cpugov, "_write", lambda path, value: wrote.append((path, value)) or True)

    with cpugov.performance(log=lambda m: None) as changed:
        assert cpugov.forced(changed)
        assert state["profile"] == "performance"

    assert state["profile"] == "balanced"
    assert wrote == [], "a host with a daemon on the bus must not write the raw governor"


def test_el10_without_an_active_session_says_so_and_does_not_write_the_governor(monkeypatch):
    """When the switch will not take (a run over SSH, where polkit refuses), it says why and runs
    under the current profile. It still does not write the governor: the daemon owns it and would
    revert it, so a write would be both futile and misleading."""
    state = _dbus_host(monkeypatch, active="balanced", settable=False)
    said = []
    wrote = []
    monkeypatch.setattr(cpugov, "_write", lambda path, value: wrote.append((path, value)) or True)

    with cpugov.performance(log=said.append) as changed:
        assert not cpugov.forced(changed)

    assert state["profile"] == "balanced"
    assert wrote == []
    assert any("would not switch to performance" in m for m in said)
    assert any("active desktop session" in m for m in said)


# --- how it sits in a run --------------------------------------------------------


def test_a_benchmark_run_forces_the_governor_and_records_it(monkeypatch, tmp_path):
    import contextlib

    from alma_certify import cli
    from alma_certify.state import RunState

    state = RunState.create(str(tmp_path), "9f3c1a2e-1111-4111-8111-111111111111", {})
    seen = {}

    @contextlib.contextmanager
    def fake_perf(log=print):
        seen["entered"] = True
        yield frozenset({"/sys/x"})   # pretend one CPU was changed

    monkeypatch.setattr(cli.nosleep, "inhibited",
                        lambda log=print: contextlib.nullcontext(None))
    monkeypatch.setattr(cli.nosleep, "held", lambda child: False)
    monkeypatch.setattr(cli.cpugov, "performance", fake_perf)
    monkeypatch.setattr(cli, "_execute_tests", lambda *a, **kw: cli.EXIT_OK)

    assert cli._execute(state, cli.Config.load("/nonexistent"), ["benchmark"]) == cli.EXIT_OK
    assert seen.get("entered"), "a benchmark run must set the governor"
    assert state.meta["cpu_governor_forced"] is True
    assert RunState.load(str(tmp_path), state.run_id).meta["cpu_governor_forced"] is True


def test_a_validate_only_run_leaves_the_governor_alone(monkeypatch, tmp_path):
    import contextlib

    from alma_certify import cli
    from alma_certify.state import RunState

    state = RunState.create(str(tmp_path), "9f3c1a2e-2222-4222-8222-222222222222", {})
    touched = []

    @contextlib.contextmanager
    def fake_perf(log=print):
        touched.append(True)
        yield frozenset()

    monkeypatch.setattr(cli.nosleep, "inhibited",
                        lambda log=print: contextlib.nullcontext(None))
    monkeypatch.setattr(cli.nosleep, "held", lambda child: False)
    monkeypatch.setattr(cli.cpugov, "performance", fake_perf)
    monkeypatch.setattr(cli, "_execute_tests", lambda *a, **kw: cli.EXIT_OK)

    cli._execute(state, cli.Config.load("/nonexistent"), ["validate"])

    assert touched == [], "validation must not touch the CPU governor"
    assert state.meta["cpu_governor_forced"] is False


def test_the_report_carries_it(tmp_path):
    from alma_certify import report as report_mod
    from alma_certify.state import RunState

    state = RunState.create(str(tmp_path), "9f3c1a2e-3333-4333-8333-333333333333", {
        "run_types": ["benchmark"], "cpu_governor_forced": True,
    })

    assembled = report_mod.assemble(state, {}, [], {})

    assert assembled["run"]["cpu_governor_forced"] is True
