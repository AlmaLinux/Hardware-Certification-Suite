"""Hardware-presence predicates used by tests' applicable() checks."""

from __future__ import annotations

import glob
import os
import re
from typing import Any, Dict, List

from . import procutil


def cpu_virt_flag() -> str:
    """Return 'vmx', 'svm', or ''. """
    content = procutil.read_file("/proc/cpuinfo", "") or ""
    for flag in ("vmx", "svm"):
        if " %s" % flag in content or "\t%s" % flag in content:
            return flag
    return ""


def physical_nics(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [n for n in summary.get("nics", []) if n.get("name") != "lo"]


def gpus(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    return summary.get("gpus", [])


# PCI vendor ids. The GPU collector already keeps this pair for the same purpose: deciding which
# *tool* can answer about a card, which is a fact about the ids rather than a judgement about what
# the part is called. The naming stays the server's.
NVIDIA_VENDOR_ID = "10de"
AMD_VENDOR_ID = "1002"
_VENDOR_ID_RE = re.compile(r"\[([0-9a-f]{4})\]\s*$")


def gpu_vendor_id(gpu: Dict[str, Any]) -> str:
    """The PCI vendor id of a reported GPU, lowercase, or "".

    Read out of ``pci_ids.vendor``, which the collector records verbatim as lspci printed it:
    ``"NVIDIA Corporation [10de]"``. There is no flattened ``vendor`` token on a GPU entry any
    more, and assuming there was is a mistake this project has now made three times. The first
    was the GPU collector itself, the second was the NIC collector, which shipped a catalog
    component called "Device", and the third skipped every NVIDIA test on a machine with an
    NVIDIA card in it while reporting "no NVIDIA GPU detected".

    The legacy shape is still accepted, because a test may run against a summary from an older
    collector and a one-line fallback is cheaper than a second way to be wrong.
    """
    ids = gpu.get("pci_ids") or {}
    match = _VENDOR_ID_RE.search(ids.get("vendor") or "")
    if match:
        return match.group(1).lower()
    legacy = (gpu.get("vendor") or "").strip().lower()
    return {"nvidia": NVIDIA_VENDOR_ID, "amd": AMD_VENDOR_ID}.get(legacy, "")


def gpu_name(gpu: Dict[str, Any]) -> str:
    """A card's name for a message, from whatever the collector recorded."""
    ids = gpu.get("pci_ids") or {}
    return gpu.get("smi_name") or ids.get("device") or "an unnamed GPU"


# PCI vendor ids of baseboard-management display adapters - the VGA console chip on nearly every
# server (ASPEED AST, Matrox G200). They enumerate as display-class (03xx) PCI devices with a bound
# in-kernel driver (``ast``, ``mgag200``), so they look like GPUs to anything that only asks "class
# 03xx with a driver", but they are not accelerators: nothing benchmarks on them. Matched by id
# because the collector records the vendor inside ``pci_ids.vendor`` ("ASPEED Technology, Inc.
# [1a03]"), and a substring match on a driver named ``ast`` also matches "last" and "broadcast".
MANAGEMENT_ADAPTER_VENDOR_IDS = frozenset({"1a03", "102b"})  # ASPEED, Matrox
# The same two as the flattened token an older collector wrote, for a bundle predating ``pci_ids``.
_MANAGEMENT_ADAPTER_NAMES = frozenset({"aspeed", "matrox"})


def is_management_adapter(gpu: Dict[str, Any]) -> bool:
    """Whether a reported GPU entry is a baseboard-management display adapter, not an accelerator.

    Matches on the PCI vendor id read from ``pci_ids.vendor`` (current collectors), falling back to
    the legacy flattened ``vendor`` token so a stored older bundle is read the same way.
    """
    if gpu_vendor_id(gpu) in MANAGEMENT_ADAPTER_VENDOR_IDS:
        return True
    return (gpu.get("vendor") or "").strip().lower() in _MANAGEMENT_ADAPTER_NAMES


def accelerator_gpus(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The reported GPUs that are actual accelerators: every enumerated display-class device minus
    the baseboard-management adapters. This is the set a GPU benchmark or driver check should
    consider - a machine whose only "GPU" is its BMC console has nothing to accelerate.
    """
    return [gpu for gpu in gpus(summary) if not is_management_adapter(gpu)]


def bound_gpus(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The GPUs the kernel has actually bound a driver to.

    The collector records ``driver`` from lspci's "Kernel driver in use", or ``None`` when the
    line is absent. A card the running kernel has no driver for is still enumerated (it is on the
    bus), so presence in the inventory is not the same as being usable, and this is the difference.

    Management adapters are *not* filtered here: the GPU-API validation, which shares this, treats a
    BMC console as a display device the ICD gate may still probe. The stricter benchmark question -
    is there an accelerator to benchmark - is ``accelerator_skip_reason``.
    """
    return [gpu for gpu in gpus(summary) if gpu.get("driver")]


def _collected_kernel(summary: Dict[str, Any]) -> str:
    """The kernel the bundle was collected on, not the one analyzing it.

    ``summary['drivers']['kernel']`` is recorded at collection time, so it is right even when the
    reason is built by re-analyzing a stored bundle on another machine (the results path). Falling
    back to the live kernel only when a summary carries none keeps older bundles working.
    """
    return (summary.get("drivers") or {}).get("kernel") or os.uname().release


def no_driver_reason(summary: Dict[str, Any]):
    """Why the GPU tests cannot run when no card has a bound driver, or ``None`` when one does.

    ``None`` as soon as any GPU is driver-bound: there is a card to test, so this is not the reason
    to skip. Otherwise it names the cause, and the cause worth naming is an AMD card that is present
    but that no kernel driver has bound. "no GPU with an identified driver" reads as "no card",
    which is the opposite of the truth for a Radeon that lspci lists and nothing claims: the part is
    there, unclaimed, so there is no render node and no compute device for the tests to reach.

    It stops there and does not diagnose *why* amdgpu did not bind it, or prescribe a release. This
    function knows only the inventory, and a card the running kernel supports binds without help, so
    an unbound one points at driver support - a matter of which kernel or amdgpu build claims the
    model - rather than anything a reinstall or reboot of the same kernel changes. Earlier versions
    of this message asserted which AlmaLinux releases carry the id; that was read off container
    packages and real hardware contradicted it, so the specifics are gone and only what the
    inventory shows remains.
    """
    if bound_gpus(summary):
        return None
    present = gpus(summary)
    if not present:
        return "no GPU detected in the inventory"
    amd_unbound = [
        gpu for gpu in present
        if gpu_vendor_id(gpu) == AMD_VENDOR_ID and not gpu.get("driver")
    ]
    if amd_unbound:
        names = ", ".join(gpu_name(gpu) for gpu in amd_unbound)
        return (
            "an AMD GPU (%s) is present but no kernel driver is bound to it, so it cannot be used "
            "here. The GPU tests need the card claimed by amdgpu, and a card the running kernel "
            "(%s) supports binds on its own, so an unbound one means this kernel's amdgpu is not "
            "claiming this model. Driving it needs a kernel or amdgpu build that does - a newer or "
            "out-of-tree amdgpu, such as amdgpu-dkms - or the bind forced by hand. No package "
            "installed here, and no reboot of this kernel, changes which driver binds it."
            % (names, _collected_kernel(summary))
        )
    return "no GPU has a driver bound, so there is no card the GPU tests can use"


def accelerator_skip_reason(summary: Dict[str, Any]):
    """Why a GPU *benchmark* has no accelerator to run on, or ``None`` when a driver-bound one is
    present.

    Stricter than ``no_driver_reason``, which the GPU-API validation shares under a different policy
    ("the ICD decides which display device to probe"): a benchmark on a baseboard-management adapter
    measures the CPU software rasterizer, so a machine whose only display device is a BMC console
    (ASPEED/Matrox) is skipped by name here even though that adapter has a bound driver - the case
    that made clpeak fire on a headless server and then fail against ``llvmpipe``/``lavapipe``.
    """
    accelerators = accelerator_gpus(summary)
    if any(gpu.get("driver") for gpu in accelerators):
        return None
    present = gpus(summary)
    if present and not accelerators:
        return (
            "the only display device is a baseboard-management adapter (%s), which drives the VGA "
            "console and is not an accelerator, so there is no GPU to benchmark here"
            % ", ".join(gpu_name(gpu) for gpu in present)
        )
    # An accelerator is present but unbound, or nothing is: defer to the shared reason (the "no GPU
    # at all", unbound-AMD-with-its-kernel-story, and generic-unbound cases), computed on the
    # accelerators alone so a BMC sitting beside an unbound card is not mistaken for a bound GPU.
    return no_driver_reason({**summary, "gpus": accelerators})


def has_edac() -> bool:
    return bool(glob.glob("/sys/devices/system/edac/mc/mc*"))


def nvme_devices(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [d for d in summary.get("disks", []) if d.get("transport") == "nvme"]


def thermal_zones() -> List[str]:
    return sorted(glob.glob("/sys/class/thermal/thermal_zone*"))


def watchdog_devices() -> List[str]:
    return sorted(glob.glob("/sys/class/watchdog/watchdog*"))


def cpufreq_available() -> bool:
    return os.path.isdir("/sys/devices/system/cpu/cpu0/cpufreq")


def is_efi() -> bool:
    return os.path.isdir("/sys/firmware/efi")
