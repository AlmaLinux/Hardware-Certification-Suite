"""IPMI/BMC checks: is the management controller reachable, and what does it
say about the chassis?

Only the **in-band** host interface is tested. Out-of-band access (IPMI over
LAN, or Redfish on anything modern) needs the BMC's own credentials, which the
suite has no business holding, and which would test the network path rather
than the machine under certification.

The BMC's network configuration is deliberately never recorded. A management
interface address is a live attack surface and these reports get published, so
`ipmitool lan print` is not run at all - there is nothing to leak if it is
never collected.

Applicability comes from SMBIOS type 38 (IPMI Device Information), so desktops,
laptops, and cloud instances skip without probing anything. Where SMBIOS is
silent but the kernel found an IPMI interface anyway, that counts too.
"""

from __future__ import annotations

import glob
import re
from typing import Any, Dict, List, Optional

from .. import procutil
from ..registry import REGISTRY, RunContext, Test
from ..result import Severity, Status

# Loaded only once SMBIOS has declared a BMC, so this never probes hardware
# that has none. All three are in-tree, so loading them does not taint.
IPMI_MODULES = ("ipmi_si", "ipmi_ssif", "ipmi_devintf")


def _ipmi_devices() -> List[str]:
    devices: List[str] = []
    for pattern in ("/dev/ipmi*", "/dev/ipmi/*", "/dev/ipmidev/*"):
        devices.extend(glob.glob(pattern))
    return sorted(devices)


def _ipmi_sysfs() -> List[str]:
    """The kernel's IPMI class, which exists when a driver bound even if no device node was made.

    Its own function rather than a glob inline in ``_bmc_declared``, because it is the second half
    of "does this machine have a BMC" and a test that wants to describe a machine without one has
    to be able to say so about both halves. Inline, it was not stubbable, and the suite's own
    package build failed on a builder whose /sys/class/ipmi was populated.
    """
    return sorted(glob.glob("/sys/class/ipmi/*"))


def _bmc_declared(ctx: RunContext) -> bool:
    if (ctx.summary.get("bmc") or {}).get("present"):
        return True
    # A BMC that SMBIOS did not declare but the kernel bound anyway.
    return bool(_ipmi_devices() or _ipmi_sysfs())


def _parse_keyvals(text: str) -> Dict[str, str]:
    """Parse ipmitool's "Key : Value" output.

    Indented continuation lines (the "Additional Device Support" list) are
    skipped: they carry no key of their own.
    """
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        if not line.strip() or line[:1].isspace() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key:
            out[key] = value
    return out


class IpmiTest(Test):
    """Shared applicability and module handling for the IPMI tests."""

    category = "ipmi"
    run_type = "validate"
    packages = ("ipmitool",)          # AppStream on AlmaLinux 8, 9, and 10

    def applicable(self, ctx: RunContext) -> Optional[str]:
        if not _bmc_declared(ctx):
            return (
                "no IPMI device: SMBIOS declares no BMC and the kernel exposes "
                "no IPMI interface"
            )
        return None

    def setup(self, ctx: RunContext) -> None:
        self._modprobe: Dict[str, str] = {}
        if _ipmi_devices() or not procutil.which("modprobe"):
            return
        for module in IPMI_MODULES:
            res = procutil.run_cmd(["modprobe", module], timeout=60)
            self._modprobe[module] = "loaded" if res.ok else (
                (res.stderr or "").strip() or "modprobe failed"
            )
            if _ipmi_devices() and module != "ipmi_devintf":
                # A system interface bound; still need the char device.
                continue

    def _no_device_reason(self) -> str:
        """Why the in-band interface never came up.

        This is a skip rather than a failure on purpose. SMBIOS type 38 is
        occasionally stale, and a BMC can be disabled in firmware while still
        being advertised, so a machine is not condemned for it. The modprobe
        errors go in the reason so a reviewer sees the real cause on the run
        page instead of having to open artifacts.
        """
        detail = "; ".join(
            "%s: %s" % (mod, why)
            for mod, why in sorted(getattr(self, "_modprobe", {}).items())
            if why != "loaded"
        )
        return (
            "SMBIOS declares a BMC but no in-band IPMI device appeared, so it "
            "could not be queried%s" % ((" (%s)" % detail) if detail else "")
        )


class BmcReachable(IpmiTest):
    """Does the BMC answer over the host interface, and who is it?

    Failing means the interface exists and the controller behind it does not
    answer, which is a real defect. The interface not coming up at all is a
    skip: see _no_device_reason.
    """

    id = "validate.ipmi.bmc"
    severity = Severity.CONDITIONAL
    default_timeout = 240

    def run(self, ctx: RunContext):
        devices = _ipmi_devices()
        details: Dict[str, Any] = {
            "devices": devices,
            "modules": getattr(self, "_modprobe", {}),
            "smbios": ctx.summary.get("bmc") or {},
        }
        if not devices:
            return self.result(
                Status.SKIP, reason=self._no_device_reason(), details=details
            )

        res = ctx.cmd(["ipmitool", "mc", "info"], timeout=120,
                      artifact="ipmitool-mc-info.log")
        info = _parse_keyvals(res.stdout)
        details["mc_info"] = info
        artifacts = [ctx.rel_artifact("ipmitool-mc-info.log")]

        firmware = info.get("Firmware Revision")
        if not res.ok or not firmware:
            return self.result(
                Status.FAIL,
                reason=(
                    "the IPMI host interface is present (%s) but the BMC did "
                    "not answer `ipmitool mc info`%s"
                    % (", ".join(devices),
                       (": %s" % (res.stderr or "").strip().splitlines()[0])
                       if (res.stderr or "").strip() else "")
                ),
                details=details,
                artifacts=artifacts,
            )

        # Chassis status is recorded here for provenance; its fault flags are
        # gated by validate.ipmi.health, which is where chassis health belongs.
        chassis = ctx.cmd(["ipmitool", "chassis", "status"], timeout=60)
        if chassis.ok:
            details["chassis"] = _parse_keyvals(chassis.stdout)

        available = (info.get("Device Available") or "").lower()
        if available and available != "yes":
            return self.result(
                Status.FAIL,
                reason="the BMC reports itself unavailable (Device Available: "
                       "%s)" % info.get("Device Available"),
                details=details,
                artifacts=artifacts,
            )
        return self.result(
            Status.PASS,
            reason="%s BMC, firmware %s, IPMI %s" % (
                info.get("Manufacturer Name") or "unidentified",
                firmware,
                info.get("IPMI Version") or "version unreported",
            ),
            details=details,
            artifacts=artifacts,
        )


# ipmitool's sdr state column. "ns" covers sensors that are present but have
# no reading right now, which is normal for unpopulated fan headers and empty
# PSU bays, so it never gates.
SENSOR_STATES = {
    "ok": "ok",
    "ns": "no reading",
    "nc": "non-critical",
    "cr": "critical",
    "nr": "non-recoverable",
}
GATING_STATES = ("critical", "non-recoverable")

# Chassis fault flags worth failing on. Deliberately excludes Chassis
# Intrusion, which is asserted on any machine whose case has ever been opened
# and is therefore true of most test hardware.
CHASSIS_FAULTS = (
    "Power Overload",
    "Main Power Fault",
    "Power Control Fault",
    "Drive Fault",
    "Cooling/Fan Fault",
)


class IpmiHealth(IpmiTest):
    """Chassis health as the BMC reports it: sensor states plus fault flags.

    The disk-SMART check for everything that is not a disk. A fan, PSU, or
    temperature sensor in a critical state is a hardware problem the host OS
    would otherwise never see.
    """

    id = "validate.ipmi.health"
    severity = Severity.CONDITIONAL
    default_timeout = 300

    def run(self, ctx: RunContext):
        if not _ipmi_devices():
            return self.result(Status.SKIP, reason=self._no_device_reason())

        res = ctx.cmd(["ipmitool", "sdr", "elist"], timeout=240,
                      artifact="ipmitool-sdr.log")
        artifacts = [ctx.rel_artifact("ipmitool-sdr.log")]
        sensors = self._parse_sdr(res.stdout)

        by_state: Dict[str, List[Dict[str, str]]] = {}
        for sensor in sensors:
            by_state.setdefault(sensor["state"], []).append(sensor)
        details: Dict[str, Any] = {
            "sensor_count": len(sensors),
            "sensor_states": {k: len(v) for k, v in sorted(by_state.items())},
            # The full listing is the artifact; details carry what matters, so
            # a 200-sensor chassis does not bloat every report.
            "sensors_of_note": [
                s for s in sensors
                if s["state"] not in ("ok", "no reading")
            ][:40],
        }

        faults = []
        chassis = ctx.cmd(["ipmitool", "chassis", "status"], timeout=60)
        if chassis.ok:
            status = _parse_keyvals(chassis.stdout)
            details["chassis"] = status
            faults = [
                flag for flag in CHASSIS_FAULTS
                if (status.get(flag) or "").strip().lower() == "true"
            ]

        if not sensors and not chassis.ok:
            return self.result(
                Status.SKIP,
                reason="the BMC returned no sensor records and no chassis "
                       "status, so there is nothing to check",
                details=details,
                artifacts=artifacts,
            )

        critical = [s for s in sensors if s["state"] in GATING_STATES]
        if critical or faults:
            parts = ["%s: %s (%s)" % (s["name"], s["reading"], s["state"])
                     for s in critical[:10]]
            parts += ["chassis reports %s" % flag for flag in faults]
            return self.result(
                Status.FAIL,
                reason="the BMC reports a hardware fault: %s" % "; ".join(parts),
                details=details,
                artifacts=artifacts,
            )

        warned = by_state.get("non-critical", [])
        reason = "%d sensor(s) read, none critical" % len(sensors)
        if warned:
            # Same treatment as a notable kernel taint bit: surfaced for a
            # reviewer, not a gate. A non-critical threshold is a warning.
            reason += "; worth a look: %s" % "; ".join(
                "%s at %s" % (s["name"], s["reading"]) for s in warned[:5]
            )
        return self.result(
            Status.PASS, reason=reason, details=details, artifacts=artifacts
        )

    @staticmethod
    def _parse_sdr(text: str) -> List[Dict[str, str]]:
        """Parse `sdr elist` (5 columns) or `sdr list` (3 columns).

        elist:  name | id | state | entity | reading
        list:   name | reading | state
        """
        sensors: List[Dict[str, str]] = []
        for line in (text or "").splitlines():
            if "|" not in line:
                continue
            fields = [f.strip() for f in line.split("|")]
            if len(fields) >= 5:
                name, state, reading = fields[0], fields[2], fields[4]
            elif len(fields) == 3:
                name, reading, state = fields
            else:
                continue
            if not name:
                continue
            token = state.lower()
            sensors.append({
                "name": name,
                "reading": reading,
                "state": SENSOR_STATES.get(token, token or "unknown"),
            })
        return sensors


# Events that mean something failed, as opposed to the routine power-on and
# threshold chatter every SEL accumulates. Plain "Correctable ECC" is excluded
# because it is self-healing and ubiquitous; hitting the logging limit is not.
SEL_SERIOUS = re.compile(
    r"uncorrectable|non-recoverable|thermal trip|caterr|ierr|"
    r"predictive failure|logging limit reached|"
    r"power supply (failure|fault)|bus (fatal|uncorrectable) error",
    re.I,
)


class IpmiEventLog(IpmiTest):
    """Read the System Event Log and report what is in it.

    Informational on purpose. The SEL is historical: it survives OS installs
    and often predates the current owner, so an event in it says nothing about
    whether the machine works now. Gating on it would fail good hardware for
    something that happened before the certification run existed, which is the
    same mistake as gating on a kernel taint bit.
    """

    id = "validate.ipmi.sel"
    severity = Severity.INFORMATIONAL
    default_timeout = 300

    def run(self, ctx: RunContext):
        if not _ipmi_devices():
            return self.result(Status.SKIP, reason=self._no_device_reason())

        info = ctx.cmd(["ipmitool", "sel", "info"], timeout=120)
        details: Dict[str, Any] = {}
        if info.ok:
            parsed = _parse_keyvals(info.stdout)
            details["sel_info"] = {
                key: parsed[key] for key in
                ("Entries", "Percent Used", "Overflow", "Last Add Time")
                if key in parsed
            }
        # `sel list` rather than `sel elist`: the extended form resolves every
        # record against the SDR, which takes minutes on a full log.
        listing = ctx.cmd(["ipmitool", "sel", "list"], timeout=240,
                          artifact="ipmitool-sel.log")
        artifacts = [ctx.rel_artifact("ipmitool-sel.log")]
        if not info.ok and not listing.ok:
            return self.result(
                Status.SKIP,
                reason="the BMC does not expose a readable System Event Log",
                details=details,
                artifacts=artifacts,
            )

        entries = [ln.strip() for ln in (listing.stdout or "").splitlines()
                   if "|" in ln]
        serious = [ln for ln in entries if SEL_SERIOUS.search(ln)]
        details["entry_count"] = len(entries)
        details["serious_entries"] = serious[-20:]

        if serious:
            return self.result(
                Status.PASS,
                reason="%d SEL entries, %d worth a look: %s" % (
                    len(entries), len(serious), serious[-1]
                ),
                details=details,
                artifacts=artifacts,
            )
        return self.result(
            Status.PASS,
            reason="%d SEL entries, none indicating a hardware failure"
                   % len(entries),
            details=details,
            artifacts=artifacts,
        )


REGISTRY.register(BmcReachable)
REGISTRY.register(IpmiHealth)
REGISTRY.register(IpmiEventLog)
