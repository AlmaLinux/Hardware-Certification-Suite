"""Kernel health: taint flags and a dmesg error scan."""

from __future__ import annotations

import os
import re
from typing import List, Tuple

from .. import procutil
from ..registry import REGISTRY, RunContext, Test
from ..result import Severity, Status

# /proc/sys/kernel/tainted bit meanings (subset that matters here)
# /proc/sys/kernel/tainted bits: (letter, description, weight).
#
#   "fault"   - an unambiguous hardware or kernel failure; fails the run
#   "notable" - not a failure, but a reviewer should see it
#   "routine" - packaging or firmware reality; recorded only
#
# Only genuine faults gate. A tainted kernel usually says something about
# software packaging or firmware quirks rather than about the hardware:
#
#   I (11) the kernel worked *around* a firmware bug - the system works, and
#          this is set on a great deal of perfectly good hardware
#   X (16) the distro's own auxiliary taint; AlmaLinux and RHEL use it to mark
#          unsupported or Technology Preview modules
#   C (10) a staging driver was loaded - a packaging decision
#   P/O/E  proprietary, out-of-tree, or unsigned modules, e.g. a GPU driver
#   W  (9) a kernel warning fired. Non-gating on purpose: validate.kernel.
#          dmesg scans the log with an allowlist and can tell a benign driver
#          grumble from a real problem, which a single taint bit cannot.
#   S  (2) "out of spec" is ambiguous. RHEL-family kernels have long used this
#          bit via mark_hardware_unsupported() to flag hardware the vendor
#          does not *support*, which is a policy statement, not a malfunction;
#          it is also set for genuinely out-of-spec CPU conditions. Machines
#          carrying it frequently work fine, and the bit itself carries no
#          detail, so it is surfaced for review with the kernel's own log
#          message rather than used to fail a run outright.
TAINT_BITS = {
    0: ("P", "proprietary module loaded", "routine"),
    1: ("F", "module force-loaded", "routine"),
    2: ("S", "kernel considers this hardware out of spec or unsupported",
        "notable"),
    3: ("R", "module force-unloaded", "routine"),
    4: ("M", "processor reported a machine check exception", "fault"),
    5: ("B", "bad page referenced", "fault"),
    6: ("U", "user-requested taint", "routine"),
    7: ("D", "kernel died (OOPS/BUG)", "fault"),
    8: ("A", "ACPI table overridden", "routine"),
    9: ("W", "kernel issued a warning", "routine"),
    10: ("C", "staging driver loaded", "routine"),
    11: ("I", "platform firmware workaround applied", "routine"),
    12: ("O", "out-of-tree module loaded", "routine"),
    13: ("E", "unsigned module loaded", "routine"),
    14: ("L", "soft lockup occurred", "fault"),
    15: ("K", "kernel live-patched", "routine"),
    16: ("X", "distribution auxiliary taint (unsupported module)", "routine"),
    17: ("T", "built with the struct randomization plugin", "routine"),
    18: ("N", "an in-kernel test has been run", "routine"),
}

# Lines the kernel logs when it taints, which is where the actual reason
# lives - the bit on its own explains nothing.
TAINT_EVIDENCE = re.compile(
    r"not supported by|unsupported|out of spec|taint|"
    r"disabling lock debugging|SMP kernel|microcode",
    re.I,
)


def describe_taint(value: int) -> dict:
    """Split a taint value into faults, things worth a look, and noise."""
    flags, faults, notable = [], [], []
    for bit in range(64):
        if not value & (1 << bit):
            continue
        letter, description, weight = TAINT_BITS.get(
            bit, ("?", "undocumented taint bit %d" % bit, "routine")
        )
        entry = {"bit": bit, "letter": letter, "description": description,
                 "weight": weight}
        flags.append(entry)
        if weight == "fault":
            faults.append(entry)
        elif weight == "notable":
            notable.append(entry)
    return {
        "tainted": value,
        # the letter string as the kernel documents it, e.g. "P I X"
        "letters": " ".join(f["letter"] for f in flags),
        "flags": [f["description"] for f in flags],
        "faults": [f["description"] for f in faults],
        "notable": [n["description"] for n in notable],
    }


class TaintCheck(Test):
    """Fail only on taint bits that mean a hardware or kernel failure.

    An unmodified distribution kernel is routinely tainted - firmware
    workarounds, staging drivers, a vendor GPU module, or AlmaLinux's own
    unsupported-module marker all set bits without saying anything bad about
    the hardware. Where a bit does warrant attention, the kernel's own log
    message is captured with it, because the bit alone explains nothing.
    """

    id = "validate.kernel.taint"
    category = "kernel"
    run_type = "validate"
    severity = Severity.REQUIRED
    default_timeout = 60

    def _evidence(self, ctx: RunContext) -> list:
        """The kernel's own words about why it tainted."""
        try:
            res = ctx.cmd(["dmesg"], timeout=30, artifact="taint-dmesg.log")
        except Exception:
            return []
        if not getattr(res, "ok", False):
            return []
        return [
            line.strip()
            for line in res.stdout.splitlines()
            if TAINT_EVIDENCE.search(line)
        ][:20]

    def run(self, ctx: RunContext):
        raw = procutil.read_file("/proc/sys/kernel/tainted", "0")
        try:
            value = int(raw or 0)
        except ValueError:
            return self.result(
                Status.ERROR, reason="could not read /proc/sys/kernel/tainted"
            )
        details = describe_taint(value)
        if value:
            details["kernel_log"] = self._evidence(ctx)

        if details["faults"]:
            return self.result(
                Status.FAIL,
                reason="kernel tainted by a fault: %s (tainted=%d, %s)"
                % (", ".join(details["faults"]), value, details["letters"]),
                details=details,
            )
        if details["notable"]:
            return self.result(
                Status.PASS,
                reason="worth a look: %s (tainted=%d, %s)%s"
                % (
                    ", ".join(details["notable"]),
                    value,
                    details["letters"],
                    _first_evidence(details.get("kernel_log")),
                ),
                details=details,
            )
        if details["flags"]:
            return self.result(
                Status.PASS,
                reason="tainted (%s) but not by anything hardware-related: %s"
                % (details["letters"], ", ".join(details["flags"])),
                details=details,
            )
        return self.result(Status.PASS, details=details)


def _first_evidence(lines) -> str:
    return " - kernel said: %s" % lines[0] if lines else ""


def load_patterns(path: str) -> Tuple[List[re.Pattern], List[re.Pattern]]:
    deny: List[re.Pattern] = []
    allow: List[re.Pattern] = []
    section = None
    content = procutil.read_file(path, "") or ""
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line == "[deny]":
            section = deny
            continue
        if line == "[allow]":
            section = allow
            continue
        if section is not None:
            try:
                section.append(re.compile(line, re.I))
            except re.error:
                pass
    return deny, allow


class DmesgScan(Test):
    id = "validate.kernel.dmesg"
    category = "kernel"
    run_type = "validate"
    severity = Severity.REQUIRED
    default_timeout = 60

    def run(self, ctx: RunContext):
        res = ctx.cmd(
            ["dmesg", "--level=emerg,alert,crit,err"],
            timeout=30,
            artifact="dmesg-errors.log",
        )
        if not res.ok:
            return self.result(Status.ERROR, reason="dmesg failed")
        patterns_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data",
            "dmesg-patterns.conf",
        )
        deny, allow = load_patterns(patterns_path)
        hits: List[str] = []
        for line in res.stdout.splitlines():
            if any(p.search(line) for p in allow):
                continue
            if any(p.search(line) for p in deny):
                hits.append(line.strip())
        artifacts = [ctx.rel_artifact("dmesg-errors.log")]
        if hits:
            return self.result(
                Status.FAIL,
                reason="%d kernel error message(s) matched failure patterns" % len(hits),
                details={"matches": hits[:25]},
                artifacts=artifacts,
            )
        return self.result(
            Status.PASS,
            details={"error_lines_reviewed": len(res.stdout.splitlines())},
            artifacts=artifacts,
        )


REGISTRY.register(TaintCheck)
REGISTRY.register(DmesgScan)
