"""Power management: cpufreq scaling, suspend/resume (interactive)."""

from __future__ import annotations

import glob
import os
import re
import time

from .. import hwquery, procutil
from ..registry import REGISTRY, RunContext, Test
from ..result import Severity, Status


class CpufreqScaling(Test):
    id = "validate.power.cpufreq"
    category = "power"
    run_type = "validate"
    severity = Severity.CONDITIONAL
    default_timeout = 120

    def applicable(self, ctx: RunContext):
        if not hwquery.cpufreq_available():
            return "no cpufreq driver (frequency scaling not exposed)"
        return None

    def run(self, ctx: RunContext):
        base = "/sys/devices/system/cpu/cpu0/cpufreq"
        driver = procutil.read_file(os.path.join(base, "scaling_driver"))
        governor = procutil.read_file(os.path.join(base, "scaling_governor"))
        governors = (procutil.read_file(
            os.path.join(base, "scaling_available_governors"), "") or "").split()
        details = {
            "driver": driver,
            "governor": governor,
            "available_governors": governors,
        }
        if not driver:
            return self.result(Status.FAIL, reason="cpufreq driver not identifiable",
                               details=details)
        # verify the governor can be switched and switched back
        if governor and len(governors) > 1:
            other = next((g for g in governors if g != governor), None)
            if other:
                try:
                    with open(os.path.join(base, "scaling_governor"), "w") as fh:
                        fh.write(other)
                    time.sleep(1)
                    now = procutil.read_file(os.path.join(base, "scaling_governor"))
                    switched = now == other
                finally:
                    with open(os.path.join(base, "scaling_governor"), "w") as fh:
                        fh.write(governor)
                details["governor_switch"] = switched
                if not switched:
                    return self.result(
                        Status.FAIL, reason="governor switch did not take effect",
                        details=details,
                    )
        return self.result(Status.PASS, details=details)


class SuspendResume(Test):
    """rtcwake-based suspend for 20s, then verify devices re-enumerate.
    Interactive: suspending a remote/headless server kills the session."""

    id = "validate.power.suspend"
    category = "power"
    run_type = "validate"
    severity = Severity.CONDITIONAL
    interactive = True
    default_timeout = 300

    def applicable(self, ctx: RunContext):
        states = procutil.read_file("/sys/power/state", "") or ""
        if "mem" not in states.split():
            return "suspend-to-RAM not supported by this platform"
        return None

    def run(self, ctx: RunContext):
        before_pci = self._pci_count(ctx)
        res = ctx.cmd(
            ["rtcwake", "-m", "mem", "-s", "20"],
            timeout=180,
            artifact="rtcwake.log",
        )
        if not res.ok:
            return self.result(
                Status.FAIL, reason="suspend/resume via rtcwake failed",
                artifacts=[ctx.rel_artifact("rtcwake.log")],
            )
        time.sleep(5)
        after_pci = self._pci_count(ctx)
        details = {"pci_devices_before": before_pci, "pci_devices_after": after_pci}
        if after_pci != before_pci:
            return self.result(
                Status.FAIL,
                reason="PCI device count changed across suspend (%d -> %d)"
                % (before_pci, after_pci),
                details=details,
            )
        return self.result(Status.PASS, details=details)

    def _pci_count(self, ctx: RunContext) -> int:
        return len(glob.glob("/sys/bus/pci/devices/*"))


REGISTRY.register(CpufreqScaling)
REGISTRY.register(SuspendResume)


class BacklightControl(Test):
    """Can the system actually change display brightness?

    Brightness reaches a display by two different routes, and a machine only
    has nothing to test when it has neither:

    - the kernel's sysfs backlight class, which is what an internal laptop
      panel exposes (along with external panels some drivers can drive)
    - DDC/CI over the monitor's own I2C channel, which is how a desktop
      drives an external monitor, and what a desktop session's brightness
      slider uses when /sys/class/backlight is empty

    Looking only at sysfs told a desktop whose monitor was perfectly
    controllable that its brightness was "not under system control", so the
    second route is not optional for the verdict to mean anything.

    No GUI is involved in either route, deliberately: a desktop session's
    power daemon competes for the same control, so going straight at the
    hardware tests the hardware rather than the desktop's opinion of it.

    Control is demonstrated by the panel *responding*, not by it echoing a
    number back. ``brightness`` and ``actual_brightness`` are not guaranteed to
    share a scale: amdgpu reports the latter through the panel's PWM curve, so
    on an AMD laptop it never equals what was written. See ``_classify``.

    Outcomes, because "something else owns this" and "this display has no
    brightness control" are both different from broken:

    - the panel showed our value, or moved as asked  -> pass
    - the value snaps back to where it started       -> skip (something owns it)
    - the value is still moving on its own           -> skip (contended)
    - no route offers an adjustable brightness       -> skip (nothing to test)
    - the driver refuses a valid value               -> fail
    """

    id = "validate.power.backlight"
    category = "power"
    run_type = "validate"
    severity = Severity.CONDITIONAL
    default_timeout = 300

    SETTLE_SECONDS = 1.0
    # Sampled across the settle window instead of read once at the end. A
    # desktop session moves the brightness for its own reasons mid-test (idle
    # dimming is the usual one), and a single late read cannot tell "the panel
    # refused our value" from "something changed it afterwards". The first
    # sample is taken immediately, before anything in userspace can react.
    SAMPLES = 4
    # DDC/CI is a slow serial protocol over I2C and monitors are allowed to
    # take their time, so it gets a longer settle and generous per-call
    # timeouts. Both are far below the test timeout above. Fewer samples,
    # because each one is a full protocol round trip.
    DDC_SETTLE_SECONDS = 2.0
    DDC_SAMPLES = 2
    DDC_TIMEOUT = 45
    # VCP feature code 0x10 is "Brightness" in the MCCS specification.
    DDC_FEATURE = "10"
    DETECT_LOG = "ddcutil-detect.log"

    # -- discovery ---------------------------------------------------------

    @staticmethod
    def _sysfs_devices() -> list:
        return sorted(glob.glob("/sys/class/backlight/*"))

    @staticmethod
    def _connected_outputs() -> list:
        """DRM connectors with something plugged into them.

        Cheap and dependency-free, and it distinguishes the two cases the old
        skip reason ran together: a headless server with no display at all,
        and a desktop with a monitor whose brightness simply is not reachable
        through sysfs.
        """
        found = []
        for path in sorted(glob.glob("/sys/class/drm/card*-*/status")):
            if (procutil.read_file(path, "") or "").strip() == "connected":
                found.append(os.path.basename(os.path.dirname(path)))
        return found

    def applicable(self, ctx: RunContext):
        if self._sysfs_devices():
            return None
        if not self._connected_outputs():
            return (
                "no display attached: the kernel exposes no backlight device "
                "and no display output is connected"
            )
        return None

    def setup(self, ctx: RunContext) -> None:
        self._ddc_ready = False
        self._ddcutil_unavailable = False
        self._ddcutil_version = ""
        self._i2c_dev_loaded = False
        if self._sysfs_devices():
            return  # the kernel route needs no tooling

        # A display is attached but the kernel offers no backlight, so DDC/CI
        # is the only route left. ddcutil comes from EPEL and EPEL 8 has no
        # build of it, so this stays best-effort: a tool this platform cannot
        # supply means "could not test", never a failure.
        if not procutil.which("ddcutil") and ctx.pkg:
            self._ddcutil_unavailable = bool(
                ctx.pkg.missing(("ddcutil",), repos=("epel",))
            )
        if not procutil.which("ddcutil"):
            self._ddcutil_unavailable = True
            return
        # Recorded for comparability: ddcutil 1.x and 2.x differ in their
        # output shapes, and EPEL 9 and 10 carry different major versions.
        version = procutil.run_cmd(["ddcutil", "--version"], timeout=30)
        lines = (version.stdout or "").strip().splitlines()
        self._ddcutil_version = lines[0].strip() if lines else ""

        if not glob.glob("/dev/i2c-*") and procutil.which("modprobe"):
            # ddcutil reaches the monitor through /dev/i2c-*, which only exist
            # once i2c-dev is loaded. It is an in-tree module, so loading it
            # does not taint the kernel, and it is left loaded afterwards
            # because a desktop session may now be relying on it too.
            procutil.run_cmd(["modprobe", "i2c-dev"], timeout=30)
            self._i2c_dev_loaded = bool(glob.glob("/dev/i2c-*"))
        self._ddc_ready = bool(glob.glob("/dev/i2c-*"))

    # -- the run -----------------------------------------------------------

    def run(self, ctx: RunContext):
        outcomes = [self._exercise_sysfs(ctx, p) for p in self._sysfs_devices()]
        artifacts: list = []
        if not outcomes and getattr(self, "_ddc_ready", False):
            outcomes.extend(self._exercise_ddc(ctx))
            artifacts.append(ctx.rel_artifact(self.DETECT_LOG))

        details = {"displays": outcomes}
        if getattr(self, "_ddcutil_version", ""):
            details["ddcutil_version"] = self._ddcutil_version
        if getattr(self, "_i2c_dev_loaded", False):
            details["i2c_dev_loaded_by_suite"] = True

        def having(*results):
            return [o for o in outcomes if o["result"] in results]

        if having("controlled"):
            return self.result(Status.PASS, details=details, artifacts=artifacts)
        # Checked before the hard-failure branch on purpose: if any display is
        # merely contested, the machine has not demonstrated a defect, and a
        # vestigial acpi_video0 that refuses writes should not condemn it.
        contested = having("reverted", "contended", "unverifiable")
        if contested:
            return self.result(
                Status.SKIP,
                reason=(
                    "the brightness result cannot be attributed to the "
                    "hardware (%s). The driver accepted the value we wrote; "
                    "what the display then reported was either moved by "
                    "something else (a desktop session's power daemon or its "
                    "idle dimming) or reported on a different scale than it "
                    "accepts. Re-run from a console to test the hardware path"
                    % "; ".join("%s %s" % (r["device"], r["result"])
                                for r in contested)
                ),
                details=details,
                artifacts=artifacts,
            )
        broken = having("write rejected", "write refused", "unreadable")
        if broken:
            return self.result(
                Status.FAIL,
                reason="the driver did not accept a valid brightness value on "
                "%s" % "; ".join(
                    "%s (%s)" % (r["device"], r.get("error") or r["result"])
                    for r in broken
                ),
                details=details,
                artifacts=artifacts,
            )
        return self.result(
            Status.SKIP,
            reason=self._nothing_adjustable(outcomes),
            details=details,
            artifacts=artifacts,
        )

    def _nothing_adjustable(self, outcomes: list) -> str:
        """Say what was actually checked, not what it implies.

        The old wording asserted that brightness was "not under system
        control" on the strength of an empty /sys/class/backlight, which was
        wrong wherever DDC/CI worked. Every branch here names the route it
        could not use and why.
        """
        sysfs = self._sysfs_devices()
        if sysfs:
            return (
                "brightness is not adjustable on this system: the kernel "
                "backlight device(s) %s expose no adjustable range"
                % ", ".join(os.path.basename(p) for p in sysfs)
            )

        parts = ["the kernel exposes no backlight device"]
        if getattr(self, "_ddcutil_unavailable", False):
            parts.append(
                "and DDC/CI could not be tested because ddcutil is not "
                "available in the configured repositories (AlmaLinux 8 ships "
                "no build of it)"
            )
        elif not getattr(self, "_ddc_ready", False):
            parts.append(
                "and DDC/CI could not be tested because no /dev/i2c-* device "
                "is present"
            )
        else:
            parts.append(
                "and the attached display offered no brightness control over "
                "DDC/CI (%s)"
                % "; ".join(o.get("note") or o["result"] for o in outcomes)
            )
        return "brightness is not adjustable on this system: " + " ".join(parts)

    # -- shared decision rules ---------------------------------------------

    @staticmethod
    def _pick_target(original: int, maximum: int) -> int:
        """A value clearly different from the current one, and never 0 - a
        display left fully dark looks like a broken machine if restore fails.
        """
        target = maximum // 4 if original > maximum // 2 else (maximum * 3) // 4
        return max(1, min(maximum, target))

    def _watch(self, read, count: int, window=None) -> list:
        """Read `count` times across the settle window, first read immediate.

        The immediate read is the one that decides the verdict: it shows what
        the driver programmed before any other process can interfere. The later
        reads only say whether the value then held.
        """
        window = self.SETTLE_SECONDS if window is None else window
        step = float(window) / (count - 1) if count > 1 else 0.0
        samples = [read()]
        for _ in range(count - 1):
            if step:
                time.sleep(step)
            samples.append(read())
        return samples

    @staticmethod
    def _classify(samples: list, target: int, original: int,
                  baseline: int, tolerance: int) -> str:
        """Decide from the whole sample series, not just the last reading.

        Two ways to demonstrate control, because ``brightness`` and
        ``actual_brightness`` are not necessarily the same scale:

        1. a sample equals what we wrote. True on i915 and on DDC/CI, where
           the reported value is the value accepted.
        2. a sample moved away from where the panel was, in the direction we
           asked for. amdgpu reports ``actual_brightness`` through the panel's
           PWM curve, so on an AMD laptop it never equals what was written
           (writing 299250 of 399000 reads back 267001). Requiring equality
           there fails every one of them, so what matters is that the hardware
           moved, and moved the way it was told.

        Order matters. If the display ever showed our value or ever responded,
        the write was honored whatever happened afterwards: a laptop that dims
        itself mid-test is not a laptop with a broken backlight.
        """
        def near(a, b):
            return abs(a - b) <= tolerance

        if any(near(s, target) for s in samples):
            return "controlled"
        wanted = target - original
        if wanted:
            for sample in samples:
                moved = sample - baseline
                if abs(moved) >= tolerance and (moved > 0) == (wanted > 0):
                    return "controlled"
        if len(set(samples)) > 1:
            return "contended"      # still moving, so somebody else is driving
        if near(samples[-1], baseline) or near(samples[-1], original):
            return "reverted"
        # Stable, unequal to our value, and it never moved. The write itself
        # was verified separately, so this is not a defect we can attribute to
        # the hardware; it is simply not something software can confirm.
        return "unverifiable"

    # -- route 1: the kernel backlight class -------------------------------

    def _exercise_sysfs(self, ctx: RunContext, path: str) -> dict:
        """Set a distinct brightness, read it back, then restore."""
        name = os.path.basename(path)
        info = {
            "device": name,
            "interface": "sysfs",
            "type": procutil.read_file(os.path.join(path, "type")),
            "result": "unknown",
        }
        try:
            maximum = int(procutil.read_file(os.path.join(path, "max_brightness")) or 0)
            original = int(procutil.read_file(os.path.join(path, "brightness")) or 0)
        except ValueError:
            info["result"] = "unreadable"
            info["error"] = "max_brightness or brightness is not a number"
            return info
        if maximum <= 1:
            # An on/off stub, commonly a vestigial acpi_video0. There is no
            # range to verify, so this is nothing-to-test rather than broken.
            info["result"] = "not adjustable"
            info["note"] = "max_brightness is %d, so there is no range to set" % maximum
            return info
        info["max_brightness"] = maximum
        info["original"] = original
        info["target"] = target = self._pick_target(original, maximum)
        # What the panel reports *before* the write. Without this there is
        # nothing to compare against on a driver whose actual_brightness runs
        # on its own scale, which is why the old check could only ever ask the
        # unanswerable question "does it equal what we wrote".
        info["baseline"] = baseline = self._read_actual(path)

        try:
            self._write(path, target)
        except OSError as exc:
            info["result"] = "write refused"
            info["error"] = str(exc)
            return info

        # Read `brightness` back straight away. It holds what the driver
        # accepted, so a mismatch here means the kernel silently rejected or
        # clamped a value that was already inside [1, max_brightness]. That is
        # a driver defect, and nothing in userspace could have caused it in the
        # microseconds since the write. This is the one hardware-attributable
        # failure this test can make; everything after it is interpretation.
        try:
            accepted = int(procutil.read_file(
                os.path.join(path, "brightness")) or -1)
        except ValueError:
            accepted = -1
        info["accepted"] = accepted
        if accepted != target:
            info["result"] = "write rejected"
            info["error"] = (
                "wrote %d to brightness, driver stored %d" % (target, accepted)
            )
            try:
                self._write(path, original)
            except OSError:
                pass
            return info

        try:
            samples = self._watch(lambda: self._read_actual(path), self.SAMPLES)
            # Kept in the result: when a verdict is disputed, the sample series
            # is the difference between diagnosing it and guessing.
            info["samples"] = samples
            info["observed"] = samples[-1]
            info["result"] = self._classify(
                samples, target, original, baseline, max(1, maximum // 100)
            )
            return info
        finally:
            try:
                self._write(path, original)
                info["restored"] = True
            except OSError:
                info["restored"] = False
                ctx.log(
                    "warning: could not restore %s brightness to %d"
                    % (name, original)
                )

    @staticmethod
    def _read_actual(path: str) -> int:
        """actual_brightness is what the hardware reports; brightness is only
        what was last written, so it would pass trivially. Not every driver
        exposes the former."""
        raw = procutil.read_file(
            os.path.join(path, "actual_brightness")
        ) or procutil.read_file(os.path.join(path, "brightness"))
        try:
            return int(raw or -1)
        except ValueError:
            return -1

    @staticmethod
    def _write(path: str, value: int) -> None:
        with open(os.path.join(path, "brightness"), "w") as fh:
            fh.write("%d\n" % value)

    # -- route 2: DDC/CI over I2C ------------------------------------------

    _DISPLAY_RE = re.compile(r"^Display\s*(\d+)?\s*$", re.I)
    _BUS_RE = re.compile(r"/dev/i2c-(\d+)")
    # ddcutil 2.x terse form: "VCP 10 C 50 100" (code, type, current, max).
    _VCP_TERSE_RE = re.compile(r"\bVCP\s+(?:0x)?10\s+\S+\s+(\d+)\s+(\d+)", re.I)
    # ddcutil 1.x and any non-terse output.
    _VCP_VERBOSE_RE = re.compile(
        r"current value\s*=\s*(\d+).*?max value\s*=\s*(\d+)", re.I | re.S
    )

    @classmethod
    def _parse_detect(cls, text: str) -> list:
        """Pull (bus, connector, monitor) out of `ddcutil detect --terse`.

        Parsed rather than exit-code checked on purpose: ddcutil exits 0 even
        when it finds nothing at all, and its permission diagnostics share the
        left margin with the display blocks.
        """
        displays: list = []
        current = None
        for line in (text or "").splitlines():
            if not line.strip():
                continue
            if not line[0].isspace():
                match = cls._DISPLAY_RE.match(line.strip())
                current = None
                if match:
                    current = {
                        "display": match.group(1) or "",
                        "bus": None,
                        "connector": "",
                        "monitor": "",
                    }
                    displays.append(current)
                continue
            if current is None:
                continue
            key, _, value = line.strip().partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "i2c bus":
                bus = cls._BUS_RE.search(value)
                if bus:
                    current["bus"] = bus.group(1)
            elif key == "drm connector":
                current["connector"] = value
            elif key == "monitor":
                current["monitor"] = value
        return [d for d in displays if d["bus"]]

    def _exercise_ddc(self, ctx: RunContext) -> list:
        res = ctx.cmd(
            ["ddcutil", "detect", "--terse"],
            timeout=self.DDC_TIMEOUT,
            artifact=self.DETECT_LOG,
        )
        displays = self._parse_detect(res.stdout)
        if not displays:
            return [{
                "device": ", ".join(self._connected_outputs()) or "display",
                "interface": "ddc",
                "result": "unsupported",
                "note": "no display answered DDC/CI",
            }]
        return [self._exercise_ddc_display(ctx, d) for d in displays]

    def _exercise_ddc_display(self, ctx: RunContext, display: dict) -> dict:
        bus = display["bus"]
        info = {
            "device": display["connector"] or ("i2c-%s" % bus),
            "interface": "ddc",
            "i2c_bus": bus,
            "monitor": display["monitor"],
            "result": "unknown",
        }
        reading = self._ddc_get(ctx, bus)
        if reading is None:
            info["result"] = "unsupported"
            info["note"] = "brightness (VCP 0x10) is not reported over DDC/CI"
            return info
        original, maximum = reading
        info["max_brightness"] = maximum
        info["original"] = original
        if maximum <= 1:
            info["result"] = "not adjustable"
            info["note"] = "reported brightness range is %d" % maximum
            return info
        info["target"] = target = self._pick_target(original, maximum)

        if not self._ddc_set(ctx, bus, target):
            info["result"] = "write refused"
            info["error"] = "ddcutil setvcp %s %d failed" % (self.DDC_FEATURE, target)
            return info
        try:
            def read():
                got = self._ddc_get(ctx, bus)
                return got[0] if got else -1

            samples = self._watch(read, self.DDC_SAMPLES,
                                  window=self.DDC_SETTLE_SECONDS)
            info["samples"] = samples
            info["observed"] = samples[-1]
            # Monitors are free to quantize what they accept, so this is
            # looser than the sysfs tolerance: a panel that steps in fives is
            # still under system control.
            info["result"] = self._classify(
                samples, target, original, original, max(2, maximum // 20)
            )
            return info
        finally:
            if self._ddc_set(ctx, bus, original):
                info["restored"] = True
            else:
                info["restored"] = False
                ctx.log(
                    "warning: could not restore DDC/CI brightness on "
                    "/dev/i2c-%s to %d" % (bus, original)
                )

    def _ddc_get(self, ctx: RunContext, bus: str):
        res = ctx.cmd(
            ["ddcutil", "--bus", bus, "getvcp", self.DDC_FEATURE, "--terse"],
            timeout=self.DDC_TIMEOUT,
        )
        text = "%s\n%s" % (res.stdout, res.stderr)
        for pattern in (self._VCP_TERSE_RE, self._VCP_VERBOSE_RE):
            match = pattern.search(text)
            if match:
                try:
                    return int(match.group(1)), int(match.group(2))
                except ValueError:
                    return None
        return None

    def _ddc_set(self, ctx: RunContext, bus: str, value: int) -> bool:
        return ctx.cmd(
            ["ddcutil", "--bus", bus, "setvcp", self.DDC_FEATURE, str(value)],
            timeout=self.DDC_TIMEOUT,
        ).ok


REGISTRY.register(BacklightControl)
