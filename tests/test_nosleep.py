"""Keeping the machine awake for the length of a run.

A full run is hours, a workstation suspends on idle, and a laptop suspends when its lid closes. Any
of those part way through a benchmark pass loses the run and can leave a half-written report that
looks like a result.

The two tests that matter most are about what it does *not* do. It must not inhibit shutdown, since
two validation tests reboot the machine on purpose, and it must not refuse to run when it cannot
take the lock: a machine that will not certify itself for want of a suspend setting is worse than
one that might get suspended.
"""

import subprocess

import pytest

from alma_certify import nosleep

# The real reload, captured before the autouse fixture stubs the module attribute, so the one test
# that exercises _reload_logind itself can reach past the stub.
_REAL_RELOAD = nosleep._reload_logind


class FakeChild:
    """A ``systemd-inhibit`` that either holds the lock or exited when refused."""

    def __init__(self, holds=True, returncode=1, stderr=""):
        self.holds = holds
        self.returncode = returncode
        self.stdin = _Stream()
        self.stderr = _Stream(stderr)
        self.waits = []
        self.killed = False

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.holds and not self.stdin.closed:
            raise subprocess.TimeoutExpired("systemd-inhibit", timeout)
        return self.returncode

    def poll(self):
        return None if (self.holds and not self.stdin.closed) else self.returncode

    def kill(self):
        self.killed = True


class _Stream:
    def __init__(self, text=""):
        self.text = text
        self.closed = False

    def read(self):
        return self.text

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _never_touch_the_real_logind(monkeypatch, tmp_path):
    """No test may write the real /etc/systemd/logind.conf.d or reload the real logind.

    Redirects the drop-in under a tmp path and stubs the reload, so the lid-config side of
    ``inhibited`` is exercised hermetically. Without this a test run as root would rewrite the
    machine's logind config and bounce its logind, which is exactly the kind of surprise a test
    suite must not spring. ``reloads`` is exposed for the tests that assert on it.
    """
    dropin_dir = tmp_path / "logind.conf.d"
    monkeypatch.setattr(nosleep, "LOGIND_DROPIN_DIR", str(dropin_dir))
    monkeypatch.setattr(nosleep, "LOGIND_DROPIN",
                        str(dropin_dir / "10-alma-certify-nosuspend.conf"))
    reloads = []
    monkeypatch.setattr(nosleep, "_reload_logind",
                        lambda log: (reloads.append(True) or True))
    return {"reloads": reloads, "dropin": dropin_dir / "10-alma-certify-nosuspend.conf"}


@pytest.fixture
def lid(_never_touch_the_real_logind):
    """The hermetic logind state: the drop-in path and the recorded reloads."""
    return _never_touch_the_real_logind


@pytest.fixture
def spawned(monkeypatch):
    """Record the argv the inhibitor was started with, and hand back a child we control."""
    calls = {}

    def make(holds=True, stderr=""):
        def popen(argv, **kwargs):
            calls["argv"] = argv
            calls["kwargs"] = kwargs
            calls["child"] = FakeChild(holds=holds, stderr=stderr)
            return calls["child"]

        monkeypatch.setattr(nosleep.procutil, "find_tool",
                            lambda tool, extra_dirs=(): "/usr/bin/" + tool)
        monkeypatch.setattr(nosleep.subprocess, "Popen", popen)
        return calls

    return make


# --- what it asks for ------------------------------------------------------------


def test_shutdown_is_not_inhibited(spawned):
    """The load-bearing omission. ``--what`` defaults to ``idle:sleep:shutdown``, and in
    ``mode=block`` that would stop ``systemctl reboot``, which is exactly what
    ``validate.platform.reboot`` does on purpose."""
    calls = spawned()

    with nosleep.inhibited(log=lambda m: None):
        pass

    what = next(a for a in calls["argv"] if a.startswith("--what="))
    assert "shutdown" not in what
    assert "sleep" in what and "idle" in what and "handle-lid-switch" in what
    assert "shutdown" not in nosleep.WHAT


def test_it_blocks_rather_than_delays(spawned):
    """A delay inhibitor only postpones a suspend by a few seconds. Blocking is the point."""
    calls = spawned()

    with nosleep.inhibited(log=lambda m: None):
        pass

    assert "--mode=block" in calls["argv"]


def test_the_lock_is_held_by_a_pipe_not_a_timer(spawned):
    """``cat`` reading a pipe this process owns. A child told to sleep for a fixed time would
    outlive a suite killed with SIGKILL and keep the machine awake until it expired, where a pipe is
    closed by the kernel when we die."""
    calls = spawned()

    with nosleep.inhibited(log=lambda m: None):
        pass

    assert calls["argv"][-1] == "cat"
    assert calls["kwargs"]["stdin"] is subprocess.PIPE
    assert not any("sleep" == arg or arg.isdigit() for arg in calls["argv"][1:])


def test_it_says_who_and_why(spawned):
    """``systemd-inhibit --list`` is where an operator looks when something holds a lock, and an
    unexplained one is a mystery to whoever finds it."""
    calls = spawned()

    with nosleep.inhibited(log=lambda m: None):
        pass

    assert "--who=alma-certify" in calls["argv"]
    assert any(a.startswith("--why=") and "certification" in a for a in calls["argv"])


# --- holding and releasing -------------------------------------------------------


def test_a_held_lock_is_reported_and_released(spawned):
    calls = spawned(holds=True)
    said = []

    with nosleep.inhibited(log=said.append) as child:
        assert nosleep.held(child) is True

    assert calls["child"].stdin.closed, "the pipe has to close, or the lock outlives the run"
    assert any("inhibited for the duration" in line for line in said)


def test_the_lock_is_released_even_when_the_run_raises(spawned):
    calls = spawned(holds=True)

    with pytest.raises(RuntimeError):
        with nosleep.inhibited(log=lambda m: None):
            raise RuntimeError("a test blew up")

    assert calls["child"].stdin.closed


def test_a_child_that_will_not_go_is_killed(spawned, monkeypatch):
    calls = spawned(holds=True)

    class Stubborn(FakeChild):
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("systemd-inhibit", timeout)

    monkeypatch.setattr(nosleep.subprocess, "Popen",
                        lambda argv, **kw: calls.setdefault("child", Stubborn()))

    with nosleep.inhibited(log=lambda m: None):
        pass

    assert calls["child"].killed


# --- when it cannot ---------------------------------------------------------------


def test_being_refused_is_a_note_and_not_a_failure(spawned):
    """It needs privileges polkit grants to root. The message is the real one, from an unprivileged
    caller on a machine with logind."""
    spawned(holds=False, stderr=(
        "Failed to inhibit: Access denied as the requested operation requires interactive "
        "authentication. However, interactive authentication has not been enabled by the calling "
        "program.\n"
    ))
    said = []

    with nosleep.inhibited(log=said.append) as child:
        assert child is None, "nothing is held, and the caller has to be able to tell"

    assert any("could not stop the machine sleeping" in line for line in said)
    assert any("Access denied" in line for line in said)


def test_a_machine_without_systemd_inhibit_still_runs(monkeypatch):
    """A container or a stripped image. The run says so once and carries on."""
    monkeypatch.setattr(nosleep.procutil, "find_tool", lambda tool, extra_dirs=(): None)
    said = []

    with nosleep.inhibited(log=said.append) as child:
        assert child is None

    assert any("not installed" in line for line in said)


def test_a_spawn_that_fails_outright_still_runs(monkeypatch):
    monkeypatch.setattr(nosleep.procutil, "find_tool", lambda tool, extra_dirs=(): "/usr/bin/x")

    def boom(argv, **kwargs):
        raise OSError("no fork for you")

    monkeypatch.setattr(nosleep.subprocess, "Popen", boom)
    said = []

    with nosleep.inhibited(log=said.append) as child:
        assert child is None

    assert any("no fork for you" in line for line in said)


def test_held_is_false_for_nothing():
    assert nosleep.held(None) is False


# --- the lid switch, which the lock alone does not cover -------------------------
#
# logind ignores inhibitor locks for the lid by default (LidSwitchIgnoreInhibited=yes), so on a
# laptop the handle-lid-switch lock does nothing until a drop-in flips that. This is the half that
# was missing when a lid close suspended a machine mid-run on AlmaLinux 8.


def test_the_lid_dropin_says_respect_the_lock_and_logind_is_reloaded(spawned, lid):
    """While held, the drop-in exists with LidSwitchIgnoreInhibited=no and logind was reloaded."""
    spawned(holds=True)

    with nosleep.inhibited(log=lambda m: None):
        body = lid["dropin"].read_text()
        assert "[Login]" in body
        assert "LidSwitchIgnoreInhibited=no" in body
        assert lid["reloads"], "logind must be reloaded, or the drop-in is never read"

    assert not lid["dropin"].exists(), "the drop-in must be removed when the run ends"


def test_the_lid_dropin_is_removed_even_when_the_run_raises(spawned, lid):
    spawned(holds=True)

    with pytest.raises(RuntimeError):
        with nosleep.inhibited(log=lambda m: None):
            assert lid["dropin"].exists()
            raise RuntimeError("a test blew up")

    assert not lid["dropin"].exists()


def test_the_success_message_names_the_lid_when_it_is_covered(spawned, lid):
    spawned(holds=True)
    said = []

    with nosleep.inhibited(log=said.append):
        pass

    assert any("lid-close are inhibited" in line for line in said)


def test_when_logind_will_not_reload_the_lid_is_reported_uncovered(spawned, lid, monkeypatch):
    """The honest degradation. If logind cannot be reloaded the drop-in would do nothing, so it is
    taken back at once and the operator is told plainly not to close the lid rather than being
    promised a cover that is not there."""
    monkeypatch.setattr(nosleep, "_reload_logind", lambda log: False)
    spawned(holds=True)
    said = []

    with nosleep.inhibited(log=said.append) as child:
        assert nosleep.held(child) is True, "the lock is still taken; only the lid is uncovered"
        assert not lid["dropin"].exists(), "a drop-in logind will not read must not be left behind"

    assert any("lid switch is not covered" in line for line in said)
    assert any("do not close the lid" in line for line in said)


def test_a_dropin_that_cannot_be_written_still_lets_the_run_take_the_lock(spawned, monkeypatch):
    """A read-only /etc, or no permission. The lid is uncovered, but the lock is still taken and the
    run still starts."""
    def boom(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(nosleep.os, "makedirs", boom)
    spawned(holds=True)
    said = []

    with nosleep.inhibited(log=said.append) as child:
        assert nosleep.held(child) is True

    assert any("could not make the lid switch respect" in line for line in said)


def test_reload_logind_reloads_rather_than_restarts(monkeypatch):
    """A reload re-reads the config without dropping sessions; a restart would bounce every login.
    So the command must be ``systemctl reload systemd-logind``."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(nosleep.procutil, "find_tool",
                        lambda tool, extra_dirs=(): "/usr/bin/" + tool)
    monkeypatch.setattr(nosleep.subprocess, "run", fake_run)

    assert _REAL_RELOAD(log=lambda m: None) is True
    assert seen["argv"] == ["/usr/bin/systemctl", "reload", "systemd-logind"]
    assert "restart" not in seen["argv"]


def test_reload_logind_without_systemctl_is_false_not_an_error(monkeypatch):
    monkeypatch.setattr(nosleep.procutil, "find_tool", lambda tool, extra_dirs=(): None)
    assert _REAL_RELOAD(log=lambda m: None) is False


# --- how it sits in a run --------------------------------------------------------


def test_a_run_takes_the_lock_and_records_whether_it_got_it(monkeypatch, tmp_path):
    """Recorded because "was this machine allowed to sleep during the run" is a question about the
    conditions the results were produced under, and a reviewer cannot ask the machine later."""
    import contextlib

    from alma_certify import cli
    from alma_certify.state import RunState

    state = RunState.create(str(tmp_path), "9f3c1a2e-1111-4111-8111-111111111111", {})
    ran = []

    @contextlib.contextmanager
    def fake_inhibited(log=print):
        yield "a held lock"

    monkeypatch.setattr(cli.nosleep, "inhibited", fake_inhibited)
    monkeypatch.setattr(cli.nosleep, "held", lambda child: child is not None)
    monkeypatch.setattr(cli, "_execute_tests", lambda *a, **kw: ran.append(a) or cli.EXIT_OK)

    assert cli._execute(state, cli.Config.load("/nonexistent"), ["collect"]) == cli.EXIT_OK
    assert ran, "the run still has to happen"
    assert state.meta["sleep_inhibited"] is True
    # And saved, so a crash mid-run does not lose it.
    assert RunState.load(str(tmp_path), state.run_id).meta["sleep_inhibited"] is True


def test_the_report_carries_it(tmp_path):
    from alma_certify import report as report_mod
    from alma_certify.state import RunState

    state = RunState.create(str(tmp_path), "9f3c1a2e-1111-4111-8111-111111111111", {
        "run_types": ["validate"], "sleep_inhibited": False,
    })

    assembled = report_mod.assemble(state, {}, [], {})

    assert assembled["run"]["sleep_inhibited"] is False
