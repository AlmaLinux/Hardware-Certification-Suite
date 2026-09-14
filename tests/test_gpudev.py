"""Choosing which GPUs clpeak benchmarks: enumeration, dedupe-by-default, and --all-gpus.

The parsing is of ``clpeak --list-devices`` text, so the tests pin the exact layout and, just as
importantly, that anything unreadable falls back to running every device rather than guessing.
"""

from alma_certify import gpudev

# One physical layout across three backends: OpenCL sees the NVIDIA card via its ICD, CUDA sees two
# identical L40S, Vulkan sees an Intel iGPU and one of the NVIDIA cards. The trailing "[type]" is
# clpeak's, appended after the device name.
LIST_OUTPUT = """
=== OpenCL backend ===
Platform 0: NVIDIA CUDA
  Device 0: NVIDIA L40S [GPU]

=== CUDA backend ===
  CUDA Device 0: NVIDIA L40S [Discrete GPU]
  CUDA Device 1: NVIDIA L40S [Discrete GPU]

=== Vulkan backend ===
  Vulkan Device 0: Intel(R) UHD Graphics 630 [Integrated GPU]
  Vulkan Device 1: NVIDIA L40S [Discrete GPU]
"""


class _Res:
    def __init__(self, ok=True, stdout="", returncode=0):
        self.ok, self.stdout, self.returncode = ok, stdout, returncode


def _run(stdout, ok=True):
    def run(argv):
        assert argv[1] == "--list-devices"
        return _Res(ok=ok, stdout=stdout)
    return run


def test_list_devices_parses_each_backend_with_indices():
    devices = gpudev.list_devices("clpeak", _run(LIST_OUTPUT))

    assert devices["cuda"] == [
        (0, "NVIDIA L40S [Discrete GPU]"), (1, "NVIDIA L40S [Discrete GPU]"),
    ]
    assert devices["vulkan"] == [
        (0, "Intel(R) UHD Graphics 630 [Integrated GPU]"), (1, "NVIDIA L40S [Discrete GPU]"),
    ]
    assert devices["opencl"] == [(0, "NVIDIA L40S [GPU]")]


def test_an_unreadable_enumeration_is_empty():
    assert gpudev.list_devices("clpeak", _run("", ok=False)) == {}
    assert gpudev.list_devices("clpeak", _run("")) == {}

    def boom(argv):
        raise RuntimeError("clpeak is not here")

    assert gpudev.list_devices("clpeak", boom) == {}


def test_default_benchmarks_one_of_each_identical_card():
    plan = gpudev.plan("clpeak", _run(LIST_OUTPUT), ["cuda", "vulkan"], all_gpus=False)

    # Two identical CUDA cards: one invocation pinned to the first index only.
    assert plan["cuda"] == [gpudev.Invocation(["--cuda-device", "0"], None)]
    # Two distinct Vulkan devices: one invocation pinned to both.
    assert plan["vulkan"] == [gpudev.Invocation(["--vk-device", "0,1"], None)]


def test_all_gpus_runs_every_card_with_an_ordinal():
    plan = gpudev.plan("clpeak", _run(LIST_OUTPUT), ["cuda"], all_gpus=True)

    # Each identical card its own invocation, ordinal 0 then 1, so they submit individually.
    assert plan["cuda"] == [
        gpudev.Invocation(["--cuda-device", "0"], 0),
        gpudev.Invocation(["--cuda-device", "1"], 1),
    ]


def test_opencl_is_never_pinned():
    """OpenCL's device index is platform-relative, not a flat ordinal, so it always runs every
    device (one unpinned invocation) - correct, just not deduped. Same in both modes."""
    default = gpudev.plan("clpeak", _run(LIST_OUTPUT), ["opencl"], all_gpus=False)
    every = gpudev.plan("clpeak", _run(LIST_OUTPUT), ["opencl"], all_gpus=True)

    assert default["opencl"] == [gpudev.Invocation([], None)]
    assert every["opencl"] == [gpudev.Invocation([], None)]


def test_an_unenumerable_backend_falls_back_to_running_every_device():
    plan = gpudev.plan("clpeak", _run("", ok=False), ["cuda", "vulkan"], all_gpus=False)

    assert plan["cuda"] == [gpudev.Invocation([], None)]
    assert plan["vulkan"] == [gpudev.Invocation([], None)]


# --- leaving software rasterizers out --------------------------------------------
#
# lavapipe (Vulkan) and rusticl/pocl-on-llvmpipe (OpenCL) are CPU implementations clpeak enumerates
# like any device. Benchmarking one records a CPU's compute as a graphics card's; "llvmpipe" turning
# up in the GPU catalog is exactly that. They are left out of the plan so a pinnable backend never
# selects one; the curated figures drop them on the backends that cannot be pinned.

LIST_WITH_LLVMPIPE = """
=== Vulkan backend ===
  Vulkan Device 0: llvmpipe (LLVM 17.0.6, 256 bits) [CPU]
  Vulkan Device 1: AMD Radeon Graphics (RADV PHOENIX) [Integrated GPU]
"""

# A CPU OpenCL runtime (pocl, or Intel's) reports the bare CPU brand string as its device name -
# no software marker in it at all - and is recognizable only by the [CPU] type clpeak's
# --list-devices appends. A real GPU shares the OpenCL platform.
LIST_WITH_CPU_RUNTIME = """
=== OpenCL backend ===
Platform 0: Portable Computing Language
  Device 0: cpu-haswell-Intel(R) Xeon(R) Gold 6430 [CPU]
Platform 1: rusticl
  Device 0: AMD Radeon Graphics (RADV PHOENIX) [GPU]
"""


def test_is_software_device_tells_a_rasterizer_from_a_gpu():
    # By name, the software rasterizers, with or without the enumeration's [type] suffix.
    assert gpudev.is_software_device("llvmpipe (LLVM 17.0.6, 256 bits) [CPU]")
    assert gpudev.is_software_device("llvmpipe (LLVM 17.0.6, 256 bits)")
    assert gpudev.is_software_device("SwiftShader Device (LLVM 10)")
    # By type, a CPU runtime whose name carries no marker at all - the pocl / Intel-runtime case.
    assert gpudev.is_software_device("cpu-haswell-Intel(R) Xeon(R) Gold 6430 [CPU]")
    assert not gpudev.is_software_device("AMD Radeon Graphics (RADV PHOENIX) [Integrated GPU]")
    assert not gpudev.is_software_device("NVIDIA L40S [Discrete GPU]")
    # Without the type suffix a bare CPU brand cannot be told from a GPU by name; that is the gap
    # software_device_names closes by cross-referencing the enumeration.
    assert not gpudev.is_software_device("Intel(R) Xeon(R) Gold 6430")
    assert not gpudev.is_software_device("")


def test_list_devices_leaves_out_a_software_rasterizer():
    """llvmpipe is not enumerated, and the real card keeps its own clpeak index, so pinning still
    selects the right device rather than a shifted one."""
    devices = gpudev.list_devices("clpeak", _run(LIST_WITH_LLVMPIPE))

    assert devices["vulkan"] == [(1, "AMD Radeon Graphics (RADV PHOENIX) [Integrated GPU]")]


def test_plan_pins_the_real_card_not_the_rasterizer():
    plan = gpudev.plan("clpeak", _run(LIST_WITH_LLVMPIPE), ["vulkan"], all_gpus=False)

    # Pinned to index 1, the real GPU; llvmpipe at index 0 was left out of enumeration.
    assert plan["vulkan"] == [gpudev.Invocation(["--vk-device", "1"], None)]


def test_software_device_names_reads_cpu_devices_from_the_enumeration():
    """The CPU runtime's device name has no marker, so it is recognized by the [CPU] type in
    --list-devices, and returned with the suffix stripped so it matches the results JSON's bare
    device string. The real GPU is not returned."""
    names = gpudev.software_device_names("clpeak", _run(LIST_WITH_CPU_RUNTIME))

    assert names == {"cpu-haswell-Intel(R) Xeon(R) Gold 6430"}


def test_software_device_names_is_empty_when_enumeration_is_unreadable():
    """No enumeration, no cross-reference; the caller still drops the rasterizers by name."""
    assert gpudev.software_device_names("clpeak", _run("", ok=False)) == set()
