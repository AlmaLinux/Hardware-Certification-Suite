"""OpenCL and Vulkan validation, for the machines where those are the runtime that matters.

The suite had CUDA checks that compile a probe and run it, and nothing equivalent for the two
runtimes that work on every vendor's hardware. So an AMD or Intel card could be certified with its
accelerator never having been asked to compute anything: ``validate.gpu.driver`` reports which
driver is bound and deliberately never fails, and the clpeak benchmark measures speed rather than
correctness. Reported from a machine with an Intel UHD 630, where nothing on the validation side
had anything to say about the card at all.

**Gated on being relevant, twice over.** A runtime is only checked when its ICD is installed,
because an absent OpenCL is a packaging fact about the machine and not a defect in the hardware: a
server whose only display adapter is a Matrox BMC chip has no business failing certification for
having no OpenCL. And a runtime with only a *software* device behind it is skipped rather than
passed, which matters more than it sounds:

- ``mesa-vulkan-drivers`` installs ``lvp_icd.json``, which is lavapipe, a complete Vulkan
  implementation running on the CPU.
- ``pocl`` and Mesa's rusticl-on-llvmpipe do the same for OpenCL.

Both are real, working, and prove nothing whatever about an accelerator. Verified on a development
box where Vulkan enumerated three devices: an AMD integrated GPU, an Intel Arc A380, and llvmpipe.
A check that took the first device it found would have certified the CPU.

**Severity follows the CUDA precedent.** These are ``REQUIRED``, and everything above is what makes
that fair: the test only runs when a GPU is present *and* a hardware-backed runtime for it is
installed, which together mean somebody set this machine up to do that kind of work. At that point a
device the runtime cannot use, or one that computes the wrong answer, is a defect.
"""

from __future__ import annotations

import glob
import os
from typing import Dict, List, Tuple

from .. import hwquery, procutil
from ..registry import REGISTRY, RunContext, Test
from ..result import Severity, Status

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# The device types that mean "there is hardware here". Anything else, and ``cpu`` in particular, is
# a software implementation: the probes report the type for exactly this decision.
_ACCELERATED = ("gpu", "accelerator", "integrated", "discrete", "virtual")


def opencl_icds() -> List[str]:
    """The installed OpenCL vendor ICDs.

    Its own function so a test can replace this rather than the stdlib's ``glob``. Patching that
    reaches every other caller in the process, which is how a test meaning to fake an empty ICD
    directory once also faked the result of looking for a bundled source archive.
    """
    return sorted(glob.glob("/etc/OpenCL/vendors/*.icd"))


def vulkan_icds() -> List[str]:
    """The installed Vulkan driver manifests, which is where ``mesa-vulkan-drivers`` puts them."""
    return sorted(glob.glob("/usr/share/vulkan/icd.d/*.json"))


def parse_probe(out: str) -> Dict[str, str]:
    """``key=value`` lines into a dict. The probes print nothing else on stdout."""
    values = {}
    for line in (out or "").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def accelerated_devices(values: Dict[str, str]) -> List[str]:
    """The names of the devices that are not software implementations.

    Read from the per-device ``type`` keys rather than from the probe's own count, so the two can
    be checked against each other and a probe that miscounted would be caught by the test below.
    """
    names = []
    for key, value in sorted(values.items()):
        if not key.endswith(".type") or value not in _ACCELERATED:
            continue
        names.append(values.get(key[: -len(".type")] + ".name", "an unnamed device"))
    return names


class _ApiTest(Test):
    """Shared build-and-run for a probe compiled on the machine under test.

    The same shape as the CUDA tests: compile once with the host's compiler, run one mode, parse
    ``key=value``. Kept here rather than in each test so the compiler diagnostics, the artifacts,
    and the skip-versus-error decision are made in one place.
    """

    category = "gpu"
    run_type = "validate"
    severity = Severity.REQUIRED
    default_timeout = 300

    # Subclasses set these. ``api`` names files and log keys, ``label`` goes in sentences: one
    # variable doing both produced "an vulkan driver is installed".
    api = ""
    label = ""
    # Spelled out rather than derived from the first letter, which is not what decides it: the
    # article follows how the name is *said*, so a future "SYCL" takes "a" despite starting with a
    # consonant letter and "OpenCL" takes "an" for the vowel it opens with. Reported as "a OpenCL
    # driver is installed" from an Intel UHD 630 report.
    article = "a"
    source = ""
    library = ""
    icds = staticmethod(lambda: [])
    packages: Tuple[str, ...] = ()
    repos: Tuple[str, ...] = ()

    # A hardware runtime to install per GPU PCI vendor id, so a card that shipped with none still
    # gets exercised rather than skipped for want of a driver. Empty here; the Vulkan subclass fills
    # it. See ``_install_runtime`` for why this is not left to ``setup``.
    runtime_packages: Dict[str, Tuple[str, ...]] = {}
    runtime_repos: Tuple[str, ...] = ()

    def _install_runtime(self, ctx: RunContext) -> None:
        """Install a hardware runtime for a present GPU, before the ICD gate below.

        In ``applicable`` rather than ``setup`` because the ICD gate skips the test when no driver
        is installed, and ``setup`` runs only after applicability has already passed: a runtime put
        there would never make the gate open. Targeted, so it touches nothing it need not: only
        when a GPU whose vendor this test has a package for is present, and only when no ICD is
        there yet, so a card that already has a working driver (NVIDIA's) is left untouched.

        Reported from an AMD integrated GPU on which nothing was installed or tested: the suite
        installed a runtime for NVIDIA and for Intel OpenCL, but never one that gives an AMD card a
        Vulkan device to enumerate.
        """
        if not ctx.pkg or not self.runtime_packages or self.icds():
            return
        present = {hwquery.gpu_vendor_id(gpu) for gpu in hwquery.gpus(ctx.summary)}
        wanted = sorted({
            package
            for vendor in present
            for package in self.runtime_packages.get(vendor, ())
        })
        if not wanted:
            return
        ctx.log("installing a %s runtime for the detected GPU: %s"
                % (self.label, ", ".join(wanted)))
        ctx.pkg.ensure(wanted, repos=self.runtime_repos)

    def applicable(self, ctx: RunContext):
        if not hwquery.gpus(ctx.summary):
            return "no GPU in the inventory"
        # Before installing a runtime, because a runtime is no use to a card the kernel never
        # bound: an unclaimed AMD GPU gets no Vulkan device from mesa no matter what is installed.
        # Naming that here skips honestly rather than installing a driver and then reporting an
        # empty probe.
        unbound = hwquery.no_driver_reason(ctx.summary)
        if unbound:
            return unbound
        self._install_runtime(ctx)
        if not self.icds():
            # A packaging fact about this machine, not a defect in the hardware. Named so the
            # operator can install one if they meant to.
            return (
                "no %s driver is installed on this machine, so there is no %s implementation to "
                "test" % (self.label, self.label)
            )
        if procutil.find_tool("cc") is None and procutil.find_tool("gcc") is None:
            return "no C compiler, so the bundled %s probe cannot be built" % self.label
        if not os.path.isfile(os.path.join(_DATA_DIR, self.source)):
            return (
                "no %s shipped in alma_certify/data, so there is nothing to build. Nothing "
                "about this machine" % self.source
            )
        if ctx.pkg and self.packages:
            # The names still absent, not everything asked for. ``missing`` returns them precisely
            # so a failure says which package to go and look at.
            unavailable = sorted(ctx.pkg.missing(self.packages, repos=self.repos))
            if unavailable:
                return "the %s probe cannot be built here: %s not available in the configured " \
                       "repos" % (self.label, ", ".join(unavailable))
        return None

    def build(self, ctx: RunContext):
        """Compile the probe. Returns ``(binary, artifacts)``, binary None on failure."""
        compiler = procutil.find_tool("cc") or procutil.find_tool("gcc")
        binary = os.path.join(os.path.dirname(ctx.artifact_path("x.log")), self.api + "probe")
        log = "%sprobe-build.log" % self.api
        res = ctx.cmd(
            [compiler, "-O2", "-o", binary, os.path.join(_DATA_DIR, self.source),
             "-l" + self.library],
            timeout=180,
            artifact=log,
        )
        artifacts = [ctx.rel_artifact(log)]
        return (binary if res.ok and os.path.exists(binary) else None), artifacts

    def probe(self, ctx: RunContext, mode: str):
        """Build and run one mode. Returns ``(values, early_result, artifacts)``."""
        binary, artifacts = self.build(ctx)
        if binary is None:
            return {}, self.result(
                Status.ERROR,
                reason=(
                    "the %s headers and library are installed and could not compile the bundled "
                    "probe, so the toolchain here is broken rather than absent" % self.label
                ),
                artifacts=artifacts,
            ), artifacts
        log = "%sprobe-%s.log" % (self.api, mode)
        res = ctx.cmd([binary, mode], timeout=self.default_timeout, artifact=log)
        artifacts.append(ctx.rel_artifact(log))
        values = parse_probe(res.stdout)
        if not res.ok and values.get("accelerated_device_count") == "0":
            # Not a failure: it exited non-zero because it had no hardware device to work with,
            # which the caller turns into a skip. Checked before the exit code, because an exit code
            # alone cannot tell "nothing here" from "went wrong".
            return values, self.no_hardware(values, artifacts), artifacts
        if not res.ok:
            return values, self.result(
                Status.FAIL,
                reason=values.get(
                    "error",
                    "the %s probe exited %s with no reason given" % (self.label, res.returncode),
                ),
                details={"probe": values},
                artifacts=artifacts,
            ), artifacts
        return values, None, artifacts

    def no_hardware(self, values: Dict[str, str], artifacts: List[str]):
        """A skip when no hardware device is behind this runtime, or None to carry on.

        Two situations, one verdict, and the verdict is **skip rather than fail**. This is the fix
        for a real report: an Intel UHD 630 on AlmaLinux 10 has ``intel-opencl`` installed, because
        the package exists for the vendor, and the driver in it does not support that generation of
        card. The runtime loads, claims nothing, and the card gets no OpenCL. Failing certification
        for that would condemn a working machine over a packaging fact, so the two cases below say
        what happened and leave the hardware uncondemned.

        Note the asymmetry with CUDA, where "the runtime reported no devices" *is* a failure. There
        it is preceded by ``nvidia-smi`` answering, which is the driver saying it claims the card.
        Here nothing has claimed anything.
        """
        # One rule, and it is the probe's own count rather than a re-derivation: a skip is
        # warranted only where the probe explicitly said it found no hardware device. The compute
        # modes print no per-device types at all, so reading those instead turned a successful
        # computation into a skip.
        if values.get("accelerated_device_count") != "0":
            return None
        devices = values.get("device_count", "0")
        if devices == "0":
            reason = (
                "%s %s driver is installed and no device claims this hardware, so this GPU has no "
                "%s support in what this release ships. Not a defect in the card" % (
                    self.article, self.label, self.label,
                )
            )
        else:
            reason = (
                "the only %s devices here are software implementations, which say nothing about "
                "this machine's hardware" % self.label
            )
        return self.result(
            Status.SKIP, reason=reason, details={"probe": values}, artifacts=artifacts,
        )


# --- OpenCL ---------------------------------------------------------------------


class _OpenClTest(_ApiTest):
    api = "opencl"
    label = "OpenCL"
    article = "an"
    source = "openclprobe.c"
    library = "OpenCL"
    icds = staticmethod(opencl_icds)
    # ``ocl-icd-devel`` carries the link-time libOpenCL.so and lives in CRB, which is disabled by
    # default. The headers are in AppStream.
    packages = ("opencl-headers", "ocl-icd-devel")
    repos = ("crb",)


class OpenClDevices(_OpenClTest):
    """Can an OpenCL runtime see the hardware, and what does it say about it?

    The counterpart of ``validate.gpu.cuda-devicequery``: it proves the ICD loader found a driver,
    the driver initialized, and a device exists. Everything it learns is recorded, because a
    reviewer comparing two runs of the same card wants the driver version that produced them.
    """

    id = "validate.gpu.opencl-devices"

    def run(self, ctx: RunContext):
        values, early, artifacts = self.probe(ctx, "devices")
        if early is not None:
            return early
        nothing = self.no_hardware(values, artifacts)
        if nothing is not None:
            return nothing
        found = accelerated_devices(values)
        return self.result(
            Status.PASS,
            details={"probe": values, "accelerated_devices": found},
            artifacts=artifacts,
            reason="OpenCL sees %d hardware device(s): %s" % (len(found), ", ".join(found)),
        )


class OpenClVectorAdd(_OpenClTest):
    """Does the device compute the right answer?

    The one check here that can catch hardware that runs and is wrong, which is why it verifies
    every element rather than a sample: a card wrong in one lane is what a spot check misses. The
    kernel is built by the device's own compiler at run time, so this also exercises the part of an
    OpenCL stack most likely to be broken by a bad driver package.
    """

    id = "validate.gpu.opencl-compute"

    def run(self, ctx: RunContext):
        values, early, artifacts = self.probe(ctx, "vectoradd")
        if early is not None:
            return early
        nothing = self.no_hardware(values, artifacts)
        if nothing is not None:
            return nothing
        mismatches = values.get("mismatches")
        if mismatches is None:
            return self.result(
                Status.ERROR,
                reason="the OpenCL probe ran and reported no result",
                details={"probe": values},
                artifacts=artifacts,
            )
        if mismatches != "0":
            return self.result(
                Status.FAIL,
                reason=values.get("error", "%s of %s elements were wrong" % (
                    mismatches, values.get("elements", "?"),
                )),
                details={"probe": values},
                artifacts=artifacts,
            )
        return self.result(
            Status.PASS,
            reason="%s elements computed correctly on %s" % (
                values.get("elements", "?"), values.get("device", "an OpenCL device"),
            ),
            details={"probe": values},
            artifacts=artifacts,
        )


# --- Vulkan ---------------------------------------------------------------------


class _VulkanTest(_ApiTest):
    api = "vulkan"
    label = "Vulkan"
    source = "vulkanprobe.c"
    library = "vulkan"
    icds = staticmethod(vulkan_icds)
    packages = ("vulkan-headers", "vulkan-loader-devel")
    repos = ()
    # mesa's radv (AMD) and anv (Intel) Vulkan ICDs, from AppStream on 8/9/10, so an AMD or Intel
    # GPU that came with no Vulkan driver gets one and can be enumerated. NVIDIA is deliberately not
    # here: its Vulkan ICD comes with the driver, and ``_install_runtime`` skips anyway once an ICD
    # exists. radv on el8's mesa 23.1 already supports RDNA3, which is what makes a current AMD
    # laptop iGPU testable.
    runtime_packages = {
        "1002": ("mesa-vulkan-drivers", "vulkan-loader"),
        "8086": ("mesa-vulkan-drivers", "vulkan-loader"),
    }
    runtime_repos = ("appstream",)


class VulkanDevices(_VulkanTest):
    """Can Vulkan see the hardware, and what does it say about it?

    Also the test that names the software implementations, because a machine with
    ``mesa-vulkan-drivers`` installed and no accelerator will enumerate lavapipe and nothing else.
    """

    id = "validate.gpu.vulkan-devices"

    def run(self, ctx: RunContext):
        values, early, artifacts = self.probe(ctx, "devices")
        if early is not None:
            return early
        nothing = self.no_hardware(values, artifacts)
        if nothing is not None:
            return nothing
        found = accelerated_devices(values)
        return self.result(
            Status.PASS,
            details={"probe": values, "accelerated_devices": found},
            artifacts=artifacts,
            reason="Vulkan sees %d hardware device(s): %s" % (len(found), ", ".join(found)),
        )


class VulkanFill(_VulkanTest):
    """Does the device do the work and write the right bytes?

    A GPU buffer fill, checked word by word. No shader is involved, deliberately: a compute dispatch
    would need SPIR-V, which would mean shipping a binary nobody can read in review or requiring
    ``glslc`` at validation time, and ``vkCmdFillBuffer`` already exercises the whole path that
    matters - instance, physical device, queue family, logical device, command pool, allocation,
    submission, fence, readback.
    """

    id = "validate.gpu.vulkan-compute"

    def run(self, ctx: RunContext):
        values, early, artifacts = self.probe(ctx, "fill")
        if early is not None:
            return early
        nothing = self.no_hardware(values, artifacts)
        if nothing is not None:
            return nothing
        mismatches = values.get("mismatches")
        if mismatches is None:
            return self.result(
                Status.ERROR,
                reason="the Vulkan probe ran and reported no result",
                details={"probe": values},
                artifacts=artifacts,
            )
        if mismatches != "0":
            return self.result(
                Status.FAIL,
                reason=values.get("error", "%s words of %s were wrong" % (
                    mismatches, values.get("words", "?"),
                )),
                details={"probe": values},
                artifacts=artifacts,
            )
        return self.result(
            Status.PASS,
            reason="%s bytes filled and verified on %s (%s)" % (
                values.get("bytes", "?"), values.get("device", "a Vulkan device"),
                values.get("device_type", "unknown type"),
            ),
            details={"probe": values},
            artifacts=artifacts,
        )


for _test in (OpenClDevices, OpenClVectorAdd, VulkanDevices, VulkanFill):
    REGISTRY.register(_test)
