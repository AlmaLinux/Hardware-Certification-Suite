"""NVIDIA CUDA validation against captured probe output.

Every verdict is reachable without an NVIDIA card, because the probe prints ``key=value`` lines for
a parser rather than a human layout: the fixtures below are those lines. That was the reason for
choosing that format, and these tests are what it buys.

``validate.gpu.driver`` is informational on purpose and stays that way. These are required, and the
skip conditions are what makes that safe: no NVIDIA GPU, no driver, or no toolkit and they do not
run at all. A machine with a card, a driver, and a toolkit is a machine somebody set up to do GPU
work, and there a card the runtime cannot use is a defect rather than a packaging choice.
"""

import pytest

from alma_certify.config import Config
from alma_certify.registry import RunContext
from alma_certify.result import Severity, Status
from alma_certify.validate import nvidia as nvidia_mod
from alma_certify.validate.nvidia import (
    CudaBandwidth,
    CudaDeviceQuery,
    CudaVectorAdd,
    NvidiaSmi,
    parse_probe,
)

# The shape the *collector* actually produces. There is no flattened ``vendor`` token on a GPU
# entry: every name lspci gave is recorded verbatim under ``pci_ids`` so the server decides what
# the part is called.
#
# These fixtures used to say ``{"vendor": "nvidia"}``, which is why twenty tests passed while every
# NVIDIA test skipped on a machine with an NVIDIA card in it. The fixture encoded the same wrong
# assumption as the code, so the two agreed with each other and neither agreed with the collector.
# ``test_the_detection_matches_what_the_collector_emits`` below is the guard against that: it goes
# through the real parsing path instead of trusting a dict written by hand.
NVIDIA_GPU = {
    "pci": "41:00.0",
    "pci_ids": {
        "vendor": "NVIDIA Corporation [10de]",
        "device": "AD102GL [L40S] [26b9]",
        "subsystem_vendor": "NVIDIA Corporation [10de]",
    },
    "driver": "nvidia",
    "driver_version": "550.90.07",
    "smi_name": "NVIDIA L40S",
    "runtime": {"cuda": "12.4"},
    "vbios": "95.02.66.00.01",
}
AMD_GPU = {
    "pci": "63:00.0",
    "pci_ids": {"vendor": "Advanced Micro Devices, Inc. [AMD/ATI] [1002]",
                "device": "Navi 31 [Radeon Pro W7900] [744c]"},
    "driver": "amdgpu",
}
BMC_ADAPTER = {
    "pci": "07:00.0",
    "pci_ids": {"vendor": "Matrox Electronics Systems Ltd. [102b]",
                "device": "Integrated Matrox G200eW3 Graphics Controller [0536]"},
    "driver": None,
}
# One older bundle's worth of the flattened shape, still accepted so a summary from a previous
# collector does not silently stop matching.
LEGACY_NVIDIA_GPU = {"vendor": "nvidia", "model": "L40S", "driver": "nvidia"}

# Real ``lspci -vmmnnk`` output, for the detection test that does not trust a hand-written dict.
LSPCI = """\
Slot:\t41:00.0
Class:\tVGA compatible controller [0300]
Vendor:\tNVIDIA Corporation [10de]
Device:\tAD102GL [L40S] [26b9]
SVendor:\tNVIDIA Corporation [10de]
SDevice:\tAD102GL [L40S] [16a1]
Driver:\tnvidia

Slot:\t07:00.0
Class:\tVGA compatible controller [0300]
Vendor:\tMatrox Electronics Systems Ltd. [102b]
Device:\tIntegrated Matrox G200eW3 Graphics Controller [0536]

Slot:\t00:1f.6
Class:\tEthernet controller [0200]
Vendor:\tIntel Corporation [8086]
Device:\tEthernet Connection (2) I219-LM [15b8]
Driver:\te1000e
"""

DEVICES_OK = """\
device_count=1
cuda_runtime_version=12040
cuda_driver_version=12040
device.0.name=NVIDIA L40S
device.0.compute_capability=8.9
device.0.total_memory_mib=45589
device.0.multiprocessors=142
device.0.clock_khz=2520000
device.0.ecc_enabled=1
device.0.compute_mode=0
"""

DEVICES_NONE = """\
device_count=0
error=the CUDA runtime reported no devices
"""

VECTORADD_OK = """\
device=0
elements=16777216
mismatches=0
worst_absolute_error=0
"""

VECTORADD_WRONG = """\
device=0
elements=16777216
mismatches=41
worst_absolute_error=3.5
error=41 of 16777216 elements were wrong
"""

BANDWIDTH_OK = """\
device=0
bytes=268435456
iterations=10
host_to_device_gib_per_s=24.918
device_to_host_gib_per_s=26.104
device_to_device_gib_per_s=712.400
"""

BANDWIDTH_ONE_LANE = BANDWIDTH_OK.replace(
    "host_to_device_gib_per_s=24.918", "host_to_device_gib_per_s=0.212"
)

SMI_CSV = (
    "NVIDIA L40S, 550.90.07, 95.02.66.00.01, 46068 MiB, P0, Enabled, Enabled\n"
)


class FakeCmd:
    """Stands in for ``ctx.cmd``, answering by the first argument it recognizes."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def cmd(self, argv, timeout=None, artifact=None, check=False):
        self.calls.append(argv)
        for needle, (ok, stdout, stderr) in self.answers.items():
            if any(needle in str(part) for part in argv):
                return _Result(ok, stdout, stderr)
        return _Result(True, "", "")


class _Result:
    def __init__(self, ok, stdout, stderr=""):
        self.ok = ok
        self.returncode = 0 if ok else 1
        self.stdout = stdout
        self.stderr = stderr


def make_ctx(tmp_path, gpus=(NVIDIA_GPU,), answers=None):
    ctx = RunContext(
        Config.load("/nonexistent"), str(tmp_path),
        inventory={"summary": {"gpus": list(gpus)}},
    )
    ctx.log = lambda msg: None
    if answers is not None:
        ctx.cmd = FakeCmd(answers).cmd
    return ctx


@pytest.fixture
def toolchain(monkeypatch):
    """A machine with the driver and the toolkit."""
    monkeypatch.setattr(
        nvidia_mod.procutil, "find_tool", lambda tool, extra_dirs=(): "/usr/bin/" + tool,
    )


@pytest.fixture
def compiles(monkeypatch):
    """A toolkit that builds the probe. ``compile_probe`` returns the binary and the compiler
    output, because the *reason* a compile failed decides what to tell the operator."""
    monkeypatch.setattr(nvidia_mod, "compile_probe", lambda ctx: ("/tmp/cudaprobe", ""))


# --- the parser ----------------------------------------------------------------


def test_the_probe_output_parses_to_a_dict():
    values = parse_probe(DEVICES_OK)

    assert values["device_count"] == "1"
    assert values["device.0.compute_capability"] == "8.9"


def test_a_line_with_no_equals_is_ignored():
    """The probe prints only key=value, but a toolkit warning on stdout must not derail it."""
    values = parse_probe("nvcc warning: something\ndevice_count=2\n")

    assert values == {"device_count": "2"}


# --- when these run at all -----------------------------------------------------


def test_they_skip_without_an_nvidia_gpu(tmp_path, toolchain):
    """An AMD card and a BMC display adapter are both normal, and neither is what this tests."""
    ctx = make_ctx(tmp_path, gpus=(AMD_GPU, BMC_ADAPTER))

    assert CudaDeviceQuery().applicable(ctx) == "no NVIDIA GPU detected"


def test_they_skip_without_a_driver(tmp_path, monkeypatch):
    """A card with no driver installed is a packaging choice, not a defect: that judgement belongs
    to ``validate.gpu.driver``, which reports it and gates nothing."""
    monkeypatch.setattr(nvidia_mod.procutil, "find_tool", lambda tool, extra_dirs=(): None)
    ctx = make_ctx(tmp_path)

    reason = CudaDeviceQuery().applicable(ctx)

    assert "no NVIDIA driver is installed" in reason


def test_they_skip_without_a_toolkit(tmp_path, monkeypatch):
    """And the reason says what to install, because a skip nobody can act on is a dead end."""
    monkeypatch.setattr(
        nvidia_mod.procutil, "find_tool",
        lambda tool, extra_dirs=(): None if tool == "nvcc" else "/usr/bin/" + tool,
    )
    ctx = make_ctx(tmp_path)

    reason = CudaDeviceQuery().applicable(ctx)

    assert "no CUDA toolkit" in reason
    assert "cuda-toolkit" in reason


def test_nvidia_smi_runs_without_a_toolkit(tmp_path, monkeypatch):
    """It is about the driver, so a machine with the driver and no toolkit should still check it."""
    monkeypatch.setattr(
        nvidia_mod.procutil, "find_tool",
        lambda tool, extra_dirs=(): None if tool == "nvcc" else "/usr/bin/" + tool,
    )
    ctx = make_ctx(tmp_path)

    assert NvidiaSmi().applicable(ctx) is None


# --- the card is not on the NVIDIA driver ---------------------------------------
#
# Reported twice. From live media, where setup-gpu installed the driver, said nouveau was holding
# the card so it could not load, and the checks failed anyway; then from a machine whose card had no
# driver bound at all, where they failed with "no CUDA-capable device is detected".

NOUVEAU_GPU = dict(NVIDIA_GPU, driver="nouveau", driver_version=None, smi_name=None)
NOVA_CORE_GPU = dict(NVIDIA_GPU, driver="nova_core", driver_version=None, smi_name=None)
UNBOUND_GPU = dict(NVIDIA_GPU, driver=None, driver_version=None, smi_name=None)
VFIO_GPU = dict(NVIDIA_GPU, driver="vfio-pci", driver_version=None, smi_name=None)


@pytest.mark.parametrize("cls", [NvidiaSmi, CudaDeviceQuery, CudaVectorAdd, CudaBandwidth])
@pytest.mark.parametrize("gpu", [NOUVEAU_GPU, UNBOUND_GPU], ids=["nouveau", "unbound"])
def test_a_card_the_driver_never_bound_skips_rather_than_failing(cls, gpu, tmp_path, toolchain):
    """Every one of these failed on both machines. None of the failures said anything about the
    hardware: the vendor driver was never bound to the card they were asking about."""
    ctx = make_ctx(tmp_path, gpus=(gpu,))

    reason = cls().applicable(ctx)

    assert reason, "%s ran against a card the NVIDIA driver never bound" % cls.__name__


def test_the_nouveau_skip_names_nouveau(tmp_path, toolchain):
    reason = CudaDeviceQuery().applicable(make_ctx(tmp_path, gpus=(NOUVEAU_GPU,)))

    assert "nouveau" in reason


def test_the_unbound_skip_does_not_blame_a_driver_that_is_not_there(tmp_path, toolchain):
    """The reported case: "no CUDA-capable device is detected" on a machine whose card lspci shows
    with no driver at all. Naming nouveau there would be a guess."""
    reason = CudaDeviceQuery().applicable(make_ctx(tmp_path, gpus=(UNBOUND_GPU,)))

    assert "no driver bound" in reason
    assert "nouveau" not in reason.lower(), "named a driver that is not there"
    # And no guess at the cure: a reboot was said here first and was wrong on that machine, whose
    # driver package had no module for the running kernel at all.
    assert "reboot" not in reason.lower()
    assert "setup-gpu" in reason and "modprobe nvidia" in reason


def test_a_passed_through_card_says_what_holds_it(tmp_path, toolchain):
    """vfio-pci is neither nouveau nor a fault, and the blacklist advice would be wrong."""
    reason = CudaDeviceQuery().applicable(make_ctx(tmp_path, gpus=(VFIO_GPU,)))

    assert "vfio-pci" in reason
    assert "blacklist" not in reason.lower(), "a passed-through card is not waiting on a reboot"
    assert "reboot" not in reason.lower()


def test_the_nouveau_skip_says_what_to_do_about_it(tmp_path, toolchain):
    """A reboot, and the reason a reboot is what it takes: the blacklist is a boot-time thing."""
    reason = CudaDeviceQuery().applicable(make_ctx(tmp_path, gpus=(NOUVEAU_GPU,)))

    assert "initramfs" in reason and "reboot" in reason


def test_nouveaus_rust_successor_counts_too(tmp_path, toolchain):
    """``nova_core`` is on AlmaLinux 10 and the driver packages blacklist it by name as well, so it
    earns the same reboot advice rather than being treated as a foreign driver."""
    reason = CudaDeviceQuery().applicable(make_ctx(tmp_path, gpus=(NOVA_CORE_GPU,)))

    assert "nova_core" in reason
    assert "initramfs" in reason and "reboot" in reason


def test_a_second_card_on_the_vendor_driver_still_gets_tested(tmp_path, toolchain):
    """nouveau can hold one card and not another, and the CUDA runtime enumerates only the cards the
    vendor driver bound. There is something real to measure there, and skipping it would lose
    coverage on a working machine."""
    ctx = make_ctx(tmp_path, gpus=(NOUVEAU_GPU, NVIDIA_GPU))

    assert CudaDeviceQuery().applicable(ctx) is None


def test_several_cards_in_different_states_are_each_named(tmp_path, toolchain):
    """One held by nouveau beside one nothing bound is a real arrangement, and a reason that
    collapsed them would name a cause that is only half of what is on the machine."""
    unbound = dict(UNBOUND_GPU, pci_ids={"vendor": "NVIDIA Corporation [10de]",
                                         "device": "GB206 [2c38]"})

    reason = CudaDeviceQuery().applicable(make_ctx(tmp_path, gpus=(NOUVEAU_GPU, unbound)))

    assert "L40S" in reason and "nouveau" in reason
    assert "GB206 [2c38]" in reason and "no driver bound" in reason


def test_a_card_on_the_vendor_driver_is_untouched(tmp_path, toolchain):
    assert CudaDeviceQuery().applicable(make_ctx(tmp_path)) is None


def test_an_uninstalled_driver_is_still_reported_as_uninstalled(tmp_path, monkeypatch):
    """Ordered ahead of this one: telling somebody to reboot is no use when there is nothing
    installed to reboot into."""
    monkeypatch.setattr(nvidia_mod.procutil, "find_tool", lambda tool, extra_dirs=(): None)
    ctx = make_ctx(tmp_path, gpus=(NOUVEAU_GPU,))

    assert "no NVIDIA driver is installed" in CudaDeviceQuery().applicable(ctx)


def test_an_amd_card_on_amdgpu_is_not_mistaken_for_this(tmp_path, toolchain):
    """The helper is about NVIDIA cards; an in-tree driver on somebody else's card is normal."""
    assert nvidia_mod.vendor_driver_not_bound({"gpus": [AMD_GPU]}) is None


def test_an_intel_igpu_beside_an_unbound_nvidia_card_does_not_rescue_it(tmp_path, toolchain):
    """The reported machine exactly: an Arrow Lake iGPU bound to i915 and an NVIDIA card bound to
    nothing. A gate that asked only whether *some* GPU had a driver let all four run."""
    igpu = {"pci": "00:02.0", "driver": "i915",
            "pci_ids": {"vendor": "Intel Corporation [8086]", "device": "Arrow Lake-S [7d67]"}}
    ctx = make_ctx(tmp_path, gpus=(igpu, UNBOUND_GPU))

    assert CudaDeviceQuery().applicable(ctx) is not None


def test_the_cuda_benchmark_skips_there_too(tmp_path, toolchain):
    """nouveau is a bound driver as far as ``gpu_driver_info`` is concerned, so without its own
    check the benchmark built the probe and reported an error against a card it never reached."""
    from alma_certify.benchmarks import gpu as gpu_bench

    reason = gpu_bench.CudaBandwidth().applicable(make_ctx(tmp_path, gpus=(NOUVEAU_GPU,)))

    assert reason and "nouveau" in reason


def test_they_are_required_when_they_do_run():
    """The whole point of the feature. A GPU-scoped run needs at least one non-informational
    result, or it certifies a card on the strength of having seen a driver."""
    for cls in (NvidiaSmi, CudaDeviceQuery, CudaVectorAdd, CudaBandwidth):
        assert cls.severity == Severity.REQUIRED, cls.id


# --- the driver ----------------------------------------------------------------


def test_nvidia_smi_reports_the_card(tmp_path, toolchain):
    ctx = make_ctx(tmp_path, answers={"nvidia-smi": (True, SMI_CSV, "")})

    result = NvidiaSmi().run(ctx)

    assert result.status == Status.PASS
    assert "550.90.07" in result.reason
    assert result.details["gpus"][0]["name"] == "NVIDIA L40S"


def test_a_driver_built_against_another_kernel_fails(tmp_path, toolchain):
    """The commonest GPU failure there is, and the reason this test comes before the CUDA ones:
    without it the same fault surfaces as a confusing initialization error three tests later."""
    ctx = make_ctx(tmp_path, answers={
        "nvidia-smi": (False, "", "NVML: Driver/library version mismatch"),
    })

    result = NvidiaSmi().run(ctx)

    assert result.status == Status.FAIL
    assert "does not match the running kernel" in result.reason


def test_nvidia_smi_reporting_no_cards_fails(tmp_path, toolchain):
    ctx = make_ctx(tmp_path, answers={"nvidia-smi": (True, "\n", "")})

    result = NvidiaSmi().run(ctx)

    assert result.status == Status.FAIL
    assert "not bound" in result.reason


# --- deviceQuery ---------------------------------------------------------------


def test_devicequery_reports_the_capability(tmp_path, toolchain, compiles):
    """Compute capability is the fact a reader most often wants: it decides what software will run
    on the card at all."""
    ctx = make_ctx(tmp_path, answers={"cudaprobe": (True, DEVICES_OK, "")})

    result = CudaDeviceQuery().run(ctx)

    assert result.status == Status.PASS
    assert "NVIDIA L40S" in result.reason
    assert result.details["probe"]["device.0.compute_capability"] == "8.9"


def test_a_runtime_that_sees_no_device_fails(tmp_path, toolchain, compiles):
    """The driver sees the card and the runtime does not, which is a real state and a broken one."""
    ctx = make_ctx(tmp_path, answers={"cudaprobe": (False, DEVICES_NONE, "")})

    result = CudaDeviceQuery().run(ctx)

    assert result.status == Status.FAIL
    assert "no devices" in result.reason


def test_the_compute_mode_is_recorded_but_does_not_gate(tmp_path, toolchain, compiles):
    """Exclusive-process mode is somebody's deliberate choice, and it is the reason the next job on
    the machine fails, so it belongs in the record without failing this run."""
    exclusive = DEVICES_OK.replace("compute_mode=0", "compute_mode=1")
    ctx = make_ctx(tmp_path, answers={"cudaprobe": (True, exclusive, "")})

    result = CudaDeviceQuery().run(ctx)

    assert result.status == Status.PASS
    assert result.details["compute_modes"]["device.0.compute_mode"] == "1"


def test_a_broken_toolchain_is_an_error_not_a_skip(tmp_path, toolchain, monkeypatch):
    """A compiler that cannot build 200 lines of runtime-API C is a broken machine. Skipping would
    hide it behind a sentence that reads like an absent toolkit."""
    monkeypatch.setattr(
        nvidia_mod, "compile_probe", lambda ctx: (None, "internal compiler error"),
    )
    ctx = make_ctx(tmp_path)

    result = CudaDeviceQuery().run(ctx)

    assert result.status == Status.ERROR
    assert "broken rather than absent" in result.reason


def test_half_a_toolkit_skips_and_names_the_package(tmp_path, toolchain, monkeypatch):
    """``cuda-nvcc`` is the compiler alone: the headers come from ``cuda-cudart-devel`` and the
    link needs ``libcudart``. Installing only the compiler is an easy and reasonable mistake, and
    "your toolchain is broken" is the wrong thing to tell somebody who made it.

    A skip rather than an error, because as far as this machine's hardware is concerned half a
    toolkit is the same situation as none.
    """
    monkeypatch.setattr(
        nvidia_mod, "compile_probe",
        lambda ctx: (None, "cudaprobe.cu:30:10: fatal error: cuda_runtime.h: No such file"),
    )
    ctx = make_ctx(tmp_path)

    result = CudaDeviceQuery().run(ctx)

    assert result.status == Status.SKIP
    assert "cuda-toolkit" in result.reason


# --- vectorAdd -----------------------------------------------------------------


def test_vectoradd_passes_when_every_element_is_right(tmp_path, toolchain, compiles):
    ctx = make_ctx(tmp_path, answers={"cudaprobe": (True, VECTORADD_OK, "")})

    result = CudaVectorAdd().run(ctx)

    assert result.status == Status.PASS
    assert "16777216 elements" in result.reason


def test_a_card_that_computes_the_wrong_answer_fails(tmp_path, toolchain, compiles):
    """The failure mode that matters most and shows up least. Nothing else in the suite catches a
    card that runs and is wrong."""
    ctx = make_ctx(tmp_path, answers={"cudaprobe": (False, VECTORADD_WRONG, "")})

    result = CudaVectorAdd().run(ctx)

    assert result.status == Status.FAIL
    assert "41 of 16777216" in result.reason


# --- bandwidthTest -------------------------------------------------------------


def test_bandwidth_passes_at_a_sane_rate(tmp_path, toolchain, compiles):
    ctx = make_ctx(tmp_path, answers={"cudaprobe": (True, BANDWIDTH_OK, "")})

    result = CudaBandwidth().run(ctx)

    assert result.status == Status.PASS
    assert "24.92 GiB/s" in result.reason


def test_a_link_trained_at_one_lane_fails(tmp_path, toolchain, compiles):
    """A quiet fault with a large consequence: the card works, and everything on it is slow. The
    floor is low enough that any sane link clears it, so tripping it means something is wrong
    rather than merely slower than hoped."""
    ctx = make_ctx(tmp_path, answers={"cudaprobe": (True, BANDWIDTH_ONE_LANE, "")})

    result = CudaBandwidth().run(ctx)

    assert result.status == Status.FAIL
    assert "fewer lanes" in result.reason


def test_a_missing_rate_fails_rather_than_passing_silently(tmp_path, toolchain, compiles):
    """A probe that reported nothing must not read as a card that transferred nothing wrong."""
    ctx = make_ctx(tmp_path, answers={"cudaprobe": (True, "device=0\nbytes=1\n", "")})

    result = CudaBandwidth().run(ctx)

    assert result.status == Status.FAIL
    assert "no rate" in result.reason


def test_device_to_device_alone_does_not_gate(tmp_path, toolchain, compiles):
    """The floor is about the link to the host. On-card copies are orders of magnitude faster and
    have nothing to do with how the card is seated."""
    slow_internal = BANDWIDTH_OK.replace(
        "device_to_device_gib_per_s=712.400", "device_to_device_gib_per_s=0.1"
    )
    ctx = make_ctx(tmp_path, answers={"cudaprobe": (True, slow_internal, "")})

    assert CudaBandwidth().run(ctx).status == Status.PASS


# --- the bug that made all of the above pass while nothing ran -----------------


def test_the_detection_matches_what_the_collector_emits(tmp_path):
    """Through the real parsing path, not a dict written by hand.

    This is the test that was missing. ``nvidia_gpus`` read ``gpu["vendor"] == "nvidia"``, the
    fixtures said ``{"vendor": "nvidia"}``, and the two agreed with each other while neither agreed
    with the collector, which reports every name lspci gave under ``pci_ids`` and no flattened
    token at all. Every NVIDIA test skipped on a machine with an L40S in it, and a skip is not a
    failure, so the run passed and said nothing was wrong.

    Going through ``parse_lspci_vmm`` and the collector's own class filter means the fixture cannot
    drift from the producer again without this failing.
    """
    from alma_certify.inventory import pci as pci_mod
    from alma_certify.validate.nvidia import nvidia_gpus

    devices = [
        dev for dev in pci_mod.parse_lspci_vmm(LSPCI)
        if pci_mod.class_id(dev).startswith("03")
    ]
    assert len(devices) == 2, "the premise: an NVIDIA card and a Matrox display adapter"
    summary = {
        "gpus": [
            {
                "pci": dev.get("slot"),
                "pci_ids": {
                    name: dev[key]
                    for name, key in (("vendor", "vendor"), ("device", "device"),
                                      ("subsystem_vendor", "svendor"),
                                      ("subsystem_device", "sdevice"))
                    if dev.get(key)
                },
                "driver": dev.get("driver") or None,
            }
            for dev in devices
        ]
    }

    found = nvidia_gpus(summary)

    assert len(found) == 1
    assert found[0]["pci"] == "41:00.0"


def test_a_matrox_display_adapter_is_not_an_nvidia_gpu(tmp_path, toolchain):
    """The whole reason the detection has to be specific. Nearly every server has one of these."""
    from alma_certify.validate.nvidia import nvidia_gpus

    assert nvidia_gpus({"gpus": [BMC_ADAPTER]}) == []


def test_an_older_bundles_flattened_vendor_still_matches(tmp_path, toolchain):
    """A summary from a previous collector must not silently stop matching. One line of fallback is
    cheaper than a second way to be wrong."""
    from alma_certify.validate.nvidia import nvidia_gpus

    assert len(nvidia_gpus({"gpus": [LEGACY_NVIDIA_GPU]}) or []) == 1


def test_the_card_is_found_on_the_shape_the_tests_use(tmp_path, toolchain):
    """Ties the fixture to the code path: if ``NVIDIA_GPU`` is ever edited back into the flattened
    shape, the skip tests above would start passing for the wrong reason again."""
    from alma_certify.validate.nvidia import nvidia_gpus

    assert "pci_ids" in NVIDIA_GPU, "the fixture must carry the collector's real shape"
    assert nvidia_gpus({"gpus": [NVIDIA_GPU]}) == [NVIDIA_GPU]
    assert CudaDeviceQuery().applicable(make_ctx(tmp_path)) is None


# --- finding the compiler ------------------------------------------------------
#
# Reported: three tests skipped with "nvcc not present" on a host where `dnf install cuda-nvcc-13-3`
# had already succeeded. NVIDIA's RPMs install under a versioned prefix and put nothing on $PATH,
# and the suite runs as root from whatever context invoked it, so relying on the environment would
# make the skip depend on how somebody logged in.


def test_a_compiler_on_the_path_is_used_as_is(monkeypatch):
    monkeypatch.setattr(
        nvidia_mod.procutil, "find_tool", lambda tool, extra_dirs=(): "/usr/bin/nvcc",
    )

    assert nvidia_mod.find_nvcc() == "/usr/bin/nvcc"


def test_the_versioned_prefix_is_searched(monkeypatch, tmp_path):
    """The reported case. Nothing on PATH, and a real compiler under /usr/local/cuda-13.3."""
    monkeypatch.setattr(nvidia_mod.procutil, "find_tool", lambda tool, extra_dirs=(): None)
    nvcc = tmp_path / "usr/local/cuda-13.3/bin/nvcc"
    nvcc.parent.mkdir(parents=True)
    nvcc.write_text("#!/bin/sh\n")
    nvcc.chmod(0o755)
    monkeypatch.setattr(nvidia_mod, "_CUDA_PREFIXES", (str(tmp_path / "usr/local/cuda"),))

    assert nvidia_mod.find_nvcc() == str(nvcc)


def test_the_newest_toolkit_wins(monkeypatch, tmp_path):
    """Numerically. A lexical sort puts cuda-9.0 above cuda-13.3 and would pick a toolkit six major
    versions old on a machine that has both."""
    monkeypatch.setattr(nvidia_mod.procutil, "find_tool", lambda tool, extra_dirs=(): None)
    for version in ("9.0", "12.4", "13.3"):
        nvcc = tmp_path / ("usr/local/cuda-%s/bin/nvcc" % version)
        nvcc.parent.mkdir(parents=True)
        nvcc.write_text("#!/bin/sh\n")
        nvcc.chmod(0o755)
    monkeypatch.setattr(nvidia_mod, "_CUDA_PREFIXES", (str(tmp_path / "usr/local/cuda"),))

    assert nvidia_mod.find_nvcc().endswith("cuda-13.3/bin/nvcc")


def test_the_unversioned_symlink_is_preferred_over_a_versioned_one(monkeypatch, tmp_path):
    """NVIDIA's packages maintain it, and it is what their documentation tells people to put on
    PATH, so it is the machine's own answer to "which toolkit"."""
    monkeypatch.setattr(nvidia_mod.procutil, "find_tool", lambda tool, extra_dirs=(): None)
    for name in ("cuda", "cuda-12.4"):
        nvcc = tmp_path / ("usr/local/%s/bin/nvcc" % name)
        nvcc.parent.mkdir(parents=True)
        nvcc.write_text("#!/bin/sh\n")
        nvcc.chmod(0o755)
    monkeypatch.setattr(nvidia_mod, "_CUDA_PREFIXES", (str(tmp_path / "usr/local/cuda"),))

    assert nvidia_mod.find_nvcc().endswith("/cuda/bin/nvcc")


def test_cuda_home_is_honoured(monkeypatch, tmp_path):
    """The variable NVIDIA's own documentation uses. A machine with a hand-placed toolkit has no
    other way to say where it is."""
    monkeypatch.setattr(nvidia_mod.procutil, "find_tool", lambda tool, extra_dirs=(): None)
    nvcc = tmp_path / "opt/mycuda/bin/nvcc"
    nvcc.parent.mkdir(parents=True)
    nvcc.write_text("#!/bin/sh\n")
    nvcc.chmod(0o755)
    monkeypatch.setenv("CUDA_HOME", str(tmp_path / "opt/mycuda"))
    monkeypatch.setattr(nvidia_mod, "_CUDA_PREFIXES", ())

    assert nvidia_mod.find_nvcc() == str(nvcc)


def test_a_non_executable_file_is_not_a_compiler(monkeypatch, tmp_path):
    monkeypatch.setattr(nvidia_mod.procutil, "find_tool", lambda tool, extra_dirs=(): None)
    nvcc = tmp_path / "usr/local/cuda/bin/nvcc"
    nvcc.parent.mkdir(parents=True)
    nvcc.write_text("not a program\n")
    nvcc.chmod(0o644)
    monkeypatch.setattr(nvidia_mod, "_CUDA_PREFIXES", (str(tmp_path / "usr/local/cuda"),))

    assert nvidia_mod.find_nvcc() is None


def test_nothing_installed_is_still_a_skip(monkeypatch, tmp_path):
    """The driver is present and the toolkit is not, which is the ordinary state of a machine
    somebody has only just started setting up for GPU work."""
    monkeypatch.setattr(
        nvidia_mod.procutil, "find_tool",
        lambda tool, extra_dirs=(): None if tool == "nvcc" else "/usr/bin/" + tool,
    )
    monkeypatch.setattr(nvidia_mod, "_CUDA_PREFIXES", (str(tmp_path / "nowhere"),))
    monkeypatch.delenv("CUDA_HOME", raising=False)
    monkeypatch.delenv("CUDA_PATH", raising=False)

    assert nvidia_mod.find_nvcc() is None
    reason = CudaDeviceQuery().applicable(make_ctx(tmp_path))
    assert "no CUDA compiler found" in reason


# --- built for the card that is in the machine -----------------------------------
#
# Reported from AlmaLinux Kitten 10 x86_64: "the provided PTX was compiled with an unsupported
# toolchain". The kernel's kABI pins the driver a release back while cuda-toolkit stays current,
# and without -arch nvcc emits PTX that the older driver's JIT refuses at load time. Kitten aarch64
# was fine, because there the newest driver matched the kernel and the newest toolkit.


def _compiles(monkeypatch, *, ok=True, compute_cap="8.9\n", log=""):
    calls = []

    class Res:
        def __init__(self, ok, stdout="", stderr=""):
            self.ok, self.stdout, self.stderr, self.returncode = ok, stdout, stderr, 0 if ok else 1

    def cmd(argv, timeout=None, artifact=None, check=False):
        calls.append(argv)
        if "nvidia-smi" in argv[0]:
            return Res(compute_cap is not None, compute_cap or "")
        return Res(ok, "", log)

    monkeypatch.setattr(nvidia_mod, "find_nvcc", lambda: "/usr/local/cuda/bin/nvcc")
    return calls, cmd


def test_the_probe_is_built_for_the_installed_card(tmp_path, monkeypatch):
    calls, cmd = _compiles(monkeypatch)
    ctx = make_ctx(tmp_path)
    ctx.cmd = cmd

    binary, _log = nvidia_mod.compile_probe(ctx)

    assert binary is not None
    assert any("-arch=sm_89" in argv for argv in calls), calls


def test_a_card_the_toolkit_does_not_know_still_builds(tmp_path, monkeypatch):
    """A retry without -arch, so targeting the card can never be a new way to fail: a GPU newer
    than the toolkit would otherwise turn a working build into a broken one."""
    attempts = []

    class Res:
        def __init__(self, ok, stdout="", stderr=""):
            self.ok, self.stdout, self.stderr, self.returncode = ok, stdout, stderr, 0 if ok else 1

    def cmd(argv, timeout=None, artifact=None, check=False):
        if "nvidia-smi" in argv[0]:
            return Res(True, "12.0\n")
        attempts.append(argv)
        return Res("-arch=sm_120" not in argv, "", "nvcc fatal: Unsupported gpu architecture")

    monkeypatch.setattr(nvidia_mod, "find_nvcc", lambda: "/usr/local/cuda/bin/nvcc")
    ctx = make_ctx(tmp_path)
    ctx.cmd = cmd

    binary, _log = nvidia_mod.compile_probe(ctx)

    assert binary is not None, "gave up instead of retrying without -arch"
    assert len(attempts) == 2


def test_a_machine_that_cannot_report_its_arch_still_builds(tmp_path, monkeypatch):
    calls, cmd = _compiles(monkeypatch, compute_cap=None)
    ctx = make_ctx(tmp_path)
    ctx.cmd = cmd

    binary, _log = nvidia_mod.compile_probe(ctx)

    assert binary is not None
    assert not any(any("-arch" in part for part in argv) for argv in calls)


def test_nvidia_smi_output_that_is_not_a_capability_is_not_turned_into_an_arch(tmp_path,
                                                                                monkeypatch):
    """An error on stdout rather than a number, which is what a failing query looks like."""
    calls, cmd = _compiles(monkeypatch, compute_cap="Unable to determine the device handle\n")
    ctx = make_ctx(tmp_path)
    ctx.cmd = cmd

    nvidia_mod.compile_probe(ctx)

    assert not any(any("-arch" in part for part in argv) for argv in calls)


def test_an_unparseable_compute_cap_is_not_turned_into_an_arch(tmp_path, monkeypatch):
    calls, cmd = _compiles(monkeypatch, compute_cap="N/A\n")
    ctx = make_ctx(tmp_path)
    ctx.cmd = cmd

    nvidia_mod.compile_probe(ctx)

    assert not any(any("-arch" in part for part in argv) for argv in calls)


PTX = ("error=the provided PTX was compiled with an unsupported toolchain\n")


@pytest.mark.parametrize("cls", [CudaDeviceQuery, CudaVectorAdd, CudaBandwidth])
def test_a_toolkit_newer_than_the_driver_skips_rather_than_fails(cls, tmp_path, toolchain,
                                                                 compiles):
    """The card was never reached: the driver refused to compile the binary's PTX. A FAIL there
    says the hardware is defective, which is not what happened."""
    ctx = make_ctx(tmp_path, answers={"cudaprobe": (False, PTX, "")})

    result = cls().run(ctx)

    assert result.status == Status.SKIP
    assert "newer than the driver can accept" in result.reason


def test_the_skip_says_which_half_to_move(tmp_path, toolchain, compiles):
    ctx = make_ctx(tmp_path, answers={"cudaprobe": (False, PTX, "")})

    reason = CudaDeviceQuery().run(ctx).reason

    assert "toolkit" in reason and "driver" in reason


def test_an_ordinary_probe_failure_is_still_a_failure(tmp_path, toolchain, compiles):
    """The classification has to be narrow: a card that computes the wrong answer is a finding."""
    ctx = make_ctx(tmp_path, answers={"cudaprobe": (False, "error=device-side assert\n", "")})

    assert CudaDeviceQuery().run(ctx).status == Status.FAIL
