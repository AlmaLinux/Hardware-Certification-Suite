"""OpenCL and Vulkan validation: when it runs, when it refuses, and what it will not call a pass.

The suite proved a card computes only through CUDA, so an AMD or Intel card could be certified with
its accelerator never having been asked to do anything. Reported from a machine with an Intel UHD
630, where the validation side had nothing at all to say about the card.

The most valuable tests here are the refusals. Two of them:

- **No ICD is a packaging fact, not a defect.** Nearly every server has a Matrox or ASPEED BMC
  display adapter and no OpenCL, and failing certification for that would condemn working machines.
- **A software implementation is not hardware.** ``mesa-vulkan-drivers`` installs lavapipe, a
  complete Vulkan implementation on the CPU; pocl and rusticl-on-llvmpipe do the same for OpenCL.
  A check that took the first device it found would certify a server with no accelerator at all. On
  the machine these probes were written on, Vulkan enumerated an AMD integrated GPU, an Intel Arc
  A380, **and llvmpipe**.
"""

import os

import pytest

from alma_certify.config import Config
from alma_certify.registry import REGISTRY, RunContext, load_all_tests
from alma_certify.result import Severity, Status
from alma_certify.validate import gpuapi

# Real output, captured from the probes running against an Intel Arc A380 and an AMD Raphael iGPU.
OPENCL_DEVICES = """\
platform_count=1
device.0.platform=Intel(R) OpenCL Graphics
device.0.type=gpu
device.0.name=Intel(R) Arc(TM) A380 Graphics
device.0.vendor=Intel(R) Corporation
device.0.version=OpenCL 3.0 NEO
device.0.driver_version=26.22.38646.6
device.0.compute_units=128
device.0.global_memory_mib=5783
device.0.max_work_group_size=1024
device_count=1
accelerated_device_count=1
"""

# The trap, also real: three devices, one of which is a software rasterizer.
VULKAN_DEVICES = """\
device.0.name=AMD Ryzen 9 7950X 16-Core Processor (RADV RAPHAEL_MENDOCINO)
device.0.type=integrated
device.0.api_version=1.4.318
device.0.queue_family=0
device.1.name=Intel(R) Arc(tm) A380 Graphics (DG2)
device.1.type=discrete
device.1.api_version=1.4.318
device.1.queue_family=0
device.2.name=llvmpipe (LLVM 21.1.8, 256 bits)
device.2.type=cpu
device.2.queue_family=0
device_count=3
accelerated_device_count=2
"""

SOFTWARE_ONLY = """\
device.0.name=llvmpipe (LLVM 21.1.8, 256 bits)
device.0.type=cpu
device.0.queue_family=0
device_count=1
accelerated_device_count=0
"""

GPU = {"pci_ids": {"vendor": "Intel Corporation [8086]", "device": "UHD Graphics 630 [3e92]"},
       "driver": "i915"}
MATROX = {"pci_ids": {"vendor": "Matrox Electronics Systems Ltd. [102b]",
                      "device": "Integrated Matrox G200eW3 [0536]"}, "driver": "mgag200"}
# An integrated AMD GPU, as a Ryzen mobile APU reports: PCI vendor 1002, no driver of its own to
# bring a Vulkan ICD. This is the card that got no compute coverage in the report.
AMD_GPU = {"pci_ids": {"vendor": "Advanced Micro Devices, Inc. [AMD/ATI] [1002]",
                       "device": "Phoenix1 [Radeon 780M] [15bf]"}, "driver": "amdgpu"}
NVIDIA_GPU = {"pci_ids": {"vendor": "NVIDIA Corporation [10de]", "device": "AD102 [L40S] [26b9]"},
              "driver": "nvidia"}


class Res:
    def __init__(self, ok=True, stdout="", returncode=0):
        self.ok, self.stdout, self.stderr, self.returncode = ok, stdout, "", returncode
        self.timed_out = False


class Pkg:
    """Stands in for ``alma_certify.pkg.PackageManager``.

    ``missing`` is the primitive and ``ensure`` delegates to it, exactly as the real class does.
    This carried only ``ensure`` and so could not represent the case the code now reports on: some
    of the packages installed and some did not. That is the ordinary case on AlmaLinux 8, where two
    of clpeak's eight build dependencies are not packaged at all.
    """

    def __init__(self, ok=True, unavailable=()):
        self.ok = ok
        # Names this machine cannot supply, whatever ``ok`` says. Empty means ``ok`` decides for
        # every package, which is what every existing test wants.
        self.unavailable = set(unavailable)
        self.asked = []

    def missing(self, packages, repos=(), timeout=900):
        self.asked.append((tuple(packages), tuple(repos)))
        if self.unavailable:
            return [p for p in packages if p in self.unavailable]
        return [] if self.ok else list(packages)

    def ensure(self, packages, repos=(), timeout=900):
        return not self.missing(packages, repos=repos, timeout=timeout)


@pytest.fixture
def ctx(tmp_path):
    return RunContext(
        Config.load("/nonexistent"), str(tmp_path),
        inventory={"summary": {"gpus": [GPU]}}, pkg=Pkg(),
    )


@pytest.fixture
def ready(monkeypatch, tmp_path):
    """A machine with both ICDs, a compiler, and a probe that builds."""
    monkeypatch.setattr(gpuapi, "opencl_icds", lambda: ["/etc/OpenCL/vendors/intel.icd"])
    monkeypatch.setattr(gpuapi, "vulkan_icds", lambda: ["/usr/share/vulkan/icd.d/intel_icd.json"])
    monkeypatch.setattr(
        gpuapi.procutil, "find_tool", lambda tool, extra_dirs=(): "/usr/bin/" + tool,
    )
    # The base class reads the ICD list through a staticmethod captured at class definition, so the
    # patches above have to reach the classes too.
    monkeypatch.setattr(gpuapi._OpenClTest, "icds", staticmethod(gpuapi.opencl_icds))
    monkeypatch.setattr(gpuapi._VulkanTest, "icds", staticmethod(gpuapi.vulkan_icds))
    return tmp_path


def outputs(monkeypatch, ctx, **by_mode):
    """Make the probe build, and answer each mode with the given text."""
    def cmd(argv, timeout=None, artifact=None):
        if argv[0].endswith(("cc", "gcc")):
            # The compile. Produce the binary it was asked for, so ``build`` sees it.
            open(argv[argv.index("-o") + 1], "w").close()
            return Res()
        mode = argv[-1]
        text = by_mode.get(mode, "")
        return Res(ok=by_mode.get(mode + "_ok", True), stdout=text)

    monkeypatch.setattr(ctx, "cmd", cmd)


# --- when it refuses -------------------------------------------------------------


@pytest.mark.parametrize("test_class", [
    gpuapi.OpenClDevices, gpuapi.OpenClVectorAdd, gpuapi.VulkanDevices, gpuapi.VulkanFill,
])
def test_no_icd_is_a_skip_and_says_what_is_missing(monkeypatch, ctx, test_class):
    """Not a failure. A server whose only display adapter is a BMC chip has no OpenCL and no
    business failing certification for it."""
    monkeypatch.setattr(gpuapi._OpenClTest, "icds", staticmethod(lambda: []))
    monkeypatch.setattr(gpuapi._VulkanTest, "icds", staticmethod(lambda: []))

    reason = test_class().applicable(ctx)

    assert reason is not None
    assert "no %s driver is installed" % test_class.label in reason


def test_a_machine_with_no_gpu_at_all_is_skipped(monkeypatch, ready, tmp_path):
    empty = RunContext(Config.load("/nonexistent"), str(tmp_path),
                       inventory={"summary": {"gpus": []}}, pkg=Pkg())

    assert gpuapi.VulkanDevices().applicable(empty) == "no GPU in the inventory"


def test_a_management_adapter_is_still_asked_about(monkeypatch, ready, tmp_path):
    """A Matrox BMC chip is a GPU in the inventory, so the ICD gate is what excuses it rather than
    a guess about which cards are 'real'. With no ICD installed it skips, and the suite never
    pretends to know which display adapters deserve a compute runtime."""
    bmc = RunContext(Config.load("/nonexistent"), str(tmp_path),
                     inventory={"summary": {"gpus": [MATROX]}}, pkg=Pkg())

    assert gpuapi.VulkanDevices().applicable(bmc) is None, "the ICD decides, not the card"

    monkeypatch.setattr(gpuapi._VulkanTest, "icds", staticmethod(lambda: []))
    assert "no Vulkan driver is installed" in gpuapi.VulkanDevices().applicable(bmc)


# --- installing a runtime for a card that came with none -------------------------
#
# Reported from an AMD integrated GPU on which no compute test ran and nothing was installed: the
# suite set up NVIDIA and Intel OpenCL, but never gave an AMD card a Vulkan driver to enumerate.


def _amd_ctx(tmp_path, pkg):
    return RunContext(Config.load("/nonexistent"), str(tmp_path),
                      inventory={"summary": {"gpus": [AMD_GPU]}}, pkg=pkg)


def test_an_amd_gpu_gets_a_vulkan_runtime_then_the_test_runs(monkeypatch, tmp_path):
    """The whole point: with no Vulkan ICD, the gate would skip; installing mesa's radv drops one,
    so the card is enumerated instead of being silently passed over."""
    monkeypatch.setattr(gpuapi.procutil, "find_tool",
                        lambda tool, extra_dirs=(): "/usr/bin/" + tool)
    # _DATA_DIR is left real: vulkanprobe.c ships there, so applicable's "is the probe source
    # present" check passes and the only thing standing between skip and run is the ICD.
    installed = []
    monkeypatch.setattr(gpuapi._VulkanTest, "icds", staticmethod(lambda: list(installed)))

    class InstallingPkg(Pkg):
        def ensure(self, packages, repos=(), timeout=900):
            self.asked.append((tuple(packages), tuple(repos)))
            if "mesa-vulkan-drivers" in packages:
                installed.append("/usr/share/vulkan/icd.d/radeon_icd.x86_64.json")
            return True

    ctx = _amd_ctx(tmp_path, InstallingPkg())
    reason = gpuapi.VulkanDevices().applicable(ctx)

    assert reason is None, "with the runtime installed the card should be testable, not skipped"
    wanted = [pkgs for pkgs, _ in ctx.pkg.asked if "mesa-vulkan-drivers" in pkgs]
    assert wanted, "the AMD Vulkan runtime was never installed"
    repos = next(r for pkgs, r in ctx.pkg.asked if "mesa-vulkan-drivers" in pkgs)
    assert "vulkan-loader" in wanted[0]
    assert repos == ("appstream",), "mesa-vulkan-drivers is in AppStream, not CRB"


def test_the_runtime_is_not_installed_when_an_icd_is_already_there(monkeypatch, tmp_path):
    """A card with a working Vulkan driver already, NVIDIA's being the usual one, is left alone."""
    monkeypatch.setattr(gpuapi.procutil, "find_tool",
                        lambda tool, extra_dirs=(): "/usr/bin/" + tool)
    monkeypatch.setattr(gpuapi._VulkanTest, "icds",
                        staticmethod(lambda: ["/usr/share/vulkan/icd.d/radeon_icd.json"]))
    ctx = _amd_ctx(tmp_path, Pkg())

    gpuapi.VulkanDevices().applicable(ctx)

    assert not any("mesa-vulkan-drivers" in pkgs for pkgs, _ in ctx.pkg.asked)


def test_an_nvidia_gpu_does_not_pull_in_mesa(monkeypatch, tmp_path):
    """mesa's radv is for the cards that have no driver of their own. NVIDIA brings its Vulkan ICD
    with the driver, so installing mesa there would be installing something to sit unused."""
    monkeypatch.setattr(gpuapi._VulkanTest, "icds", staticmethod(lambda: []))
    ctx = RunContext(Config.load("/nonexistent"), str(tmp_path),
                     inventory={"summary": {"gpus": [NVIDIA_GPU]}}, pkg=Pkg())

    gpuapi.VulkanDevices().applicable(ctx)

    assert not any("mesa-vulkan-drivers" in pkgs for pkgs, _ in ctx.pkg.asked)


def test_opencl_does_not_auto_install_a_runtime_for_amd(monkeypatch, tmp_path):
    """OpenCL is left as it was: no AMD OpenCL runtime is installed automatically, because the only
    one is ROCm and whether to reach for it is a separate decision. So an AMD card with no OpenCL
    ICD still skips OpenCL rather than having a stack installed for it."""
    monkeypatch.setattr(gpuapi._OpenClTest, "icds", staticmethod(lambda: []))
    ctx = _amd_ctx(tmp_path, Pkg())

    reason = gpuapi.OpenClDevices().applicable(ctx)

    assert "no OpenCL driver is installed" in reason
    assert not any("rocm" in p.lower() for pkgs, _ in ctx.pkg.asked for p in pkgs)


def test_no_compiler_is_a_skip(monkeypatch, ctx, ready):
    monkeypatch.setattr(gpuapi.procutil, "find_tool", lambda tool, extra_dirs=(): None)

    assert "no C compiler" in gpuapi.OpenClDevices().applicable(ctx)


def test_a_missing_probe_source_is_not_the_machines_fault(monkeypatch, ctx, ready):
    monkeypatch.setattr(gpuapi, "_DATA_DIR", "/nonexistent")

    reason = gpuapi.VulkanFill().applicable(ctx)

    assert "vulkanprobe.c" in reason
    assert "Nothing about this machine" in reason


def test_the_build_dependencies_are_named_when_they_cannot_be_installed(monkeypatch, ctx, ready):
    ctx.pkg = Pkg(ok=False)

    reason = gpuapi.OpenClDevices().applicable(ctx)

    assert "ocl-icd-devel" in reason


def test_only_the_packages_that_are_really_missing_are_named(monkeypatch, ctx, ready):
    """Reported from an AlmaLinux 8 machine: the run said it could not install a list that included
    ``cmake``, the reader went and found cmake sitting in AppStream, and had no way to tell which
    name in the list was the real problem. ``pkg.missing`` returns exactly the absent ones, which is
    why it returns a list at all."""
    ctx.pkg = Pkg(unavailable={"ocl-icd-devel"})

    reason = gpuapi.OpenClDevices().applicable(ctx)

    assert "ocl-icd-devel" in reason
    assert "opencl-headers" not in reason


def test_the_link_library_comes_from_crb_for_opencl_only(monkeypatch, ctx, ready):
    """``ocl-icd-devel`` carries the link-time libOpenCL.so and lives in CRB, which is disabled by
    default. Vulkan's headers and loader are both in AppStream."""
    for test_class in (gpuapi.OpenClDevices, gpuapi.VulkanDevices):
        pkg = Pkg()
        ctx.pkg = pkg
        test_class().applicable(ctx)
        wanted, repos = pkg.asked[-1]
        if test_class is gpuapi.OpenClDevices:
            assert "ocl-icd-devel" in wanted and repos == ("crb",)
        else:
            assert "vulkan-loader-devel" in wanted and repos == ()


# --- the software-implementation trap --------------------------------------------


@pytest.mark.parametrize("test_class,mode", [
    (gpuapi.OpenClDevices, "devices"), (gpuapi.VulkanDevices, "devices"),
])
def test_a_software_only_runtime_is_skipped_not_passed(monkeypatch, ctx, ready, test_class, mode):
    """lavapipe answering for a machine with no accelerator is lavapipe working correctly.
    Calling it a pass would certify a server that has no GPU."""
    outputs(monkeypatch, ctx, devices=SOFTWARE_ONLY)

    result = test_class().run(ctx)

    assert result.status == Status.SKIP
    assert "software implementations" in result.reason


def test_a_real_device_beside_a_software_one_still_passes(monkeypatch, ctx, ready):
    """The real machine this was written on: an AMD iGPU, an Intel Arc, and llvmpipe. Two hardware
    devices, and the count has to say two."""
    outputs(monkeypatch, ctx, devices=VULKAN_DEVICES)

    result = gpuapi.VulkanDevices().run(ctx)

    assert result.status == Status.PASS
    assert "2 hardware device(s)" in result.reason
    assert "llvmpipe" not in result.reason
    # And the raw enumeration is kept, llvmpipe included, because a reviewer may want to know it was
    # there.
    assert "llvmpipe" in result.details["probe"]["device.2.name"]


def test_the_accelerated_count_is_derived_from_the_device_types():
    """Read from the per-device types rather than trusting the probe's own tally, so the two can
    disagree and be caught."""
    values = gpuapi.parse_probe(VULKAN_DEVICES)

    assert gpuapi.accelerated_devices(values) == [
        "AMD Ryzen 9 7950X 16-Core Processor (RADV RAPHAEL_MENDOCINO)",
        "Intel(R) Arc(tm) A380 Graphics (DG2)",
    ]
    assert values["accelerated_device_count"] == "2", "the probe's own tally should agree"


@pytest.mark.parametrize("device_type,accelerated", [
    ("gpu", True), ("accelerator", True), ("discrete", True), ("integrated", True),
    ("virtual", True), ("cpu", False), ("other", False),
])
def test_which_device_types_count_as_hardware(device_type, accelerated):
    values = {"device.0.type": device_type, "device.0.name": "something"}

    assert bool(gpuapi.accelerated_devices(values)) is accelerated


# --- what it reports on a working machine ----------------------------------------


def test_opencl_enumeration_records_what_the_driver_said(monkeypatch, ctx, ready):
    outputs(monkeypatch, ctx, devices=OPENCL_DEVICES)

    result = gpuapi.OpenClDevices().run(ctx)

    assert result.status == Status.PASS
    assert "Intel(R) Arc(TM) A380 Graphics" in result.reason
    probe = result.details["probe"]
    assert probe["device.0.driver_version"] == "26.22.38646.6"
    assert probe["device.0.compute_units"] == "128"


def test_a_correct_computation_passes_and_says_how_much_was_checked(monkeypatch, ctx, ready):
    outputs(monkeypatch, ctx, vectoradd=(
        "device=Intel(R) Arc(TM) A380 Graphics\nelements=4194304\nmismatches=0\n"
        "worst_absolute_error=0\n"
    ))

    result = gpuapi.OpenClVectorAdd().run(ctx)

    assert result.status == Status.PASS
    assert "4194304 elements" in result.reason


def test_a_wrong_answer_fails_and_says_how_wrong(monkeypatch, ctx, ready):
    """The one check that can catch hardware that runs and is wrong."""
    outputs(monkeypatch, ctx, vectoradd=(
        "device=A Card\nelements=4194304\nmismatches=17\nworst_absolute_error=3.5\n"
    ))

    result = gpuapi.OpenClVectorAdd().run(ctx)

    assert result.status == Status.FAIL
    assert "17 of 4194304 elements were wrong" in result.reason


def test_a_vulkan_fill_that_comes_back_wrong_fails(monkeypatch, ctx, ready):
    outputs(monkeypatch, ctx, fill=(
        "device=A Card\ndevice_type=discrete\nbytes=16777216\nwords=4194304\nmismatches=9\n"
    ))

    result = gpuapi.VulkanFill().run(ctx)

    assert result.status == Status.FAIL
    assert "9 words of 4194304 were wrong" in result.reason


def test_a_probe_that_exits_non_zero_reports_its_own_reason(monkeypatch, ctx, ready):
    outputs(monkeypatch, ctx, fill="error=the device did not finish a 16 MiB buffer fill\n",
            fill_ok=False)

    result = gpuapi.VulkanFill().run(ctx)

    assert result.status == Status.FAIL
    assert "did not finish" in result.reason


def test_a_probe_that_says_nothing_is_an_error_not_a_pass(monkeypatch, ctx, ready):
    """Silence is not success. A run that produced no result cannot be evidence either way."""
    outputs(monkeypatch, ctx, fill="")

    result = gpuapi.VulkanFill().run(ctx)

    assert result.status == Status.ERROR
    assert "reported no result" in result.reason


def test_a_toolchain_that_cannot_compile_is_an_error(monkeypatch, ctx, ready):
    """The headers and the library are installed, so this is a broken toolchain rather than an
    absent one, and that is the machine's problem rather than a skip."""
    def cmd(argv, timeout=None, artifact=None):
        return Res(ok=False, returncode=1)

    monkeypatch.setattr(ctx, "cmd", cmd)

    result = gpuapi.OpenClDevices().run(ctx)

    assert result.status == Status.ERROR
    assert "broken rather than absent" in result.reason


# --- how it sits in the suite ----------------------------------------------------


def test_the_tests_are_registered_and_required():
    load_all_tests()
    registered = {cls.id: cls for cls in REGISTRY.all()}

    for test_id in ("validate.gpu.opencl-devices", "validate.gpu.opencl-compute",
                    "validate.gpu.vulkan-devices", "validate.gpu.vulkan-compute"):
        assert test_id in registered, test_id
        assert registered[test_id].category == "gpu"
        # Required, on the same reasoning as the CUDA tests: everything only runs when a GPU is
        # present *and* a hardware-backed runtime for it is installed, which together mean somebody
        # set this machine up to do that work.
        assert registered[test_id].severity == Severity.REQUIRED


def test_both_probe_sources_ship_in_the_package():
    for name in ("openclprobe.c", "vulkanprobe.c"):
        assert os.path.isfile(os.path.join(gpuapi._DATA_DIR, name)), name


def test_the_probes_are_c_and_link_the_right_library():
    assert gpuapi._OpenClTest.library == "OpenCL"
    assert gpuapi._VulkanTest.library == "vulkan"
    assert gpuapi._OpenClTest.source.endswith(".c")
    assert gpuapi._VulkanTest.source.endswith(".c")


def test_the_icd_paths_are_the_ones_the_packages_use(monkeypatch):
    """Confirmed by installing the packages in a container: ``intel-opencl`` drops
    /etc/OpenCL/vendors/intel.icd and ``mesa-vulkan-drivers`` drops
    /usr/share/vulkan/icd.d/intel_icd.x86_64.json."""
    seen = []
    monkeypatch.setattr(gpuapi.glob, "glob", lambda pattern: seen.append(pattern) or [])

    gpuapi.opencl_icds()
    gpuapi.vulkan_icds()

    assert seen == ["/etc/OpenCL/vendors/*.icd", "/usr/share/vulkan/icd.d/*.json"]


# --- unsupported hardware is not broken hardware ---------------------------------
#
# Reported from an Intel UHD 630 on AlmaLinux 10. ``intel-opencl`` exists for the vendor, so the ICD
# is installed and the gate lets the test run; the driver in that release does not support that
# generation of card, so it loads, claims nothing, and the card has no OpenCL. The first version
# called that a FAIL, and because these tests are REQUIRED it failed the whole validate over a
# packaging fact about somebody else's driver.


NO_DEVICE = "platform_count=1\ndevice_count=0\naccelerated_device_count=0\n"
NO_PLATFORM = "platform_count=0\ndevice_count=0\naccelerated_device_count=0\n"


@pytest.mark.parametrize("test_class,mode", [
    (gpuapi.OpenClDevices, "devices"), (gpuapi.VulkanDevices, "devices"),
])
def test_a_runtime_that_claims_no_device_is_a_skip(monkeypatch, ctx, ready, test_class, mode):
    """The UHD 630 case. The card is not at fault and must not be condemned for it."""
    outputs(monkeypatch, ctx, devices=NO_DEVICE)

    result = test_class().run(ctx)

    assert result.status == Status.SKIP
    assert "no device claims this hardware" in result.reason
    assert "Not a defect in the card" in result.reason


def test_a_driver_that_does_not_load_at_all_is_also_a_skip(monkeypatch, ctx, ready):
    """An ICD installed with no platform behind it is a packaging problem, not a hardware one."""
    outputs(monkeypatch, ctx, devices=NO_PLATFORM)

    result = gpuapi.OpenClDevices().run(ctx)

    assert result.status == Status.SKIP


@pytest.mark.parametrize("test_class,mode", [
    (gpuapi.OpenClVectorAdd, "vectoradd"), (gpuapi.VulkanFill, "fill"),
])
def test_a_compute_test_with_nothing_to_run_on_is_a_skip(monkeypatch, ctx, ready, test_class, mode):
    """The compute probes exit non-zero when there is no device, because they could not do the
    work. An exit code alone cannot tell "nothing here" from "went wrong", so the count is read
    first."""
    outputs(monkeypatch, ctx, **{
        mode: "accelerated_device_count=0\nerror=no device to run on\n", mode + "_ok": False,
    })

    result = test_class().run(ctx)

    assert result.status == Status.SKIP
    assert "no device claims this hardware" in result.reason


def test_a_device_that_is_there_and_wrong_still_fails(monkeypatch, ctx, ready):
    """The line this draws. A runtime that claims the hardware and computes the wrong answer is a
    defect, and none of the above may soften that."""
    outputs(monkeypatch, ctx, vectoradd=(
        "device=A Card\naccelerated_device_count=1\nelements=1024\nmismatches=5\n"
    ))

    result = gpuapi.OpenClVectorAdd().run(ctx)

    assert result.status == Status.FAIL


def test_a_runtime_error_on_a_claimed_device_still_fails(monkeypatch, ctx, ready):
    outputs(monkeypatch, ctx, fill=(
        "device=A Card\naccelerated_device_count=1\n"
        "error=vkQueueSubmit failed with VkResult -4\n"
    ), fill_ok=False)

    result = gpuapi.VulkanFill().run(ctx)

    assert result.status == Status.FAIL
    assert "vkQueueSubmit" in result.reason


def test_the_names_read_properly_in_a_sentence():
    """``api`` names files and log keys; ``label`` goes in sentences. One variable doing both jobs
    produced "an vulkan driver is installed"."""
    assert gpuapi._OpenClTest.label == "OpenCL"
    assert gpuapi._VulkanTest.label == "Vulkan"
    for test_class in (gpuapi._OpenClTest, gpuapi._VulkanTest):
        assert test_class.api == test_class.api.lower()
        assert test_class.label != test_class.api


# Names whose article does not follow their first letter, because the article follows how the name
# is said. Empty today, and here so that adding one is a deliberate entry rather than a typo that
# reads as a typo on somebody's screen.
_SPOKEN_UNLIKE_SPELLED = {}


@pytest.mark.parametrize("test_class", [gpuapi._OpenClTest, gpuapi._VulkanTest])
def test_the_article_matches_the_name_it_precedes(test_class):
    """The other half of the same bug. Separating ``api`` from ``label`` fixed "an vulkan" and left
    "a OpenCL driver is installed", which then reached a report from an Intel UHD 630."""
    expected = _SPOKEN_UNLIKE_SPELLED.get(
        test_class.label, "an" if test_class.label[0] in "AEIOU" else "a"
    )

    assert test_class.article == expected, (
        "%s reads as \"%s %s\"" % (test_class.__name__, test_class.article, test_class.label)
    )


@pytest.mark.parametrize("test_class,article", [
    (gpuapi.OpenClDevices, "an OpenCL driver is installed"),
    (gpuapi.VulkanDevices, "a Vulkan driver is installed"),
])
def test_the_skip_a_user_reads_is_grammatical(monkeypatch, ctx, ready, test_class, article):
    """Through the real code path rather than off the class, because the sentence is what shipped:
    the article and the label are interpolated separately and could disagree in either direction."""
    outputs(monkeypatch, ctx, devices=NO_DEVICE)

    reason = test_class().run(ctx).reason

    assert reason.startswith(article), reason
