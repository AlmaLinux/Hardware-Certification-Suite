"""Subprocess helpers: timeouts, process-group kill, artifact teeing, tracing.

Everything the suite runs goes through ``run_cmd`` so that:

- output is captured and optionally teed to an artifact file,
- a timeout always kills the whole process group (children included),
- the environment is normalized to LC_ALL=C so parsers see English output,
- and ``--debug`` can trace every command from one place (see ``set_debug``).
"""

from __future__ import annotations

import dataclasses
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
from typing import Callable, List, Optional, Sequence


@dataclasses.dataclass
class CmdResult:
    argv: List[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    # Wall-clock seconds. Defaulted so existing callers that build a CmdResult
    # by hand keep working.
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


# --- command tracing ---------------------------------------------------------
#
# Tracing lives here because ``run_cmd`` is the one place every tool goes
# through: inventory collection, package installs, and every test are covered at
# once, and no caller has to opt in or thread a flag down to reach it.
#
# A module-level sink rather than a parameter or a context object. ``run_cmd`` is
# called from dozens of places, none of which should grow a debug argument, and
# the suite is one run per process.
_debug_write: Optional[Callable[[str], None]] = None
_debug_label: Optional[Callable[[], Optional[str]]] = None


def set_debug(
    write: Optional[Callable[[str], None]],
    label: Optional[Callable[[], Optional[str]]] = None,
) -> None:
    """Trace every command to ``write``, or pass None to stop.

    ``label`` is called per command to name whatever is running it, so the trace
    reads as a sequence of tests instead of an undifferentiated wall of commands.
    It is a callable, not a string, because the current test changes as the run
    proceeds and the sink is installed once at the start.
    """
    global _debug_write, _debug_label
    _debug_write = write
    _debug_label = label


def debug_enabled() -> bool:
    return _debug_write is not None


def _label() -> str:
    """The current context, or "". Never raises: a broken label provider must not
    take down a run that would otherwise have succeeded."""
    if _debug_label is None:
        return ""
    try:
        return _debug_label() or ""
    except Exception:
        return ""


def _trace(line: str) -> None:
    if _debug_write is not None:
        _debug_write(line)


def _trace_start(argv: List[str], timeout: Optional[float]) -> None:
    """Announce the command *before* it runs.

    Deliberately not folded into the after-the-fact trace: a command that hangs
    produces no output to print, and knowing which one you are waiting on is the
    single most useful thing a trace can tell you at that moment.
    """
    label = _label()
    _trace("%s$ %s%s" % (
        "[%s] " % label if label else "",
        " ".join(shlex.quote(part) for part in argv),
        " (timeout %gs)" % timeout if timeout else "",
    ))


def _trace_end(result: CmdResult) -> None:
    for stream, text in (("stdout", result.stdout), ("stderr", result.stderr)):
        if not text:
            continue
        _trace("  --- %s ---" % stream)
        for line in text.splitlines():
            _trace("  " + line)
    if not result.stdout and not result.stderr:
        # Said explicitly, so "produced nothing" is distinguishable from "we did
        # not capture it".
        _trace("  (no output)")
    _trace("  --- exit %d%s in %.2fs ---" % (
        result.returncode,
        " TIMED OUT" if result.timed_out else "",
        result.duration_s,
    ))


class CommandNotFound(Exception):
    pass


def which(name: str) -> Optional[str]:
    return shutil.which(name)


# Directories the inherited environment routinely omits, searched after $PATH.
#
# ``shutil.which`` sees exactly the PATH it was given and nothing else. With PATH unset it falls
# back to ``os.confstr("CS_PATH")``, which is ``/usr/bin`` alone; with PATH set to the empty string
# it finds nothing at all. So which tests run depended on how the suite was invoked: a login shell,
# a systemd unit, and a cron job give three different answers on the same machine.
#
# On AlmaLinux 8 and 9 ``/usr/sbin`` is a separate directory from ``/usr/bin``, and it is where most
# of what this suite depends on lives: lspci, dmidecode, smartctl, ethtool, hwclock, modprobe,
# nvme. A run whose PATH omits it loses the hardware inventory and every test built on it.
_FALLBACK_DIRS = ("/usr/sbin", "/sbin", "/usr/local/sbin", "/usr/local/bin", "/usr/bin", "/bin")


def find_tool(name: str, extra_dirs: Sequence[str] = ()) -> Optional[str]:
    """An executable's absolute path, looking beyond $PATH. None if it is genuinely absent.

    One resolver, because this problem kept being solved in one place and missed in others. The
    NVIDIA tests needed it for nvcc, which NVIDIA's RPMs install under
    ``/usr/local/cuda-<version>/bin`` and put nowhere on PATH - one of several tools each found
    to be off PATH separately.

    ``extra_dirs`` is for a tool with a vendor-specific home, searched before the general fallbacks
    so a caller's own knowledge wins.

    Returns an absolute path, which ``run_cmd`` accepts directly, so nothing needs a doctored
    environment.
    """
    found = shutil.which(name)
    if found:
        return found
    for directory in tuple(extra_dirs) + _FALLBACK_DIRS:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


# --- live children -----------------------------------------------------------
#
# Every command the suite runs is its own session (``start_new_session`` below), so each one can be
# killed as a group without touching the suite. That is what a timeout does. This registry exists
# for the other case: the interface is abandoned part way through a run - the operator quits while
# a dnf transaction or a benchmark is mid-flight - and the run's thread is left detached. Without
# it those children carry on after the process that started them is gone, writing into a run
# directory nobody is watching.
_live = set()
_live_lock = threading.Lock()


def kill_live() -> int:
    """SIGKILL every command still running, by process group. Returns how many were signalled.

    Called when a run is abandoned rather than finished. Best effort by design: a child already
    reaped, or one this process may no longer signal, is not a failure worth reporting to somebody
    who has just asked to quit.
    """
    with _live_lock:
        procs = list(_live)
    killed = 0
    for proc in procs:
        if proc.poll() is not None:
            continue
        _kill_group(proc)
        killed += 1
    return killed


def run_cmd(
    argv: List[str],
    timeout: Optional[float] = None,
    tee_path: Optional[str] = None,
    check: bool = False,
    stdin: Optional[str] = None,
    env_extra: Optional[dict] = None,
    on_line: Optional[Callable[[str], None]] = None,
) -> CmdResult:
    """Run a command, capture output, kill the whole group on timeout.

    ``on_line`` makes a long command watchable. Without it this uses ``communicate``, which buffers
    everything until the process exits, and the tee writes the lot afterwards: so a command taking
    four minutes shows nothing for four minutes and is indistinguishable from a hang. That is fine
    for the hundreds of sub-second commands a run makes, and wrong for the few slow enough to worry
    somebody: a CUDA stack install or a kernel module build.

    Given a callback, each line is handed over as it arrives and still accumulated for the result,
    so the artifact and ``stdout``/``stderr`` are unchanged. Off by default, because streaming every
    command would bury the run's own log in package manager chatter.
    """
    if which(argv[0]) is None and not os.path.exists(argv[0]):
        # Traced too: a missing tool is the reason behind a great many skips, and
        # without this the trace would simply have no entry for the command.
        _trace("%s$ %s -> command not found" % (
            "[%s] " % _label() if _label() else "", shlex.quote(argv[0]),
        ))
        raise CommandNotFound(argv[0])

    if debug_enabled():
        _trace_start(argv, timeout)

    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    if env_extra:
        env.update(env_extra)

    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        env=env,
        start_new_session=True,
        text=True,
        errors="replace",
    )
    with _live_lock:
        _live.add(proc)
    timed_out = False
    started = time.monotonic()
    try:
        if on_line is not None:
            stdout, stderr, timed_out = _stream(proc, stdin, timeout, on_line)
        else:
            try:
                stdout, stderr = proc.communicate(input=stdin, timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(proc)
                # Bounded, because SIGKILL does not reach a process blocked in the kernel: a
                # smartctl or nvme call against a drive that has stopped answering sits in
                # uninterruptible sleep until its I/O returns, and an unbounded wait here made the
                # timeout it was enforcing meaningless - the run hung on the command it had just
                # given up on. A second communicate that times out leaves the child to the reaper
                # and reports what was read.
                try:
                    stdout, stderr = proc.communicate(timeout=_KILL_GRACE)
                except subprocess.TimeoutExpired:
                    stdout, stderr = "", ""
    finally:
        with _live_lock:
            _live.discard(proc)

    result = CmdResult(
        argv=list(argv),
        returncode=proc.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
        timed_out=timed_out,
        duration_s=round(time.monotonic() - started, 3),
    )
    if debug_enabled():
        _trace_end(result)
    if tee_path:
        _write_tee(tee_path, result)
    if check and not result.ok:
        raise subprocess.CalledProcessError(
            result.returncode, argv, output=result.stdout, stderr=result.stderr
        )
    return result


def _stream(proc, stdin, timeout, on_line):
    """Read both pipes as they fill, reporting each line. Returns (stdout, stderr, timed_out).

    A thread per pipe rather than a select loop, because that is what ``communicate`` does under the
    hood and for the same reason: reading one pipe to exhaustion while the other fills will deadlock
    on a command that writes enough to either.
    """
    collected = {"stdout": [], "stderr": []}

    def drain(stream, key):
        try:
            for line in iter(stream.readline, ""):
                collected[key].append(line)
                try:
                    on_line(line.rstrip("\n"))
                except Exception:  # noqa: BLE001 - a reporting failure must not kill the command
                    pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    if stdin is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin)
        finally:
            proc.stdin.close()
    readers = [
        threading.Thread(target=drain, args=(proc.stdout, "stdout"), daemon=True),
        threading.Thread(target=drain, args=(proc.stderr, "stderr"), daemon=True),
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        # Bounded for the reason given in run_cmd: a killed process is not always a dead one.
        try:
            proc.wait(timeout=_KILL_GRACE)
        except subprocess.TimeoutExpired:
            pass
    # Bounded, so a child that leaked a still-open pipe to a grandchild cannot hang the run here
    # after the command itself is gone - and not waited on at all when the command is not gone.
    # The readers are blocked reading pipes the process still holds open, so joining them is
    # waiting on the process a second time by another name; they are daemon threads and the
    # output they would have collected is output nobody is getting either way.
    grace = 10 if proc.poll() is not None else 0
    for reader in readers:
        reader.join(timeout=grace)
    return "".join(collected["stdout"]), "".join(collected["stderr"]), timed_out


# How long to wait for a killed command to actually die before giving up on it. SIGKILL is
# immediate for a process that can receive it and has no effect at all on one stuck in the kernel,
# so this is short: either it is already gone or no amount of waiting will change that.
_KILL_GRACE = 5.0


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def _write_tee(path: str, result: CmdResult) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:  # type: IO[str]
        fh.write("$ " + " ".join(result.argv) + "\n")
        fh.write(result.stdout)
        if result.stderr:
            fh.write("\n--- stderr ---\n")
            fh.write(result.stderr)
        if result.timed_out:
            fh.write("\n--- TIMED OUT ---\n")
        fh.write("\n--- exit %d ---\n" % result.returncode)


def read_file(path: str, default: Optional[str] = None) -> Optional[str]:
    """Read a small file (sysfs/proc), returning ``default`` on any error."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return default
