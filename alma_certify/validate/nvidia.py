"""NVIDIA GPU validation: the driver talks to the card, and the card computes correctly.

``validate.gpu.driver`` records which GPUs are present and whether a driver is bound, and it is
informational on purpose: an unbound discrete GPU is not a hardware defect, because the driver may
live outside the distribution or be blacklisted deliberately, and the suite cannot know whether the
machine was ever meant to do GPU work. Its docstring says where the line is instead: "where GPU
capability does have to be proven, it is enforced at the point of the claim".

These tests are that point. They only run when there is an NVIDIA GPU and a CUDA toolchain, and
when both are present they are not informational: a card whose runtime cannot see it, or which
returns the wrong answer, has failed something a reader would want to know about before buying one.

The three checks are the three NVIDIA's own samples demonstrate, for the three questions a GPU
certification has to answer. Can the runtime see the card and agree with it about what it is
(deviceQuery). Does it compute the right answer (vectorAdd), which is the only one of the three
that catches a card that runs and is wrong. Do transfers over the link work (bandwidthTest).

They compile ``data/cudaprobe.cu`` rather than calling NVIDIA's sample binaries, because the
samples stopped being packaged after CUDA 12 and now live in a GitHub repository, so hunting
install paths would be guesswork with a short shelf life. The suite already compiles
``streamish.c`` and ``memlat.c`` from the same directory the same way.

Nothing here installs the driver or the toolkit. Both come from NVIDIA's own repository under
their license, and a certification suite that silently adds a third-party repository to a machine
it was pointed at would be doing something the operator did not ask for. When the toolchain is
absent these skip and say what to install.
"""

from __future__ import annotations

import glob
import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from .. import hwquery, procutil
from ..registry import REGISTRY, RunContext, Test
from ..result import Severity, Status

_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)
_SOURCE = "cudaprobe.cu"

# Where NVIDIA's own RPMs put the toolkit. Their packages install under a versioned prefix and put
# nothing on $PATH, so ``shutil.which("nvcc")`` finds nothing on a machine that has it: reported as
# three tests skipping with "nvcc not present" on a host where ``dnf install cuda-nvcc-13-3`` had
# already succeeded.
#
# No profile.d snippet exists to rescue this. Checked against their rhel9 and rhel10 repositories:
# no package there ships anything under /etc/profile.d and none ships /usr/bin/nvcc, so there is no
# configuration in which nvcc arrives on PATH by itself.
#
# The unversioned /usr/local/cuda symlink is real but is created by update-alternatives in
# cuda-toolkit-<ver>-config-common, which cuda-cudart pulls in and cuda-nvcc does not. So in exactly
# the reported situation, a compiler installed with cuda-nvcc alone, that symlink does not exist
# either and only the versioned prefix finds anything.
_CUDA_PREFIXES = ("/usr/local/cuda", "/opt/cuda")


def _version_key(path: str):
    """Sort key for a versioned CUDA prefix, newest first.

    Numerically, because a lexical sort puts cuda-9.0 above cuda-13.3 and would pick a toolkit six
    major versions old on a machine that has both.
    """
    name = os.path.basename(os.path.dirname(os.path.dirname(path)))
    _, _, version = name.partition("-")
    parts = []
    for chunk in version.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(-1)
    return parts or [-1]


def find_nvcc() -> Optional[str]:
    """The CUDA compiler, wherever this machine keeps it.

    In order: $PATH, because a distribution package or a hand-rolled setup may have put it there;
    the environment variables NVIDIA's documentation uses; the unversioned symlink their packages
    maintain; then the versioned prefixes, newest first.

    Returns an absolute path, which ``procutil.run_cmd`` accepts directly, so nothing downstream
    needs a modified PATH.
    """
    found = procutil.find_tool("nvcc")
    if found:
        return found
    roots = []
    for variable in ("CUDA_HOME", "CUDA_PATH"):
        value = os.environ.get(variable)
        if value:
            roots.append(value)
    roots.extend(_CUDA_PREFIXES)
    for root in roots:
        candidate = os.path.join(root, "bin", "nvcc")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    versioned = []
    for prefix in _CUDA_PREFIXES:
        versioned.extend(glob.glob(prefix + "-*/bin/nvcc"))
    for candidate in sorted(versioned, key=_version_key, reverse=True):
        if os.access(candidate, os.X_OK):
            return candidate
    return None

# What to install, named in the skip reason. Not installed automatically: see the module docstring.
# Named in every skip reason, and the command comes first.
#
# A run's stderr carries the same advice once, before it starts, but nobody reads stderr afterwards:
# the reviewable artifacts are the log and report.json, and a reason recorded there is the only form
# of this hint that survives the run. Reported as a benchmark run whose whole GPU category skipped
# with nothing in the log saying what to do about it.
_TOOLKIT_HINT = (
    "run 'alma-certify setup-gpu' to install it. By hand, that is the whole CUDA toolkit "
    "(cuda-toolkit) and the driver, not just the compiler: cuda-nvcc alone cannot build anything, "
    "because the runtime headers and libcudart come from cuda-cudart-devel"
)


def nvidia_gpus(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The NVIDIA GPUs in the inventory.

    By PCI vendor id, which is the only thing on a reported GPU that says who made the chip. This
    read ``gpu["vendor"] == "nvidia"`` and there is no such key: the collector reports every name
    lspci gave, verbatim, under ``pci_ids``, precisely so the server decides what a part is called.
    Its own comment warns about this, having made the mistake once itself.

    The symptom was as quiet as it gets. Every NVIDIA test skipped with "no NVIDIA GPU detected" on
    a machine with an NVIDIA card that ``lspci`` and ``nvidia-smi`` both saw, and a skip is not a
    failure, so the run passed and said nothing was wrong.

    A machine with an NVIDIA card and an AMD one is a real configuration, and these tests are about
    the NVIDIA one.
    """
    return [
        gpu for gpu in hwquery.gpus(summary)
        if hwquery.gpu_vendor_id(gpu) == hwquery.NVIDIA_VENDOR_ID
    ]


# The in-tree drivers for an NVIDIA card. The vendor driver cannot share a card with one, and
# ``gpusetup`` blacklists both by name when it installs.
_IN_TREE_DRIVERS = ("nouveau", "nova_core")


def vendor_driver_not_bound(summary: Dict[str, Any]) -> Optional[str]:
    """Why nothing here can measure this machine's NVIDIA card, or None to carry on.

    The card has to be on the ``nvidia`` driver. Nothing else answers a CUDA call, so where it is
    not, these tests produce facts about the running kernel rather than about the hardware, and a
    failure claims the card is defective when nothing ever reached it.

    Reported twice. First from live media, where ``setup-gpu`` installed the driver, said nouveau
    was holding the card so it could not load, and all four checks then failed anyway. Then from a
    machine whose card had no driver bound at all, where they failed with "no CUDA-capable device is
    detected" - which a first fix that looked only for nouveau did not cover, because an unbound
    card is the commoner shape of the same situation.

    From the inventory rather than ``/proc/modules``: it is collected after the setup offer, it
    records the driver bound to each card, and that is the question. Only when no NVIDIA card is on
    the vendor driver, because a machine can have two cards with one on each, and then there is
    something real to measure.
    """
    cards = nvidia_gpus(summary)
    # No card at all is somebody else's gate to report, and saying "no driver is bound to the
    # NVIDIA card" about a machine that has none would be a lie.
    if not cards:
        return None
    if any((gpu.get("driver") or "") == "nvidia" for gpu in cards):
        return None
    # Per card, because a machine can have several and they need not be in the same state: one held
    # by nouveau beside one nothing has bound is a real arrangement, and a message that collapsed
    # them would name a cause that is only half of what is there.
    detail = ", ".join(
        "%s (%s)" % (hwquery.gpu_name(gpu), gpu.get("driver") or "no driver bound")
        for gpu in cards
    )
    reason = (
        "no NVIDIA card on this machine is bound to the NVIDIA driver, so nvidia-smi and every "
        "CUDA call have nothing to reach: %s" % detail
    )
    if any((gpu.get("driver") or "") in _IN_TREE_DRIVERS for gpu in cards):
        return reason + (
            ". Blacklisting an in-tree driver happens on the kernel command line and in the "
            "initramfs, which take effect only at boot, so this needs a reboot with the NVIDIA "
            "driver installed"
        )
    if not any(gpu.get("driver") for gpu in cards):
        # No guess about why. A reboot was the first thing said here and it was wrong on the machine
        # that prompted it, where the driver package simply had no module for the running kernel.
        # The inventory cannot tell those apart, and the two commands below can.
        return reason + (
            ". Run 'alma-certify setup-gpu' to install and load it; where it is installed already, "
            "'modprobe nvidia' reports what stops it"
        )
    return reason


# A compile that failed for want of the CUDA runtime headers or library, rather than because the
# machine is broken. ``cuda-nvcc`` is the compiler alone: the headers come from
# ``cuda-cudart-devel`` and the link needs ``libcudart``. Installing only the compiler is an easy
# and reasonable mistake, and "your toolchain is broken" is the wrong thing to tell somebody who
# made it.
_MISSING_RUNTIME = ("cuda_runtime.h", "cannot find -lcudart", "libcudart", "crt/host_config.h")

# What the driver says when it is asked to JIT-compile PTX from a newer toolkit than it knows.
# Reported from AlmaLinux Kitten 10 x86_64, where the kernel's kABI pinned the driver a release
# back while the toolkit stayed current.
_PTX_TOO_NEW = (
    "unsupported toolchain",
    "PTX JIT compilation failed",
    "the provided PTX was compiled with an unsupported toolchain",
)


def target_arch(ctx: RunContext) -> Optional[str]:
    """``sm_89`` for the card in this machine, or None if it cannot be asked.

    From ``nvidia-smi``, which is already a prerequisite of every test here, rather than from the
    probe's own output: the arch has to be known before the probe is built.
    """
    res = ctx.cmd(
        ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"], timeout=60,
    )
    # Judged on what it printed rather than on its exit code as well: anything that is not a bare
    # compute capability fails the digit test, which makes a separate check of ``ok`` a branch no
    # input can reach.
    first = ((res.stdout or "").strip().splitlines() or [""])[0]
    digits = first.strip().replace(".", "")
    return "sm_%s" % digits if digits.isdigit() else None


def compile_probe(ctx: RunContext) -> Tuple[Optional[str], str]:
    """Compile the bundled CUDA probe. Returns ``(binary path or None, compiler output)``.

    Built for the card that is in the machine, which is not a micro-optimization. Without
    ``-arch`` nvcc emits PTX for its own default architecture and the driver JIT-compiles it at
    load time, so a toolkit newer than the driver fails at *run* time with "the provided PTX was
    compiled with an unsupported toolchain". Compiling for the real architecture emits SASS and
    asks the driver to compile nothing. CUDA's minor version compatibility covers the rest of the
    gap; PTX JIT is the one thing it explicitly does not cover, so it is the one thing to avoid.

    The retry without ``-arch`` keeps this from being a new way to fail: an arch nvcc does not know
    (a card newer than the toolkit) would otherwise turn a working build into a broken one.

    The output comes back because the *reason* a compile failed decides what to tell the operator,
    and the two reasons need different sentences: a missing runtime header means half the toolkit
    is installed, while anything else means a machine that cannot build 200 lines of runtime-API C.
    """
    nvcc = find_nvcc()
    if nvcc is None:
        return None, ""
    workdir = tempfile.mkdtemp(prefix="alma-certify-cuda-")
    binary = os.path.join(workdir, "cudaprobe")
    source = os.path.join(_DATA_DIR, _SOURCE)
    arch = target_arch(ctx)
    attempts = [["-arch=%s" % arch]] if arch else []
    attempts.append([])
    log = ""
    for extra in attempts:
        res = ctx.cmd(
            [nvcc, "-O2", *extra, "-o", binary, source], timeout=300, artifact="nvcc.log",
        )
        log = (res.stdout or "") + (res.stderr or "")
        if res.ok:
            return binary, log
    return None, log


def incomplete_toolkit(log: str) -> bool:
    """Whether a compile failure says the toolkit is half-installed rather than broken."""
    return any(needle in log for needle in _MISSING_RUNTIME)


def parse_probe(stdout: str) -> Dict[str, str]:
    """The probe's ``key=value`` lines as a dict.

    The probe prints for a parser rather than for a person, so nothing here has to scrape a human
    layout that a toolkit update can reflow.
    """
    values: Dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


class _CudaTest(Test):
    """Shared gate: an NVIDIA card, a driver that answers, and a toolchain to build with."""

    category = "gpu"
    run_type = "validate"
    # Required, unlike ``validate.gpu.driver``. Everything below skips unless the machine has an
    # NVIDIA GPU *and* a CUDA toolchain, which together mean somebody set this machine up to do
    # GPU work. At that point a card the runtime cannot use is a defect, not a packaging choice.
    severity = Severity.REQUIRED
    default_timeout = 600
    packages = ()

    def applicable(self, ctx: RunContext):
        if not nvidia_gpus(ctx.summary):
            return "no NVIDIA GPU detected"
        if procutil.find_tool("nvidia-smi") is None:
            return "nvidia-smi not present, so no NVIDIA driver is installed; " + _TOOLKIT_HINT
        held = vendor_driver_not_bound(ctx.summary)
        if held:
            return held
        if find_nvcc() is None:
            return "no CUDA compiler found, so there is no CUDA toolkit; " + _TOOLKIT_HINT
        return None

    def probe(self, ctx: RunContext, mode: str, device: int = 0):
        """Compile once, run one mode. Returns ``(values, result_or_None, artifacts)``.

        The result is non-None when something already went wrong, so the caller can return it
        without repeating the reporting.
        """
        artifacts = []
        binary, log = compile_probe(ctx)
        artifacts.append(ctx.rel_artifact("nvcc.log"))
        if binary is None:
            if incomplete_toolkit(log):
                # A skip, not an error. Half a toolkit is the same situation as no toolkit as far
                # as this machine's hardware is concerned, and the operator needs a package name
                # rather than a verdict.
                return {}, self.result(
                    Status.SKIP,
                    reason=(
                        "the CUDA compiler is installed but the runtime headers and library are "
                        "not, so the probe cannot be built; " + _TOOLKIT_HINT
                    ),
                    artifacts=artifacts,
                ), artifacts
            return {}, self.result(
                Status.ERROR,
                reason=(
                    "the CUDA compiler is installed and could not compile the bundled probe, "
                    "which means the toolchain on this machine is broken rather than absent"
                ),
                artifacts=artifacts,
            ), artifacts
        res = ctx.cmd(
            [binary, mode, str(device)],
            timeout=self.default_timeout,
            artifact="cudaprobe-%s.log" % mode,
        )
        artifacts.append(ctx.rel_artifact("cudaprobe-%s.log" % mode))
        values = parse_probe(res.stdout)
        if not res.ok:
            said = values.get(
                "error", "the CUDA probe exited %s with no reason given" % res.returncode
            )
            if any(marker in said for marker in _PTX_TOO_NEW):
                # A skip, not a failure. The card was never reached: the driver refused to compile
                # the binary's PTX because the toolkit that produced it is newer than the driver
                # understands. Nothing here is a fact about the hardware.
                return values, self.result(
                    Status.SKIP,
                    reason=(
                        "the CUDA toolkit on this machine is newer than the driver can accept: %s. "
                        "Install a toolkit matching the driver, or a driver matching the toolkit; "
                        "on a kernel whose kABI pins the driver back, the toolkit is the one to "
                        "move" % said
                    ),
                    details={"probe": values},
                    artifacts=artifacts,
                ), artifacts
            return values, self.result(
                Status.FAIL,
                reason=said,
                details={"probe": values},
                artifacts=artifacts,
            ), artifacts
        return values, None, artifacts


class NvidiaSmi(_CudaTest):
    """The driver answers about the card.

    Separate from the CUDA checks and first, because it isolates the commonest failure: a driver
    built against a different kernel than the one running. ``nvidia-smi`` fails there with a clear
    message, and without this test that would surface as a confusing CUDA initialization error
    three tests later.
    """

    id = "validate.gpu.nvidia-smi"
    default_timeout = 120

    def applicable(self, ctx: RunContext):
        # No nvcc requirement: this one is about the driver, and it is worth running on a machine
        # that has the driver and no toolkit.
        if not nvidia_gpus(ctx.summary):
            return "no NVIDIA GPU detected"
        if procutil.find_tool("nvidia-smi") is None:
            return "nvidia-smi not present, so no NVIDIA driver is installed; " + _TOOLKIT_HINT
        return vendor_driver_not_bound(ctx.summary)

    def run(self, ctx: RunContext):
        res = ctx.cmd(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,vbios_version,memory.total,pstate,"
                "ecc.mode.current,persistence_mode",
                "--format=csv,noheader",
            ],
            timeout=self.default_timeout,
            artifact="nvidia-smi.csv",
        )
        artifacts = [ctx.rel_artifact("nvidia-smi.csv")]
        if not res.ok:
            return self.result(
                Status.FAIL,
                reason=(
                    "nvidia-smi failed, which usually means the installed driver does not match "
                    "the running kernel: %s" % (res.stderr.strip().splitlines() or ["no output"])[0]
                ),
                artifacts=artifacts,
            )
        cards = [line for line in res.stdout.splitlines() if line.strip()]
        if not cards:
            return self.result(
                Status.FAIL,
                reason=(
                    "nvidia-smi ran but reported no GPUs, so the driver is not bound to the card"
                ),
                artifacts=artifacts,
            )
        detail = [
            dict(
                zip(
                    ("name", "driver_version", "vbios_version", "memory_total",
                     "pstate", "ecc_mode", "persistence_mode"),
                    [field.strip() for field in line.split(",")],
                )
            )
            for line in cards
        ]
        return self.result(
            Status.PASS,
            reason="%d NVIDIA GPU(s), driver %s" % (
                len(detail), detail[0].get("driver_version", "?"),
            ),
            details={"gpus": detail},
            artifacts=artifacts,
        )


class CudaDeviceQuery(_CudaTest):
    """The CUDA runtime enumerates the card and agrees with the driver about it.

    deviceQuery, and the first thing that has to work before any of the rest means anything. The
    reported compute capability is also the fact a reader most often wants: it decides which
    software will run on the card at all.
    """

    id = "validate.gpu.cuda-devicequery"
    default_timeout = 300

    def run(self, ctx: RunContext):
        values, failure, artifacts = self.probe(ctx, "devices")
        if failure is not None:
            return failure
        count = int(values.get("device_count", "0") or 0)
        if count < 1:
            return self.result(
                Status.FAIL,
                reason="the CUDA runtime reported no devices although the driver sees the card",
                details={"probe": values}, artifacts=artifacts,
            )
        # Reported rather than gated. A card in exclusive-process mode is a configuration choice
        # somebody made on purpose, and it is the reason the *next* job on the machine fails, so
        # it belongs in the record even though it is not a defect.
        modes = {k: v for k, v in values.items() if k.endswith(".compute_mode")}
        names = [v for k, v in sorted(values.items()) if k.endswith(".name")]
        return self.result(
            Status.PASS,
            reason="CUDA sees %d device(s): %s (runtime %s, driver %s)" % (
                count, ", ".join(names) or "unnamed",
                values.get("cuda_runtime_version", "?"),
                values.get("cuda_driver_version", "?"),
            ),
            details={"probe": values, "compute_modes": modes},
            artifacts=artifacts,
        )


class CudaVectorAdd(_CudaTest):
    """The card computes the right answer.

    vectorAdd, and the only one of these three that can catch a card which runs and is wrong,
    which is the failure mode that matters most and shows up least. Every element is checked, not
    a sample: a card wrong in one lane is exactly what a spot check misses.
    """

    id = "validate.gpu.cuda-vectoradd"
    default_timeout = 300

    def run(self, ctx: RunContext):
        values, failure, artifacts = self.probe(ctx, "vectoradd")
        if failure is not None:
            return failure
        mismatches = int(values.get("mismatches", "0") or 0)
        if mismatches:
            return self.result(
                Status.FAIL,
                reason="%s of %s elements came back wrong, worst error %s" % (
                    values.get("mismatches"), values.get("elements"),
                    values.get("worst_absolute_error"),
                ),
                details={"probe": values}, artifacts=artifacts,
            )
        return self.result(
            Status.PASS,
            reason="%s elements computed correctly on the device" % values.get("elements", "?"),
            details={"probe": values}, artifacts=artifacts,
        )


class CudaBandwidth(_CudaTest):
    """Transfers over the link work in both directions and on the card.

    bandwidthTest, as a validation rather than a measurement: the question here is whether a
    transfer completes and returns a rate that is not absurd, because a link negotiated down to
    one lane is a real and quiet fault. How fast the card actually is belongs on a leaderboard,
    and ``bench.gpu.cuda-bandwidth`` records it there from the same probe.
    """

    id = "validate.gpu.cuda-bandwidth"
    default_timeout = 300
    # A floor, not a target. Any PCIe generation at any sane width clears this comfortably, so
    # tripping it means something is badly wrong rather than merely slower than hoped: a link
    # trained at one lane, or a card sitting behind a saturated switch.
    MINIMUM_GIB_PER_S = 0.5

    def run(self, ctx: RunContext):
        values, failure, artifacts = self.probe(ctx, "bandwidth")
        if failure is not None:
            return failure
        rates = {
            key: float(values[key])
            for key in ("host_to_device_gib_per_s", "device_to_host_gib_per_s",
                        "device_to_device_gib_per_s")
            if key in values
        }
        missing = [
            key for key in ("host_to_device_gib_per_s", "device_to_host_gib_per_s")
            if key not in rates
        ]
        if missing:
            return self.result(
                Status.FAIL,
                reason="the probe reported no rate for %s" % ", ".join(missing),
                details={"probe": values}, artifacts=artifacts,
            )
        slow = {
            key: rate for key, rate in rates.items()
            if key != "device_to_device_gib_per_s" and rate < self.MINIMUM_GIB_PER_S
        }
        if slow:
            return self.result(
                Status.FAIL,
                reason=(
                    "%s below %s GiB/s, which suggests a link trained at fewer lanes than the "
                    "card expects rather than a slow card" % (
                        ", ".join("%s at %.2f" % (k, v) for k, v in sorted(slow.items())),
                        self.MINIMUM_GIB_PER_S,
                    )
                ),
                details={"probe": values}, artifacts=artifacts,
            )
        return self.result(
            Status.PASS,
            reason="host to device %.2f GiB/s, device to host %.2f GiB/s" % (
                rates["host_to_device_gib_per_s"], rates["device_to_host_gib_per_s"],
            ),
            details={"probe": values}, artifacts=artifacts,
        )


REGISTRY.register(NvidiaSmi)
REGISTRY.register(CudaDeviceQuery)
REGISTRY.register(CudaVectorAdd)
REGISTRY.register(CudaBandwidth)
