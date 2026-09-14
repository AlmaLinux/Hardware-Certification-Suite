"""Platform checks: SELinux, Secure Boot, firmware, RTC, watchdog, thermal, AER."""

from __future__ import annotations

import glob
import os
import re
import time
from typing import Optional

from .. import hwquery, procutil
from ..registry import REGISTRY, RunContext, Test
from ..result import Severity, Status


class SelinuxState(Test):
    """Record which SELinux mode the run was performed in.

    Not a hardware property. A machine with SELinux permissive has nothing wrong
    with it, so failing certification for a boot parameter condemned good
    hardware - and it did: a real submission failed here with SELinux disabled
    while every hardware test on it passed.

    Recorded rather than gated because what it actually describes is
    **provenance**: the configuration the evidence was collected in. That is
    worth knowing, because driver probing and firmware loading do go through
    paths that behave differently under enforcing, so a pass collected with
    SELinux off is weaker evidence than the same pass under the configuration
    AlmaLinux ships. How much weaker is a reviewer's judgement about a specific
    submission, not a threshold this test can set.
    """

    id = "validate.platform.selinux"
    category = "platform"
    run_type = "validate"
    severity = Severity.INFORMATIONAL
    default_timeout = 30

    def run(self, ctx: RunContext):
        try:
            res = ctx.cmd(["getenforce"], timeout=15)
        except procutil.CommandNotFound:
            # Nothing was measured, which is not the same as a finding.
            return self.result(
                Status.SKIP, reason="SELinux tooling (getenforce) is not installed"
            )
        state = (res.stdout or "").strip().lower() or "unknown"
        details = {"state": state}
        if state == "enforcing":
            return self.result(
                Status.PASS, reason="SELinux enforcing", details=details
            )
        return self.result(
            Status.PASS,
            reason=(
                "worth a look: SELinux is %s, so this run was not performed in "
                "the configuration AlmaLinux ships. The hardware results stand; "
                "treat them as weaker evidence for anything that depends on "
                "driver or firmware-loading paths under enforcing" % state
            ),
            details=details,
        )


class SecureBootInfo(Test):
    id = "validate.platform.secureboot"
    category = "platform"
    run_type = "validate"
    severity = Severity.INFORMATIONAL
    default_timeout = 30

    def run(self, ctx: RunContext):
        state = "unknown"
        try:
            res = ctx.cmd(["mokutil", "--sb-state"], timeout=15)
            if res.ok:
                state = "enabled" if "enabled" in res.stdout.lower() else "disabled"
        except procutil.CommandNotFound:
            if not hwquery.is_efi():
                state = "disabled"
        return self.result(
            Status.PASS,
            details={"secure_boot": state, "efi": hwquery.is_efi()},
        )


class FirmwareInfo(Test):
    id = "validate.platform.firmware"
    category = "platform"
    run_type = "validate"
    severity = Severity.INFORMATIONAL
    default_timeout = 120

    def run(self, ctx: RunContext):
        bios = ctx.summary.get("system", {}).get("bios", {})
        details = {"bios": bios}
        try:
            res = ctx.cmd(
                ["fwupdmgr", "get-devices", "--json"],
                timeout=90,
                artifact="fwupdmgr.json",
            )
            if res.ok:
                details["fwupd"] = "captured"
        except procutil.CommandNotFound:
            pass
        if not bios.get("version"):
            return self.result(
                Status.PASS, reason="BIOS version not reported by DMI", details=details
            )
        return self.result(Status.PASS, details=details)


class RtcCheck(Test):
    """Does the real-time clock work as hardware?

    Deliberately *not* a comparison against the system clock. ``hwclock``
    renders the RTC through the system timezone and the /etc/adjtime
    convention, so that comparison really asks "is the system clock synced to
    the RTC", which is chrony's job. Live media runs no NTP and often ships
    no /etc/adjtime, so perfectly good hardware failed that check.

    What is actually hardware:

    1. the RTC is readable,
    2. it is *running* - two reads a few seconds apart must advance,
    3. it is writable - set it, read it back, then put it back.

    The offset against the system clock is still recorded, as information for
    a reviewer, but never gates the result.
    """

    id = "validate.platform.rtc"
    category = "platform"
    run_type = "validate"
    severity = Severity.REQUIRED
    default_timeout = 120

    # Small enough that a failed restore leaves the clock only slightly off,
    # large enough to be unambiguous against read jitter.
    WRITE_OFFSET_SECONDS = 120

    def applicable(self, ctx: RunContext):
        if not os.path.exists("/dev/rtc") and not glob.glob("/dev/rtc[0-9]*"):
            return "no RTC device"
        return None

    def _read(self, ctx: RunContext):
        """Return the RTC time as an aware datetime, or None.

        No --utc/--localtime flag: hwclock's configured convention is used
        for both reads and writes, so offsets computed between them hold
        whichever convention this machine uses.
        """
        res = ctx.cmd(["hwclock", "-r"], timeout=30, artifact="hwclock.txt")
        if not res.ok:
            return None
        return _parse_hwclock(res.stdout)

    def run(self, ctx: RunContext):
        import datetime

        artifacts = [ctx.rel_artifact("hwclock.txt")]
        try:
            first = self._read(ctx)
        except procutil.CommandNotFound:
            return self.result(Status.ERROR, reason="hwclock not available")
        if first is None:
            return self.result(
                Status.FAIL, reason="the RTC could not be read",
                artifacts=artifacts,
            )

        details = {"rtc_time": first.isoformat()}
        system_now = datetime.datetime.now(datetime.timezone.utc)
        details["system_offset_s"] = round(
            (system_now - first).total_seconds(), 1
        )

        # 2. is it running?
        time.sleep(3)
        second = self._read(ctx)
        if second is None:
            return self.result(
                Status.FAIL, reason="the RTC became unreadable mid-test",
                details=details, artifacts=artifacts,
            )
        advanced = (second - first).total_seconds()
        details["advanced_s"] = round(advanced, 1)
        if advanced <= 0:
            return self.result(
                Status.FAIL,
                reason="the RTC is not running: it read %s twice over 3 seconds"
                % second.isoformat(),
                details=details, artifacts=artifacts,
            )

        # 3. is it writable? Set it forward, confirm the write took, put it
        # back. Restored in a finally block so an exception cannot leave the
        # clock wrong.
        write_result = self._check_writable(ctx, second, details)
        if write_result is not None:
            return self.result(
                Status.FAIL, reason=write_result, details=details,
                artifacts=artifacts,
            )
        return self.result(Status.PASS, details=details, artifacts=artifacts)

    def _check_writable(self, ctx: RunContext, before, details) -> Optional[str]:
        """Set / verify / restore. Returns a failure reason, or None."""
        import datetime

        target = before + datetime.timedelta(seconds=self.WRITE_OFFSET_SECONDS)
        started = time.monotonic()
        set_res = ctx.cmd(
            ["hwclock", "--set", "--date", target.strftime("%Y-%m-%d %H:%M:%S")],
            timeout=30,
            artifact="hwclock.txt",
        )
        if not set_res.ok:
            details["writable"] = False
            return "the RTC could not be set: %s" % _first_line(set_res)
        try:
            readback = self._read(ctx)
            if readback is None:
                details["writable"] = False
                return "the RTC could not be read after being set"
            # allow for the seconds spent between the two operations
            elapsed = time.monotonic() - started
            error = abs((readback - target).total_seconds() - elapsed)
            details["writable"] = error <= 5
            details["write_error_s"] = round(error, 1)
            if error > 5:
                return (
                    "the RTC did not hold the value it was set to "
                    "(off by %.1fs)" % error
                )
            return None
        finally:
            restore = before + datetime.timedelta(
                seconds=time.monotonic() - started
            )
            restored = ctx.cmd(
                ["hwclock", "--set", "--date",
                 restore.strftime("%Y-%m-%d %H:%M:%S")],
                timeout=30,
                artifact="hwclock.txt",
            )
            details["restored"] = restored.ok
            if not restored.ok:
                ctx.log(
                    "warning: could not restore the RTC; it may be up to "
                    "%d seconds fast" % self.WRITE_OFFSET_SECONDS
                )


def _first_line(res) -> str:
    for line in (res.stderr + res.stdout).splitlines():
        if line.strip():
            return line.strip()[:120]
    return "exit %d" % res.returncode


def _parse_hwclock(output: str):
    """Parse hwclock's output across util-linux versions.

    Modern releases print ISO-ish "2026-07-29 14:03:12.123456+00:00"; older
    ones print "Tue 29 Jul 2026 02:03:12 PM EDT  -0.123456 seconds". Returns
    an aware datetime, assuming local time when no offset is given.
    """
    import datetime

    text = output.strip().splitlines()[0].strip() if output.strip() else ""
    if not text:
        return None
    # ISO form, optionally with a fractional part and an offset
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None:
        # drop a trailing "  -0.123456 seconds" then try common layouts
        stripped = re.sub(r"\s+[-+][\d.]+\s+seconds?$", "", text)
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%a %d %b %Y %I:%M:%S %p %Z",
            "%a %b %d %H:%M:%S %Y",
        ):
            try:
                parsed = datetime.datetime.strptime(stripped.split(".")[0], fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


class WatchdogPresent(Test):
    id = "validate.platform.watchdog"
    category = "platform"
    run_type = "validate"
    severity = Severity.CONDITIONAL
    default_timeout = 30

    def applicable(self, ctx: RunContext):
        if not hwquery.watchdog_devices():
            return "no watchdog device"
        return None

    def run(self, ctx: RunContext):
        devices = []
        for path in hwquery.watchdog_devices():
            devices.append(
                {
                    "device": os.path.basename(path),
                    "identity": procutil.read_file(os.path.join(path, "identity")),
                }
            )
        # presence + identified driver; never pet or fire the watchdog
        return self.result(Status.PASS, details={"watchdogs": devices})


class ThermalZones(Test):
    id = "validate.platform.thermal"
    category = "platform"
    run_type = "validate"
    severity = Severity.CONDITIONAL
    default_timeout = 60

    def applicable(self, ctx: RunContext):
        if not hwquery.thermal_zones():
            return "no ACPI thermal zones"
        return None

    def run(self, ctx: RunContext):
        problems = []
        zones = []
        for zone in hwquery.thermal_zones():
            temp = procutil.read_file(os.path.join(zone, "temp"))
            ztype = procutil.read_file(os.path.join(zone, "type"), "unknown")
            if temp is None:
                continue
            try:
                temp_c = int(temp) / 1000.0
            except ValueError:
                continue
            zones.append({"zone": os.path.basename(zone), "type": ztype, "temp_c": temp_c})
            # compare against critical trip point if exposed
            for trip in glob.glob(os.path.join(zone, "trip_point_*_type")):
                if (procutil.read_file(trip) or "") == "critical":
                    crit = procutil.read_file(trip.replace("_type", "_temp"))
                    try:
                        if crit and temp_c >= int(crit) / 1000.0:
                            problems.append(
                                "%s (%s) at %.1f°C ≥ critical" % (zone, ztype, temp_c)
                            )
                    except ValueError:
                        pass
        if problems:
            return self.result(Status.FAIL, reason="; ".join(problems),
                               details={"zones": zones})
        return self.result(Status.PASS, details={"zones": zones})


class PcieAer(Test):
    id = "validate.platform.pcie-aer"
    category = "platform"
    run_type = "validate"
    severity = Severity.REQUIRED
    default_timeout = 60

    def run(self, ctx: RunContext):
        res = ctx.cmd(["dmesg"], timeout=30)
        aer_lines = [
            line.strip()
            for line in (res.stdout.splitlines() if res.ok else [])
            if "AER:" in line and ("error" in line.lower() or "corrected" in line.lower())
        ]
        uncorrected = [line for line in aer_lines if "uncorrected" in line.lower()]
        details = {"aer_events": len(aer_lines), "sample": aer_lines[:10]}
        if uncorrected:
            return self.result(
                Status.FAIL,
                reason="%d uncorrected PCIe AER error(s) logged" % len(uncorrected),
                details=details,
            )
        if aer_lines:
            return self.result(
                Status.PASS,
                reason="corrected AER events present (not gating)",
                details=details,
            )
        return self.result(Status.PASS, details=details)


class AudioPresence(Test):
    id = "validate.media.audio"
    category = "media"
    run_type = "validate"
    severity = Severity.INFORMATIONAL
    default_timeout = 30

    def run(self, ctx: RunContext):
        cards = procutil.read_file("/proc/asound/cards", "") or ""
        has_audio = bool(cards.strip()) and "no soundcards" not in cards
        return self.result(
            Status.PASS,
            details={"audio_present": has_audio, "cards": cards.strip()[:500]},
        )


class VideoPresence(Test):
    id = "validate.media.video"
    category = "media"
    run_type = "validate"
    severity = Severity.INFORMATIONAL
    default_timeout = 30

    def run(self, ctx: RunContext):
        cards = sorted(
            os.path.basename(p) for p in glob.glob("/sys/class/drm/card[0-9]*")
            if "-" not in os.path.basename(p)
        )
        connectors = {}
        for card in cards:
            for conn in glob.glob("/sys/class/drm/%s-*" % card):
                status = procutil.read_file(os.path.join(conn, "status"))
                if status:
                    connectors[os.path.basename(conn)] = status
        return self.result(
            Status.PASS, details={"drm_cards": cards, "connectors": connectors}
        )


REGISTRY.register(SelinuxState)
REGISTRY.register(SecureBootInfo)
REGISTRY.register(FirmwareInfo)
REGISTRY.register(RtcCheck)
REGISTRY.register(WatchdogPresent)
REGISTRY.register(ThermalZones)
REGISTRY.register(PcieAer)
REGISTRY.register(AudioPresence)
REGISTRY.register(VideoPresence)
