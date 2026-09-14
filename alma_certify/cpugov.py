"""Hold the CPU frequency governor at ``performance`` for the length of a benchmark.

A benchmark measures the hardware, and the default governor works against that. ``powersave``,
``schedutil``, and ``ondemand`` all ramp frequency up lazily in response to load, so the opening
seconds of a pass run below the chip's real clock and the figure comes out low; on a laptop the
governor is ``powersave`` almost always. stress-ng says as much itself, noting on a run that "cpus
have scaling governors set to powersave and this may impact performance". Pinning ``performance``
for the benchmark removes that variable so a number describes the silicon rather than the governor.

**Set, then put back.** The original governor of every CPU is recorded and restored when the block
ends, however it ends, because the machine's normal power policy is the operator's to keep, not the
suite's to change permanently. A hard kill leaves the governor at ``performance``, which is a
performance choice and not a hazard, and the next boot resets it regardless.

**Best-effort, never a precondition.** Many virtual machines and cloud instances expose no cpufreq
at all, and some drivers offer no ``performance`` governor. Either way the run goes on under
whatever governor is there and says so, rather than refusing to benchmark for want of a setting.
Only ``benchmark`` runs use this: validation asks whether the hardware works, not how fast.

**Through the power-profiles daemon where one runs.** A desktop's power widget and, on a modern
``amd_pstate``/``intel_pstate`` in active mode, the clock itself are decided by
power-profiles-daemon (or tuned's ppd shim on 10), not by the raw ``scaling_governor``. Writing the
governor underneath it leaves the widget on "balanced" and can be reverted on the next power event,
so the number would not describe performance and nobody watching would see it engage. Where such a
daemon answers, its ``performance`` profile is held for the benchmark and restored after; the raw
governor is written only where no daemon runs (servers, VMs, minimal installs). The daemon is
reached through its ``powerprofilesctl`` client where that is installed and otherwise over its D-Bus
interface directly, because AlmaLinux 10 ships the daemon (as ``tuned-ppd``) without that client.
"""

from __future__ import annotations

import contextlib
import glob
import os
from typing import Dict, FrozenSet, Optional

from . import procutil

# One governor file per CPU. ``cpu[0-9]*`` rather than ``cpu*`` so the ``cpufreq`` and ``cpuidle``
# directories under ``/sys/devices/system/cpu`` are not swept up as if they were CPUs.
GOVERNOR_GLOB = "/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor"
TARGET = "performance"


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read().strip()


def _write(path: str, value: str) -> bool:
    """Write a governor, returning whether it took. Never raises: a CPU that refuses the write is
    not a reason to fail, and offlined CPUs and locked-down drivers both refuse."""
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(value)
        return True
    except OSError:
        return False


def _offers_performance(path: str) -> bool:
    """Whether this CPU's driver lists ``performance`` among the governors it will accept.

    Writing one it does not offer fails, so this is asked first: an ``intel_pstate`` in a mode that
    exposes only ``powersave`` would otherwise produce a write error per CPU and no change.
    """
    available = os.path.join(os.path.dirname(path), "scaling_available_governors")
    try:
        return TARGET in _read(available).split()
    except OSError:
        return False


# --- power-profiles-daemon -------------------------------------------------------
#
# The active power profile is what the desktop widget shows and, on an active-mode
# ``amd_pstate``/``intel_pstate``, what sets the EPP hint that governs the clock. A benchmark forces
# it to performance where a daemon answers, rather than writing the raw governor underneath it.
#
# There are two ways to reach the same daemon, because the packaging differs by release:
#
# - ``powerprofilesctl``, the daemon's own command-line client. It ships with power-profiles-daemon,
#   which is what AlmaLinux 9 runs. Preferred where installed.
# - the ``net.hadess.PowerProfiles`` D-Bus interface, reached with ``busctl``. AlmaLinux 10 has no
#   power-profiles-daemon package at all: it is obsoleted by ``tuned-ppd``, which serves that same
#   D-Bus name (so KDE and GNOME read it unchanged) but deliberately does not ship
#   ``powerprofilesctl``. Detecting the daemon by the client's presence therefore missed it on 10;
#   the governor was written underneath a live daemon, and the widget stayed on "balanced" for the
#   whole run. ``busctl`` is part of systemd, so it is on every AlmaLinux, and reading the profile
#   over it needs no privilege. Switching it is subject to the same polkit rule as the client (an
#   active desktop session), so neither path can switch the profile from a bare SSH login.
POWERPROFILESCTL = "powerprofilesctl"
PROFILE_TARGET = "performance"
# What a caller records when performance came from the power profile rather than the governor.
PPD_MARKER = "power-profile:performance"

# The D-Bus name power-profiles-daemon defined and tuned-ppd took over, with its object and
# interface. ActiveProfile is a read/write string property: reading it is what the widget does,
# writing it is what switches the profile.
BUSCTL = "busctl"
PPD_BUS_NAME = "net.hadess.PowerProfiles"
PPD_OBJECT_PATH = "/net/hadess/PowerProfiles"
PPD_INTERFACE = "net.hadess.PowerProfiles"
PPD_PROFILE_PROPERTY = "ActiveProfile"


def _have_ppd_cli() -> bool:
    return procutil.find_tool(POWERPROFILESCTL) is not None


def _cli_get() -> Optional[str]:
    try:
        res = procutil.run_cmd([POWERPROFILESCTL, "get"], timeout=15)
    except procutil.CommandNotFound:
        return None
    return res.stdout.strip() if res.ok else None


def _cli_offers(profile: str) -> bool:
    res = procutil.run_cmd([POWERPROFILESCTL, "list"], timeout=15)
    return bool(res.ok and profile in res.stdout)


def _cli_set(profile: str) -> bool:
    try:
        return procutil.run_cmd([POWERPROFILESCTL, "set", profile], timeout=15).ok
    except procutil.CommandNotFound:
        return False


def _parse_busctl_string(out: str) -> Optional[str]:
    """The value out of a ``busctl get-property`` string variant, printed as ``s "balanced"``."""
    out = out.strip()
    if '"' not in out:
        return None
    return out.split('"', 1)[1].rsplit('"', 1)[0] or None


def _busctl_get() -> Optional[str]:
    if procutil.find_tool(BUSCTL) is None:
        return None
    try:
        res = procutil.run_cmd(
            [BUSCTL, "get-property", PPD_BUS_NAME, PPD_OBJECT_PATH, PPD_INTERFACE,
             PPD_PROFILE_PROPERTY],
            timeout=15,
        )
    except procutil.CommandNotFound:
        return None
    return _parse_busctl_string(res.stdout) if res.ok else None


def _busctl_set(profile: str) -> bool:
    """Set the active profile over D-Bus, confirmed by reading it back.

    The read-back is the point: polkit refuses the switch without an active desktop session, and the
    refusal does not always come back as a non-zero exit, so a bare success is not proof it took.
    """
    if procutil.find_tool(BUSCTL) is None:
        return False
    try:
        res = procutil.run_cmd(
            [BUSCTL, "set-property", PPD_BUS_NAME, PPD_OBJECT_PATH, PPD_INTERFACE,
             PPD_PROFILE_PROPERTY, "s", profile],
            timeout=15,
        )
    except procutil.CommandNotFound:
        return False
    return bool(res.ok) and _busctl_get() == profile


def _ppd_get() -> Optional[str]:
    """The active power profile, or None when no power-profiles daemon answers here.

    Prefers the daemon's own client where it is installed (AlmaLinux 9), and otherwise reads the
    ``net.hadess.PowerProfiles`` D-Bus interface, which is how the profile is reached on AlmaLinux
    10, where the client is not packaged but the daemon (``tuned-ppd``) serves that name.
    """
    if _have_ppd_cli():
        return _cli_get()
    return _busctl_get()


def _ppd_offers(profile: str) -> bool:
    """Whether the daemon will offer ``profile``.

    The client can be asked directly (performance is hidden on some laptops on battery). Over D-Bus
    the answer is deferred to the verified set in ``_busctl_set``: the daemon answered ``get`` to
    bring us here, and whether this profile is actually settable is exactly what that set decides.
    """
    if _have_ppd_cli():
        return _cli_offers(profile)
    return True


def _ppd_set(profile: str) -> bool:
    if _have_ppd_cli():
        return _cli_set(profile)
    return _busctl_set(profile)


@contextlib.contextmanager
def _performance_via_ppd(current: str, log):
    """Hold the performance power profile for the block, restoring the prior one after.

    ``current`` is a real profile because a daemon answered. Restored however the block ends, as the
    governor path is; a hard kill leaves it at performance, which is a choice rather than a hazard,
    and it is not persisted across a reboot.
    """
    if current == PROFILE_TARGET:
        log("power profile is already performance; leaving it for the benchmark")
        yield frozenset()
        return
    if not _ppd_offers(PROFILE_TARGET) or not _ppd_set(PROFILE_TARGET):
        log("the power-profiles daemon would not switch to performance here (it is hidden on "
            "battery on some laptops, and switching it needs an active desktop session, so a run "
            "started over SSH cannot); benchmarking under the %r profile" % current)
        yield frozenset()
        return
    log("power profile set to performance for the benchmark (was %r); restored afterward" % current)
    try:
        yield frozenset({PPD_MARKER})
    finally:
        _ppd_set(current)


@contextlib.contextmanager
def _performance_via_governor(log):
    """Write the raw CPU governor, for hosts with no power-profiles daemon."""
    files = sorted(glob.glob(GOVERNOR_GLOB))
    if not files:
        log("note: no CPU frequency governor to set here (common in a VM); "
            "benchmarking under whatever the platform provides")
        yield frozenset()
        return

    original: Dict[str, str] = {}
    for path in files:
        try:
            current = _read(path)
        except OSError:
            continue
        if current == TARGET or not _offers_performance(path):
            continue
        if _write(path, TARGET):
            # Recorded only once the write took, so restore never writes a value that was never set.
            original[path] = current

    if original:
        log("CPU governor set to performance on %d of %d CPUs for the benchmark; "
            "each is restored afterward" % (len(original), len(files)))
    else:
        log("CPU governor left as it is: already performance, or no performance governor offered")

    try:
        yield frozenset(original)
    finally:
        for path, previous in original.items():
            _write(path, previous)


@contextlib.contextmanager
def performance(log=print):
    """Pin performance for the block, restoring the prior state after.

    Prefers power-profiles-daemon where it answers (it is what the desktop shows and what sets the
    EPP hint that governs clock on active-mode pstate drivers), and writes the raw CPU governor only
    where no such daemon runs. Yields the frozenset of things changed, empty when nothing was, so a
    caller can record whether the benchmark ran under forced performance or the machine's own.
    """
    profile = _ppd_get()
    if profile is not None:
        with _performance_via_ppd(profile, log) as changed:
            yield changed
        return
    with _performance_via_governor(log) as changed:
        yield changed


def forced(changed: FrozenSet[str]) -> bool:
    """Whether the governor was actually changed, for the report."""
    return bool(changed)
