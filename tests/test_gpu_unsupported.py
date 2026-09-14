"""A GPU the running kernel has no driver for: say so, and do not blame the card.

The case is a Radeon 780M (gfx1103, RDNA3) on AlmaLinux 9. The 5.14 kernel's in-tree ``amdgpu`` has
no PCI id for it (verified: the id table's newest AMD parts are RDNA2 and CDNA2 Aldebaran), so lspci
lists the card with no "Kernel driver in use" and the collector records ``driver: None``. The card
is present and unusable, and no userspace package and no reboot change that - only a newer kernel
does. The suite used to answer this with "no GPU with an identified driver", which reads as "no
card"; these tests pin the accurate message and the offers that must not fire for such a card.
"""

import os

import pytest

from alma_certify import hwquery
from alma_certify.benchmarks import gpu as gpu_bench
from alma_certify.config import Config
from alma_certify.registry import RunContext
from alma_certify.validate import gpuapi

# The reported card: present, RDNA3, and unclaimed by the el9 kernel.
AMD_UNBOUND = {
    "pci": "c1:00.0",
    "pci_ids": {"vendor": "Advanced Micro Devices, Inc. [AMD/ATI] [1002]",
                "device": "Phoenix1 [Radeon 780M] [15bf]"},
    "driver": None,
}
# The same shape once a new-enough kernel (or an out-of-tree build) binds it.
AMD_BOUND = dict(AMD_UNBOUND, driver="amdgpu")
NVIDIA_BOUND = {
    "pci_ids": {"vendor": "NVIDIA Corporation [10de]", "device": "AD102 [26b9]"},
    "driver": "nvidia",
}
INTEL_UNBOUND = {
    "pci_ids": {"vendor": "Intel Corporation [8086]", "device": "some very new iGPU [aaaa]"},
    "driver": None,
}


class _Pkg:
    """Records what the code asks it to install, so a test can prove nothing was installed."""

    def __init__(self):
        self.ensured = []

    def missing(self, packages, repos=(), timeout=900):
        return list(packages)

    def ensure(self, packages, repos=(), timeout=900):
        self.ensured.append(list(packages))
        return True


# --- the reason itself ----------------------------------------------------------


def test_a_bound_card_is_not_a_reason_to_skip():
    """Any driver-bound GPU means there is a card to test, so no skip reason is produced."""
    assert hwquery.no_driver_reason({"gpus": [AMD_BOUND]}) is None
    assert hwquery.no_driver_reason({"gpus": [NVIDIA_BOUND]}) is None


def test_one_bound_card_among_unbound_ones_still_runs():
    """A usable card is not held back by an unusable one beside it."""
    assert hwquery.no_driver_reason({"gpus": [AMD_UNBOUND, NVIDIA_BOUND]}) is None


def test_no_gpu_at_all_says_so_plainly():
    assert hwquery.no_driver_reason({"gpus": []}) == "no GPU detected in the inventory"


EL9_KERNEL = "5.14.0-687.39.1.el9_8.x86_64"
EL10_KERNEL = "6.12.0-55.9.1.el10_0.x86_64"


def _amd_summary(kernel):
    return {"gpus": [AMD_UNBOUND], "drivers": {"kernel": kernel}}


def test_an_unbound_amd_card_names_the_card_and_the_real_cause():
    reason = hwquery.no_driver_reason(_amd_summary(EL9_KERNEL))

    assert reason is not None
    # The card is named, so it does not read as "no card".
    assert "Radeon 780M" in reason
    # The cause is the kernel, and the fix is a different kernel driver, not a reinstall of it.
    assert "kernel" in reason
    assert "amdgpu-dkms" in reason
    assert EL9_KERNEL in reason


def test_the_message_names_the_collected_kernel_not_the_analyzing_host():
    """Built from summary['drivers']['kernel'], so re-analyzing an el9 bundle on another machine
    still names the el9 kernel that failed to bind the card - not the host running the analysis."""
    reason = hwquery.no_driver_reason(_amd_summary(EL9_KERNEL))

    assert EL9_KERNEL in reason
    # Not the kernel of whatever machine is running this test (unless it happens to be the same).
    assert os.uname().release == EL9_KERNEL or os.uname().release not in reason


@pytest.mark.parametrize("kernel", [EL9_KERNEL, EL10_KERNEL])
def test_the_message_makes_no_alma_release_claim(kernel):
    """The suite knows only the inventory, not which AlmaLinux release carries which amdgpu id -
    an earlier version claimed that from container packages and real hardware disproved it. So the
    message names no release: it states the card is unbound and points at driver support, the
    parts it can actually stand behind."""
    reason = hwquery.no_driver_reason(_amd_summary(kernel))

    assert "AlmaLinux" not in reason
    assert "amdgpu-dkms" in reason
    assert "no reboot of this kernel" in reason


def test_the_unbound_message_does_not_offer_a_reboot_as_the_fix():
    """The whole point of the change: it must not send somebody to reboot a kernel that will come
    back exactly as unable to drive the card."""
    reason = hwquery.no_driver_reason(_amd_summary(EL9_KERNEL))

    # A reboot is named only to say it does NOT help; it is never the remedy.
    assert "no reboot" in reason
    assert "reboot and run again" not in reason.lower()


def test_an_unbound_non_amd_card_gets_a_generic_reason():
    """Only the AMD case has the specific kernel-support story, so an unbound Intel part gets the
    plain reason rather than an AMD explanation that would not be true of it."""
    reason = hwquery.no_driver_reason({"gpus": [INTEL_UNBOUND]})

    assert reason == "no GPU has a driver bound, so there is no card the GPU tests can use"


# --- the benchmark uses it -------------------------------------------------------


def test_the_clpeak_benchmark_reports_the_accurate_reason(tmp_path):
    ctx = RunContext(Config.load("/nonexistent"), str(tmp_path),
                     inventory={"summary": {"gpus": [AMD_UNBOUND]}}, pkg=_Pkg())

    reason = gpu_bench.Clpeak().applicable(ctx)

    assert reason == hwquery.no_driver_reason({"gpus": [AMD_UNBOUND]})
    assert "Radeon 780M" in reason and "kernel" in reason


def test_cuda_bandwidth_does_not_emit_the_deprecated_no_card_wording(tmp_path):
    """The CUDA benchmark shares the fix: an NVIDIA card present but unbound must not read as
    'no card' either. NVIDIA presence is established first, so the helper gives the generic
    'no driver bound' text rather than the old 'no GPU with an identified driver'."""
    nvidia_unbound = {"pci_ids": {"vendor": "NVIDIA Corporation [10de]",
                                  "device": "AD102 [26b9]"}, "driver": None}
    ctx = RunContext(Config.load("/nonexistent"), str(tmp_path),
                     inventory={"summary": {"gpus": [nvidia_unbound]}}, pkg=_Pkg())

    reason = gpu_bench.CudaBandwidth().applicable(ctx)

    assert reason == hwquery.no_driver_reason({"gpus": [nvidia_unbound]})
    assert "no GPU with an identified driver" not in reason


# --- the validation uses it, and installs nothing for a card it cannot use -------


def test_vulkan_validation_skips_without_installing_a_runtime(tmp_path):
    """``_install_runtime`` would otherwise pull mesa-vulkan-drivers to give an AMD card a Vulkan
    device; for a card the kernel never bound there is no device to give, so nothing is installed
    and the skip names why."""
    pkg = _Pkg()
    ctx = RunContext(Config.load("/nonexistent"), str(tmp_path),
                     inventory={"summary": {"gpus": [AMD_UNBOUND]}}, pkg=pkg)

    reason = gpuapi.VulkanDevices().applicable(ctx)

    assert reason == hwquery.no_driver_reason({"gpus": [AMD_UNBOUND]})
    assert pkg.ensured == [], "no runtime should be installed for a card the kernel cannot bind"
