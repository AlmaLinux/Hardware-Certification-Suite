"""Keep the machine awake for the length of a run.

A full run is hours. A workstation install suspends on idle, a laptop suspends when the lid closes,
and either one part way through a benchmark pass loses the run and, worse, can leave a half-written
report that looks like a result. Nothing in the suite asked the machine to stay awake.

**``systemd-inhibit``, held open through a pipe.** The lock lives as long as the child process holds
it, and the child here is ``cat`` reading a pipe this process owns. Closing the pipe releases the
lock; so does dying, because the kernel closes the descriptor and ``cat`` sees end of file. A child
told to ``sleep`` for a fixed time instead would outlive a suite killed with SIGKILL and keep the
machine awake until it expired.

**``shutdown`` is deliberately not inhibited, and that is load-bearing.** The default for
``--what`` is ``idle:sleep:shutdown``, and taking the default would block ``systemctl reboot`` in
``mode=block``, which is exactly what ``validate.platform.reboot`` does on
purpose. So this asks for sleep, idle, and the lid switch, and nothing else.

**The lid switch needs more than the lock.** logind ignores inhibitor locks for the lid by default
(``LidSwitchIgnoreInhibited=yes`` in ``logind.conf``), so on a laptop the ``handle-lid-switch`` lock
above does nothing on its own: closing the lid suspends the machine part way through a run. This was
found the way you would expect, by closing a laptop lid during a run on AlmaLinux 8. So for the
length of the run a drop-in sets ``LidSwitchIgnoreInhibited=no`` and reloads logind, which makes it
consult the lock for the lid too; the drop-in is removed and logind reloaded again at the end. A
stray copy left by a hard kill is harmless: with no lock held the lid suspends as normal.

**Failing to take the lock is not a reason to refuse a run.** It needs privileges polkit grants to
root, which every run has, but a machine without logind, such as a container or a stripped image,
simply cannot offer it. The run says so once and carries on: refusing to start because suspend could
not be disabled would be a worse outcome than possibly being suspended. The lid drop-in is the same:
best-effort, and a machine where it cannot be written or logind cannot be reloaded gets the lock
alone and a note saying the lid is not covered.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from typing import Optional

from . import procutil

# The lid switch ignores inhibitor locks unless logind is told otherwise. This drop-in tells it,
# for the duration of a run. Its own directory so removing it cannot disturb a hand-edited
# logind.conf, and a numeric prefix so it is unambiguous which file is alma-certify's.
LOGIND_DROPIN_DIR = "/etc/systemd/logind.conf.d"
LOGIND_DROPIN = LOGIND_DROPIN_DIR + "/10-alma-certify-nosuspend.conf"
_LOGIND_DROPIN_BODY = (
    "# Written by alma-certify while a run is in progress, and removed when it ends.\n"
    "#\n"
    "# The lid switch ignores inhibitor locks by default (LidSwitchIgnoreInhibited=yes), so\n"
    "# the systemd-inhibit lock alma-certify holds does not stop a laptop suspending when the lid\n"
    "# closes. This makes logind consult that lock for the lid too. A stray copy is harmless:\n"
    "# with no lock held the lid suspends as normal.\n"
    "[Login]\n"
    "LidSwitchIgnoreInhibited=no\n"
)

# Sleep, idle suspend, and the lid switch. Not ``shutdown``: see the module docstring.
WHAT = ("sleep", "idle", "handle-lid-switch")
WHY = "alma-certify is running a hardware certification, which must not be interrupted"

# How long to wait to find out whether the lock was taken. ``systemd-inhibit`` exits at once when it
# is refused, so a child still running after this is a child holding the lock. One second, once per
# run, against a run measured in hours.
_SETTLE_SECONDS = 1.0


def _reload_logind(log) -> bool:
    """Ask logind to re-read its configuration. True when it did.

    A reload, not a restart: it re-reads logind.conf without dropping sessions, which is the
    documented way to apply a change and the safe thing to do in the middle of somebody's run.
    """
    tool = procutil.find_tool("systemctl")
    if tool is None:
        return False
    try:
        result = subprocess.run(
            [tool, "reload", "systemd-logind"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log("note: could not reload logind, so the lid switch may not be covered: %s" % exc)
        return False
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        log("note: could not reload logind, so the lid switch may not be covered: %s" % (
            detail[-1] if detail else "systemctl exited %s" % result.returncode))
        return False
    return True


@contextlib.contextmanager
def _lid_respects_the_lock(log):
    """Make logind consult inhibitor locks for the lid switch, for the block.

    Yields True when the setting was applied and False otherwise. Best-effort like the lock itself:
    if the drop-in cannot be written or logind cannot be reloaded, the run goes ahead with the lid
    uncovered rather than refusing to start. The drop-in is always removed on the way out, and only
    reloaded again if it was ever applied.
    """
    try:
        os.makedirs(LOGIND_DROPIN_DIR, exist_ok=True)
        with open(LOGIND_DROPIN, "w", encoding="utf-8") as handle:
            handle.write(_LOGIND_DROPIN_BODY)
    except OSError as exc:
        log("note: could not make the lid switch respect the run's wake-lock: %s" % exc)
        yield False
        return

    applied = _reload_logind(log)
    if not applied:
        # A drop-in logind will not re-read does nothing but sit there, so take it back now rather
        # than leave it for the exit path.
        _remove_dropin()
    try:
        yield applied
    finally:
        if applied:
            _remove_dropin()
            _reload_logind(log)


def _remove_dropin() -> None:
    try:
        os.remove(LOGIND_DROPIN)
    except OSError:
        pass


@contextlib.contextmanager
def inhibited(log=print):
    """Hold an inhibitor lock for the block, or explain why there is none.

    Yields the child process when the lock is held and None when it is not, so a caller can record
    which happened, and never raises: this is a convenience for the machine, not a precondition.
    """
    with _lid_respects_the_lock(log) as lid_covered:
        yield from _hold_inhibitor(log, lid_covered)


def _hold_inhibitor(log, lid_covered: bool):
    tool = procutil.find_tool("systemd-inhibit")
    if tool is None:
        log("note: systemd-inhibit is not installed, so this run cannot stop the machine sleeping")
        yield None
        return

    argv = [
        tool,
        "--mode=block",
        "--what=" + ":".join(WHAT),
        "--who=alma-certify",
        "--why=" + WHY,
        # ``cat`` rather than a sleep: it exits when the pipe closes, which happens on release and
        # also if this process dies without releasing.
        "cat",
    ]
    try:
        child = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )
    except OSError as exc:
        log("note: could not stop the machine sleeping: %s" % exc)
        yield None
        return

    try:
        child.wait(timeout=_SETTLE_SECONDS)
    except subprocess.TimeoutExpired:
        # Still running, so the lock is held. What it actually covers depends on whether the lid
        # drop-in took: without it the lid is not covered, and saying otherwise would be the lie
        # this whole change exists to stop.
        if lid_covered:
            log("suspend, idle, and lid-close are inhibited for the duration of this run")
        else:
            log("suspend and idle are inhibited for the duration of this run; the lid switch is "
                "not covered, so do not close the lid")
        try:
            yield child
        finally:
            _release(child)
        return

    # Exited already, which means it was refused. The child is dead, so reading cannot block.
    reason = (child.stderr.read() or "").strip().splitlines()
    log("note: could not stop the machine sleeping: %s" % (
        reason[-1] if reason else "systemd-inhibit exited %s" % child.returncode
    ))
    _close(child)
    yield None


def _release(child: subprocess.Popen) -> None:
    """Let go of the lock, and do not let letting go fail a run."""
    _close(child)
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()


def _close(child: subprocess.Popen) -> None:
    for stream in (child.stdin, child.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def held(child: Optional[subprocess.Popen]) -> bool:
    """Whether the lock is still held, for the report."""
    return child is not None and child.poll() is None
