"""Whether this machine is a virtual guest, as facts rather than as a verdict.

Certification is about hardware, and a full system validation inside a virtual machine proves things
about a hypervisor's emulation rather than about a board: the firmware is the hypervisor's, the
storage controller may be virtio, the NIC may be a paravirtual device, and the CPU is whatever the
host chose to expose. A **GPU** is the exception that matters, because a passed-through card is the
real device, which is what makes validating one in a cloud instance worth doing at all.

So the suite needs to know where it is running, and it needs to record it either way. Two consumers,
and they are deliberately different:

- **The submission gate** reads one derived answer, "may a full run be submitted from here", from
  the policy table below. This is a local decision with a precedent: the unsupported-OS gate
  refuses a submission the same way, for the same reason, and offers the same kind of override.
- **The report** carries every raw signal, with no conclusion attached. Lumina re-derives its own
  answer server-side from the same facts, because a client that can be edited cannot be the
  authority on whether its own evidence counts, and because a policy that turns out to be wrong
  should be fixable in the web application rather than by shipping a new suite to every partner.

**Detection is per-architecture, which is why systemd does it.** The x86 CPUID hypervisor flag does
not exist on aarch64; s390x announces its control program in ``/proc/sysinfo``; ppc64le has
``/proc/ppc64/lparcfg`` and the device tree. ``systemd-detect-virt`` knows all of that already and
ships with systemd, so it is on every machine this suite supports. The arch-specific files are
recorded beside its answer as corroboration, not as a reimplementation of it.
"""

from __future__ import annotations

import os
import platform
from typing import Any, Dict, List, Optional

from . import procutil

# Virtualization types a **full system validation** may still be submitted from.
#
# A policy table, named and separate from the detection, for two reasons. The first is that this is
# the part most likely to be wrong: on ppc64le, Linux normally runs in a PowerVM LPAR, and on s390x
# everything runs in a partition, so a rule that refuses "anything virtualized" would make hardware
# on those architectures impossible to certify. The second is that lumina holds the same table, and
# a table is something two codebases can be checked against each other on.
#
# ``none`` is bare metal, which includes a bare-metal cloud instance: those report no virtualization
# even though the vendor calls them instances.
#
# ``powervm`` is here at the maintainer's direction, and it is why this is a table. On ppc64le
# Linux normally runs in a PowerVM LPAR, which ``systemd-detect-virt`` reports as ``powervm``, so a
# rule that refused every kind of virtualization would make Power hardware impossible to certify at
# all. An LPAR is the platform's own partitioning rather than an emulated machine: the firmware, the
# adapters, and the CPU are the real ones.
#
# Deliberately *not* here: ``zvm`` and ``kvm`` on s390x. A plain s390x LPAR reports ``none`` and is
# already allowed; z/VM and KVM are hypervisors above that, so a guest of either is not the machine.
FULL_RUN_TYPES = ("none", "powervm")

# Types where only a scoped component claim makes sense. Everything not in ``FULL_RUN_TYPES`` is
# treated this way; this list exists so the message can name what was found.
_KNOWN_VM_TYPES = (
    "kvm", "qemu", "vmware", "microsoft", "oracle", "xen", "bochs", "uml", "parallels",
    "bhyve", "qnx", "acrn", "apple", "amazon", "powervm", "zvm",
)

_DMI_FIELDS = ("sys_vendor", "product_name", "product_family", "bios_vendor", "chassis_vendor")


def detect() -> Dict[str, Any]:
    """Every signal about virtualization this machine offers, and no interpretation of them.

    Shaped for the report. A reader months later, or a reviewer deciding whether to release a held
    run, gets the raw answers rather than this suite's opinion of them.
    """
    facts: Dict[str, Any] = {
        "type": None,
        "container": None,
        "detector": None,
        "signals": {},
    }
    tool = procutil.find_tool("systemd-detect-virt")
    if tool is not None:
        vm = _run(tool, "--vm")
        container = _run(tool, "--container")
        facts["signals"]["systemd_detect_virt"] = {"vm": vm, "container": container}
        # ``none`` and a non-zero exit both mean "nothing detected". The word is what gets
        # recorded, because an exit code alone would not survive being read by a person.
        if vm and vm != "none":
            facts["type"] = vm
            facts["detector"] = "systemd-detect-virt"
        if container and container != "none":
            facts["container"] = container
    facts["signals"]["cpu_hypervisor_flag"] = _cpu_hypervisor_flag()
    facts["signals"]["sys_hypervisor_type"] = procutil.read_file(
        "/sys/hypervisor/type", ""
    ).strip() or None
    facts["signals"]["dmi"] = _dmi()
    facts["signals"]["s390x_control_program"] = _s390x_control_program()
    facts["signals"]["ppc64_lparcfg"] = os.path.exists("/proc/ppc64/lparcfg")
    facts["signals"]["container_markers"] = _container_markers()
    facts["signals"]["arch"] = platform.machine()

    if facts["type"] is None and facts["detector"] is None:
        # No systemd-detect-virt, so fall back to the corroborating signals. Deliberately last and
        # deliberately coarse: it answers "something is virtualizing this" without guessing which,
        # because guessing a wrong type is worse than reporting an unnamed one.
        fallback = _fallback_type(facts["signals"])
        if fallback:
            facts["type"] = fallback
            facts["detector"] = "fallback signals"
    if facts["type"] is None:
        facts["type"] = "none"
        facts["detector"] = facts["detector"] or (
            "systemd-detect-virt" if tool is not None else "fallback signals"
        )
    return facts


def _run(tool: str, flag: str) -> Optional[str]:
    """One ``systemd-detect-virt`` question. Its word, or None if it could not be asked.

    A non-zero exit is how it says "nothing detected", so the return code is not an error here and
    the output is what matters.
    """
    try:
        res = procutil.run_cmd([tool, flag], timeout=30)
    except procutil.CommandNotFound:
        return None
    if res.timed_out:
        return None
    return (res.stdout or "").strip() or None


def _cpu_hypervisor_flag() -> Optional[bool]:
    """Whether the CPU advertises the hypervisor bit. None where the concept does not exist.

    x86 only. aarch64, ppc64le, and s390x have no such flag, and reporting False there would read
    as "this is bare metal" when the truth is "this signal does not apply".
    """
    if platform.machine() not in ("x86_64", "i686", "i386"):
        return None
    for line in (procutil.read_file("/proc/cpuinfo", "") or "").splitlines():
        if line.startswith("flags"):
            return "hypervisor" in line.split()
    return None


def _dmi() -> Dict[str, Optional[str]]:
    """The firmware's own idea of what this machine is.

    Recorded rather than judged. ``Amazon EC2`` appears on both an instance and a bare-metal
    instance, so this corroborates and never decides.
    """
    found = {}
    for field in _DMI_FIELDS:
        value = procutil.read_file("/sys/class/dmi/id/%s" % field, "")
        found[field] = (value or "").strip() or None
    return found


def _s390x_control_program() -> Optional[str]:
    """What is running this partition, on s390x. None elsewhere.

    ``/proc/sysinfo`` names the control program of each nesting level: a plain LPAR has none, a z/VM
    guest says so, and a KVM guest says KVM.
    """
    text = procutil.read_file("/proc/sysinfo", "")
    if not text:
        return None
    for line in text.splitlines():
        if "Control Program:" in line:
            return line.split(":", 1)[1].strip() or None
    return None


def _container_markers() -> List[str]:
    """The container marker files present, if any.

    The same list ``gpusetup`` uses, for the same reason, and recorded here so the report says why a
    run in a container was treated as one.
    """
    return [
        marker for marker in ("/run/.containerenv", "/.dockerenv", "/run/systemd/container")
        if os.path.exists(marker)
    ]


def _fallback_type(signals: Dict[str, Any]) -> Optional[str]:
    """A coarse "something is virtualizing this" from the corroborating signals only."""
    if signals.get("sys_hypervisor_type"):
        return signals["sys_hypervisor_type"]
    if signals.get("cpu_hypervisor_flag"):
        return "unknown-hypervisor"
    program = signals.get("s390x_control_program")
    if program:
        return program.split()[0].lower()
    if signals.get("ppc64_lparcfg"):
        return "powervm"
    vendor = (signals.get("dmi") or {}).get("sys_vendor") or ""
    for needle, name in (
        ("QEMU", "qemu"), ("VMware", "vmware"), ("Microsoft", "microsoft"),
        ("Xen", "xen"), ("innotek", "oracle"), ("Parallels", "parallels"),
    ):
        if needle.lower() in vendor.lower():
            return name
    return None


# --- what it means for a submission ------------------------------------------------


def full_run_reason(facts: Dict[str, Any]) -> Optional[str]:
    """Why a full system validation from here cannot be submitted, or None if it can.

    One sentence, naming what was found and what to do instead, because this is what an operator
    sees after a run they may have waited an hour for.
    """
    if facts.get("container"):
        return (
            "this is a %s container, so a full system validation would be about the host's "
            "kernel and the container's view of its devices rather than about hardware"
            % facts["container"]
        )
    kind = facts.get("type") or "none"
    if kind in FULL_RUN_TYPES:
        return None
    named = kind if kind in _KNOWN_VM_TYPES else "a virtual machine (%s)" % kind
    return (
        "this is %s, and a full system validation there proves things about the hypervisor's "
        "emulation rather than about a board: the firmware, the storage controller, and the "
        "network device are all the hypervisor's. A GPU passed through to a guest is the real "
        "device, so "
        "'--scope gpu' is submittable from here and a whole-machine claim is not" % named
    )


def summary(facts: Dict[str, Any]) -> str:
    """One short phrase for a log line or a table cell."""
    if facts.get("container"):
        return "container (%s)" % facts["container"]
    kind = facts.get("type") or "unknown"
    return "bare metal" if kind == "none" else kind
