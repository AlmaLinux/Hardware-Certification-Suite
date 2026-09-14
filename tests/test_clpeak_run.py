"""Which of clpeak's tests run inside each API, what is recorded, and what the operator is told.

Every test the backend supports runs, because that is clpeak's default and this suite passes no
test-selection flags. It always did. What changed is that the results are now read rather than
scraped, which fixed two things at once:

- a run measured a dozen tests per backend and recorded **three** of them, and
- one of those three was the wrong number. The old patterns matched the first ``float :`` line after
  each heading, which is the *narrowest* vector width, while clpeak exists to report the peak. On
  OpenCL, where compute runs at five widths, the scalar figure was being published as the card's
  single-precision compute.

The fixtures here are clpeak 2.0.18's real JSON: ``{"format_version": 2, ..., "entries": [...]}``
with one entry per (backend, device, test, variant), carrying either a value or a status and a
reason. Taken from its own ``saveJson`` in ``src/common/result_store.cpp`` rather than invented,
which is the lesson of the CMake summary this project got wrong by imagining the format.
"""

import json

import pytest

from alma_certify import gpubuild
from alma_certify.benchmarks import gpu as gpu_bench


def entry(test, metric, value, unit="gflops", device="NVIDIA L40S", backend="CUDA", **extra):
    row = {
        "backend": backend, "platform": "NVIDIA CUDA", "device": device,
        "driver": "610.57.04", "category": "fp_compute", "test": test,
        "metric": metric, "unit": unit,
    }
    if value is not None:
        row["value"] = value
    row.update(extra)
    return row


# A CUDA device: compute is single-variant, global bandwidth runs at three widths, and this card has
# no fp64 worth the name.
CUDA_ENTRIES = [
    entry("single_precision_compute", "float", 89234.12),
    entry("double_precision_compute", "double", 1398.55),
    entry("half_precision_compute", "half", 178_004.5),
    entry("half_precision_compute", "half2", 178_100.9),
    entry("global_memory_bandwidth", "float", 698.2, unit="gbps"),
    entry("global_memory_bandwidth", "float2", 731.44, unit="gbps"),
    entry("global_memory_bandwidth", "float4", 722.10, unit="gbps"),
    entry("transfer_bandwidth", "enqueueWriteBuffer", 24.9, unit="gbps"),
    entry("kernel_launch_latency", "latency", 4.2, unit="us"),
    entry("integer_compute", "int", 44_100.0, unit="gops"),
    # The matrix-engine zoo, deliberately not promoted to metrics.
    entry("wmma_fp16", "fp16_16x16x16", 361_000.0, unit="tflops"),
    entry("wmma_int8", "int8_16x16x16", 722_000.0, unit="tops"),
    entry("cublas-fp", "fp32", 88_000.0, unit="tflops"),
]


class Res:
    def __init__(self, ok=True, returncode=0):
        self.stdout, self.stderr, self.ok, self.returncode = "", "", ok, returncode


class Ctx:
    """A run context that records what was logged and writes whatever clpeak would have written."""

    summary = {"gpus": [{"pci_ids": {"vendor": "NVIDIA Corporation [10de]"}, "driver": "nvidia"}]}
    pkg = None

    def __init__(self, tmp_path, reports=None, ok=True):
        self.tmp_path = tmp_path
        self.reports = reports or {}
        self.ok = ok
        self.lines = []
        self.commands = []

    def log(self, msg):
        self.lines.append(msg)

    def artifact_path(self, name):
        return str(self.tmp_path / name)

    def rel_artifact(self, name):
        return "artifacts/bench.gpu.clpeak/" + name

    def cmd(self, argv, **kwargs):
        self.commands.append(argv)
        flag = argv[1]
        entries = self.reports.get(flag)
        if entries is not None:
            # Written where clpeak was told to write it, by the same argument.
            path = argv[argv.index("--json-file") + 1]
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"format_version": 2, "clpeak_version": "2.0.18",
                           "os": "Linux", "entries": entries}, fh)
        return Res(ok=self.ok, returncode=0 if self.ok else 1)

    @property
    def logged(self):
        return "\n".join(self.lines)


@pytest.fixture
def two_backends(monkeypatch):
    build = gpubuild.Built(
        binary="/tmp/clpeak-build/clpeak", backends=["opencl", "cuda"], version="2.0.18",
        disabled={"rocm": "ROCm/HIP package not found", "oneapi": "IntelSYCL not found"},
    )
    monkeypatch.setattr(gpubuild, "build", lambda ctx, **kw: build)
    monkeypatch.setattr(gpubuild, "cleanup", lambda b: None)
    return build


def run(ctx):
    return gpu_bench.Clpeak().run(ctx)


# --- which APIs ------------------------------------------------------------------


def test_the_apis_it_measured_are_named(two_backends, tmp_path):
    ctx = Ctx(tmp_path, {"--cuda": CUDA_ENTRIES, "--opencl": CUDA_ENTRIES})

    run(ctx)

    assert "clpeak 2.0.18 built with: opencl, cuda" in ctx.logged


def test_the_apis_it_could_not_measure_are_named_with_the_reason(two_backends, tmp_path):
    """"Which APIs ran" is only half an answer on a machine with an AMD card and no ROCm. clpeak
    states the reason itself, and "ROCm/HIP package not found" is actionable where the absence of a
    line is not."""
    ctx = Ctx(tmp_path, {"--cuda": CUDA_ENTRIES, "--opencl": CUDA_ENTRIES})

    run(ctx)

    assert "rocm (ROCm/HIP package not found)" in ctx.logged
    assert "oneapi (IntelSYCL not found)" in ctx.logged


def test_each_backend_is_announced_before_it_runs(two_backends, tmp_path):
    """Each backend is a separate invocation of several minutes, so three of them spend a quarter of
    an hour here. The operator should see which one is busy rather than infer it from the clock."""
    ctx = Ctx(tmp_path, {"--cuda": CUDA_ENTRIES, "--opencl": CUDA_ENTRIES})

    run(ctx)

    assert "  [1/2] clpeak --opencl" in ctx.lines
    assert "  [2/2] clpeak --cuda" in ctx.lines
    announced = ctx.lines.index("  [2/2] clpeak --cuda")
    reported = next(i for i, line in enumerate(ctx.lines) if line.startswith("  cuda: "))
    assert announced < reported


# --- which tests inside the API --------------------------------------------------


def test_the_tests_it_measured_are_named(two_backends, tmp_path):
    """The question this exists to answer. A bare PASS said nothing about whether half-precision or
    fp64 had been exercised, and the answer differs by card.

    One row per test, which is what makes the answer readable. Eleven figures on one line came to
    three hundred characters and wrapped into porridge.
    """
    ctx = Ctx(tmp_path, {"--cuda": CUDA_ENTRIES})
    ctx.reports["--opencl"] = []

    run(ctx)

    assert "  cuda: 7 of 7 tests measured" in ctx.lines
    for label in ("single-precision", "double-precision", "half-precision", "integer",
                  "global memory", "host transfer", "kernel launch latency"):
        assert any(line.strip().startswith(label) for line in ctx.lines), label


def test_the_tests_it_could_not_measure_are_named_with_clpeaks_reason(two_backends, tmp_path):
    """A card with no fp64 and a run that went wrong look identical without this."""
    entries = [
        entry("single_precision_compute", "float", 89234.12),
        entry("double_precision_compute", "double", None, status="unsupported",
              reason="fp64 not supported on this compute capability"),
    ]
    ctx = Ctx(tmp_path, {"--cuda": entries, "--opencl": []})

    run(ctx)

    assert "  cuda: 1 of 2 tests measured" in ctx.lines
    assert any(
        line.strip() == "double-precision       not measured: fp64 not supported on this compute "
                        "capability"
        for line in ctx.lines
    ), ctx.logged


@pytest.mark.parametrize("order", ["failure last", "failure first"])
def test_a_variant_failing_does_not_hide_a_variant_that_worked(two_backends, tmp_path, order):
    """clpeak runs a test at several widths and reports each. One width unsupported while another
    measured is not a test that failed.

    Both orders, because they take different paths and only one of them was covered at first: a
    failure *after* a success is dropped on the way in, and a failure *before* one has to be taken
    back out. With the second path untested, deleting the line that does it changed nothing that
    failed.
    """
    ok = entry("global_memory_bandwidth", "float2", 731.44, unit="gbps")
    bad = entry("global_memory_bandwidth", "float16", None, unit="gbps", status="error",
                reason="Buffer alloc failed")
    entries = [ok, bad] if order == "failure last" else [bad, ok]
    ctx = Ctx(tmp_path, {"--cuda": entries, "--opencl": []})

    result = run(ctx)

    assert "not measured" not in ctx.logged
    assert "1 of 1 tests measured" in ctx.logged
    assert {m.name for m in result.metrics} == {"cuda_global_memory_bandwidth"}
    assert result.metrics[0].value == 731.44


def test_the_matrix_engine_zoo_is_not_promoted_to_metrics(two_backends, tmp_path):
    """clpeak reports far more per backend than this records: sixteen ``wmma_*`` variants on CUDA
    alone, plus cublas, rocblas, rocwmma, mfma, coopmat, and onemkl. They are vendor-specific, mean
    nothing across vendors, and promoting each would put hundreds of rows on every run. They stay in
    the artifact."""
    ctx = Ctx(tmp_path, {"--cuda": CUDA_ENTRIES, "--opencl": []})

    result = run(ctx)

    names = {m.name for m in result.metrics}
    assert not any("wmma" in name or "cublas" in name for name in names)
    assert "cuda_single_precision_compute" in names


def test_the_whole_of_clpeaks_output_is_kept_as_evidence(two_backends, tmp_path):
    """Which is what makes the curation safe: nothing is lost, it is only not a metric."""
    ctx = Ctx(tmp_path, {"--cuda": CUDA_ENTRIES, "--opencl": []})

    result = run(ctx)

    assert "artifacts/bench.gpu.clpeak/clpeak-cuda.json" in result.artifacts
    written = json.loads((tmp_path / "clpeak-cuda.json").read_text())
    assert any(e["test"] == "wmma_fp16" for e in written["entries"])


# --- the peak, not the first thing printed ---------------------------------------


def test_the_peak_across_widths_is_what_is_recorded(two_backends, tmp_path):
    """The bug this replaced. The old pattern took the first ``float :`` line after the heading,
    which is the narrowest vector width; clpeak exists to report the peak. Here float2 wins at
    731.44 and the scalar float is 698.2."""
    ctx = Ctx(tmp_path, {"--cuda": CUDA_ENTRIES, "--opencl": []})

    result = run(ctx)

    recorded = {m.name: m.value for m in result.metrics}
    assert recorded["cuda_global_memory_bandwidth"] == 731.44


def test_the_winning_width_is_recorded_beside_the_value(two_backends, tmp_path):
    """So a reader can see whether a card needed float4 to get there."""
    ctx = Ctx(tmp_path, {"--cuda": CUDA_ENTRIES, "--opencl": []})

    result = run(ctx)

    measured = result.details["per_backend"]["cuda"]["by_device"]["NVIDIA L40S"]["measured"]
    assert measured["global_memory_bandwidth"]["variant"] == "float2"
    assert measured["half_precision_compute"]["variant"] == "half2"


def test_each_device_keeps_its_own_number(two_backends, tmp_path):
    """A machine with two different cards keeps BOTH numbers - one metric per device, each tagged
    with the card that produced it - rather than collapsing to the faster card and discarding the
    slower one. This replaces the old best-device-wins behavior."""
    entries = [
        entry("single_precision_compute", "float", 44_000.0, device="NVIDIA L4"),
        entry("single_precision_compute", "float", 89234.12, device="NVIDIA L40S"),
    ]
    ctx = Ctx(tmp_path, {"--cuda": entries, "--opencl": []})

    result = run(ctx)

    # One metric per device, both kept, each tagged with its card.
    sp = {m.device: m.value for m in result.metrics
          if m.name == "cuda_single_precision_compute"}
    assert sp == {"NVIDIA L4": 44_000.0, "NVIDIA L40S": 89234.12}
    # Exactly one primary metric, on the fastest card.
    primaries = [m for m in result.metrics if m.primary]
    assert len(primaries) == 1
    assert primaries[0].device == "NVIDIA L40S"
    # Both devices recorded in the details.
    detail = result.details["per_backend"]["cuda"]
    assert detail["devices"] == ["NVIDIA L4", "NVIDIA L40S"]
    assert set(detail["by_device"]) == {"NVIDIA L4", "NVIDIA L40S"}


def test_all_gpus_runs_each_identical_card_and_stamps_an_ordinal(two_backends, tmp_path,
                                                                 monkeypatch):
    """--all-gpus: two identical cards are benchmarked separately (one clpeak invocation each,
    pinned by index) and each metric carries its ordinal, so they submit as individual results
    despite sharing a device string."""
    from alma_certify import gpudev

    monkeypatch.setattr(gpudev, "plan", lambda binary, run, backends, all_gpus: {
        "opencl": [gpudev.Invocation([], None)],
        "cuda": [gpudev.Invocation(["--cuda-device", "0"], 0),
                 gpudev.Invocation(["--cuda-device", "1"], 1)],
    })
    sp = [entry("single_precision_compute", "float", 89234.12, device="NVIDIA L40S")]
    ctx = Ctx(tmp_path, {"--cuda": sp, "--opencl": []})

    result = run(ctx)

    pairs = sorted((m.device, m.device_ordinal) for m in result.metrics
                   if m.name == "cuda_single_precision_compute")
    assert pairs == [("NVIDIA L40S", 0), ("NVIDIA L40S", 1)]
    # Two separate clpeak invocations, each pinned to its device index.
    cuda_cmds = [a for a in ctx.commands if a[1] == "--cuda"]
    assert cuda_cmds[0][2:4] == ["--cuda-device", "0"]
    assert cuda_cmds[1][2:4] == ["--cuda-device", "1"]
    # Both recorded distinctly in the details by their ordinal label.
    assert set(result.details["per_backend"]["cuda"]["by_device"]) == {
        "NVIDIA L40S #0", "NVIDIA L40S #1",
    }


def test_a_mixed_system_keeps_both_models_and_validates_each(two_backends, tmp_path):
    """Intel iGPU + NVIDIA dGPU on one backend: both models keep their own numbers, and a test one
    card lacks (fp64 on the Intel part) reads 'not measured' for THAT device only, not masked by the
    other card succeeding. Per-model validation falls out of per-device parsing."""
    intel, nv = "Intel(R) UHD Graphics 630", "NVIDIA L40S"
    entries = [
        entry("single_precision_compute", "float", 1_100.0, device=intel, backend="OpenCL"),
        entry("double_precision_compute", "double", None, device=intel, backend="OpenCL",
              status="unsupported", reason="device has no fp64"),
        entry("single_precision_compute", "float", 89_000.0, device=nv, backend="OpenCL"),
        entry("double_precision_compute", "double", 1_400.0, device=nv, backend="OpenCL"),
    ]
    ctx = Ctx(tmp_path, {"--opencl": entries, "--cuda": []})

    result = run(ctx)

    sp = {m.device: m.value for m in result.metrics
          if m.name == "opencl_single_precision_compute"}
    assert sp == {intel: 1_100.0, nv: 89_000.0}          # both models kept
    by_device = result.details["per_backend"]["opencl"]["by_device"]
    # fp64 is "not measured" only for the card that lacks it, not masked by the other's success.
    assert "double_precision_compute" in by_device[intel]["not_measured"]
    assert "double_precision_compute" not in by_device[nv]["not_measured"]
    assert by_device[nv]["measured"]["double_precision_compute"]["value"] == 1_400.0


def test_a_software_rasterizer_is_kept_out_of_the_figures(two_backends, tmp_path):
    """llvmpipe (lavapipe on Vulkan, rusticl/pocl on OpenCL) runs on the CPU. clpeak benchmarks it
    like any device - and OpenCL is not pinned, so it always runs - but recording it would publish
    CPU compute as a graphics result, which is how "llvmpipe" turned up as a GPU in the catalog. It
    is curated out: no metric, no per-device row, no log line. The raw JSON keeps it as evidence."""
    real = "AMD Radeon Graphics (RADV PHOENIX)"
    entries = [
        entry("single_precision_compute", "float", 512.0,
              device="llvmpipe (LLVM 17.0.6, 256 bits)", backend="OpenCL"),
        entry("single_precision_compute", "float", 6_800.0, device=real, backend="OpenCL"),
    ]
    ctx = Ctx(tmp_path, {"--opencl": entries, "--cuda": []})

    result = run(ctx)

    sp = {m.device: m.value for m in result.metrics
          if m.name == "opencl_single_precision_compute"}
    assert sp == {real: 6_800.0}, "only the real GPU should have a metric"
    assert not any("llvmpipe" in (m.device or "") for m in result.metrics)
    by_device = result.details["per_backend"]["opencl"]["by_device"]
    assert real in by_device
    assert not any("llvmpipe" in name for name in by_device)
    assert "llvmpipe" not in ctx.logged
    # Still present in the raw evidence clpeak wrote, which is not curated.
    with open(tmp_path / "clpeak-opencl.json", encoding="utf-8") as fh:
        written = json.load(fh)
    assert any("llvmpipe" in e["device"] for e in written["entries"])


def test_a_cpu_opencl_runtime_is_dropped_via_its_enumerated_type(two_backends, tmp_path,
                                                                 monkeypatch):
    """pocl and Intel's CPU OpenCL runtime report the bare CPU brand as the device name, with no
    software marker in it, so the name filter alone would let them through and record a CPU as a
    GPU. clpeak's --list-devices tags them [CPU]; software_device_names carries that across, and the
    figures drop them. The real GPU on the same backend is kept."""
    from alma_certify import gpudev

    cpu = "cpu-haswell-Intel(R) Xeon(R) Gold 6430"
    real = "AMD Radeon Graphics (RADV PHOENIX)"
    monkeypatch.setattr(gpudev, "software_device_names", lambda binary, run: {cpu})
    entries = [
        entry("single_precision_compute", "float", 900.0, device=cpu, backend="OpenCL"),
        entry("single_precision_compute", "float", 6_800.0, device=real, backend="OpenCL"),
    ]
    ctx = Ctx(tmp_path, {"--opencl": entries, "--cuda": []})

    result = run(ctx)

    sp = {m.device: m.value for m in result.metrics
          if m.name == "opencl_single_precision_compute"}
    assert sp == {real: 6_800.0}, "the CPU runtime must not be recorded as a GPU"
    assert cpu not in ctx.logged


def test_a_lone_software_rasterizer_yields_no_gpu_metric(two_backends, tmp_path):
    """A machine whose only device is llvmpipe (no accelerator, mesa's software stack installed)
    produces no GPU figure at all rather than a CPU one dressed as a GPU. With nothing left to
    record once the rasterizer is dropped, it reports no parseable results, the same as any backend
    that measured nothing."""
    entries = [entry("single_precision_compute", "float", 512.0,
                     device="llvmpipe (LLVM 17.0.6, 256 bits)", backend="OpenCL")]
    ctx = Ctx(tmp_path, {"--opencl": entries, "--cuda": []})

    result = run(ctx)

    assert result.metrics == []
    assert "llvmpipe" not in ctx.logged
    assert "no parseable results" in result.reason


# --- units and direction ---------------------------------------------------------


def test_latency_is_lower_is_better(two_backends, tmp_path):
    """The one metric here where less is more. Ranking it the other way would put the slowest
    machine in the catalog at the top of the leaderboard."""
    ctx = Ctx(tmp_path, {"--cuda": CUDA_ENTRIES, "--opencl": []})

    result = run(ctx)

    latency = next(m for m in result.metrics if m.name == "cuda_kernel_launch_latency")
    assert latency.unit == "us"
    assert latency.direction == gpu_bench.Direction.LOWER
    compute = next(m for m in result.metrics if m.name == "cuda_single_precision_compute")
    assert compute.direction == gpu_bench.Direction.HIGHER


def test_clpeaks_units_are_spelled_the_way_this_suite_spells_them(two_backends, tmp_path):
    ctx = Ctx(tmp_path, {"--cuda": CUDA_ENTRIES, "--opencl": []})

    result = run(ctx)

    units = {m.name: m.unit for m in result.metrics}
    assert units["cuda_single_precision_compute"] == "GFLOPS"
    assert units["cuda_global_memory_bandwidth"] == "GB/s"
    assert units["cuda_integer_compute"] == "GOPS"


def test_an_unknown_unit_is_not_ranked_either_way(two_backends, tmp_path):
    """A unit this suite has no rule for must not be guessed into a ranking. Recorded, passed
    through, and marked informational."""
    entries = [entry("single_precision_compute", "float", 1.0, unit="furlongs")]
    ctx = Ctx(tmp_path, {"--cuda": entries, "--opencl": []})

    result = run(ctx)

    metric = result.metrics[0]
    assert metric.unit == "furlongs"
    assert metric.direction == gpu_bench.Direction.INFO


def test_every_test_recorded_has_a_label_for_the_log():
    """The tags and the labels live in one table, so a tag added without a name is impossible."""
    assert set(gpu_bench._LABELS) == {tag for tag, _label in gpu_bench._TESTS}
    for tag, label in gpu_bench._TESTS:
        assert label and label != tag
    assert gpu_bench._PRIMARY_TEST in gpu_bench._LABELS


# --- the figures and the report --------------------------------------------------


def test_the_figures_are_reported_as_they_arrive(two_backends, tmp_path):
    ctx = Ctx(tmp_path, {"--cuda": CUDA_ENTRIES, "--opencl": []})

    run(ctx)

    rows = [" ".join(line.split()) for line in ctx.lines]
    assert "single-precision 89234.12 GFLOPS" in rows
    assert "global memory 731.44 GB/s" in rows
    assert "kernel launch latency 4.2 us" in rows


def test_the_logged_figure_is_the_recorded_figure(two_backends, tmp_path):
    """``%g`` rounds to six significant digits, so 89234.12 GFLOPS printed as 89234.1 while the
    metric kept the other digit. A log that disagrees with the report is worse than one that says
    less."""
    ctx = Ctx(tmp_path, {"--cuda": CUDA_ENTRIES, "--opencl": []})

    result = run(ctx)

    recorded = {m.name: m.value for m in result.metrics}
    assert recorded["cuda_single_precision_compute"] == 89234.12
    assert "89234.12" in ctx.logged


def test_exactly_one_metric_leads_the_row(two_backends, tmp_path):
    """Single-precision compute is the number everybody recognizes for a GPU, and the row names the
    backend it came from, so nothing pretends a CUDA figure and an OpenCL one are the same
    measurement."""
    ctx = Ctx(tmp_path, {"--cuda": CUDA_ENTRIES, "--opencl": CUDA_ENTRIES})

    result = run(ctx)

    primary = [m.name for m in result.metrics if m.primary]
    assert primary == ["opencl_single_precision_compute"]


def test_a_backend_that_wrote_nothing_says_so(two_backends, tmp_path):
    """It built, it ran, and there was no device behind the runtime: usually an OpenCL ICD installed
    for a card that is not in the machine."""
    ctx = Ctx(tmp_path, {"--cuda": CUDA_ENTRIES, "--opencl": []})

    run(ctx)

    assert "opencl: no results in its output; see clpeak-opencl.log" in ctx.logged


def test_a_backend_that_failed_says_what_it_exited_with(two_backends, tmp_path):
    ctx = Ctx(tmp_path, {}, ok=False)

    result = run(ctx)

    assert "exited 1 and wrote no results" in ctx.logged
    assert result.status == "error"


def test_a_malformed_report_is_not_a_crash(two_backends, tmp_path):
    """A benchmark that cannot parse its own output should report measuring nothing, not end the
    run."""
    ctx = Ctx(tmp_path, {})

    def cmd(argv, **kwargs):
        ctx.commands.append(argv)
        path = argv[argv.index("--json-file") + 1]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        return Res()

    ctx.cmd = cmd

    result = run(ctx)

    assert result.status == "error"
    assert "produced no parseable results" in (result.reason or "")


def test_the_report_carries_what_was_and_was_not_built(two_backends, tmp_path):
    """A leaderboard row with no ROCm figure is otherwise indistinguishable from a machine where
    ROCm was there and failed."""
    ctx = Ctx(tmp_path, {"--cuda": CUDA_ENTRIES, "--opencl": CUDA_ENTRIES})

    result = run(ctx)

    assert result.details["backends_built"] == ["opencl", "cuda"]
    assert result.details["backends_not_built"] == {
        "rocm": "ROCm/HIP package not found", "oneapi": "IntelSYCL not found",
    }
    assert result.details["clpeak_version"] == "2.0.18"


def test_a_build_with_nothing_disabled_says_nothing_about_it(monkeypatch, tmp_path):
    monkeypatch.setattr(gpubuild, "build", lambda ctx, **kw: gpubuild.Built(
        binary="/tmp/clpeak", backends=["cuda"], version="2.0.18", disabled={},
    ))
    monkeypatch.setattr(gpubuild, "cleanup", lambda b: None)
    ctx = Ctx(tmp_path, {"--cuda": CUDA_ENTRIES})

    run(ctx)

    assert "not built" not in ctx.logged


def test_the_backends_reported_are_the_backends_run(two_backends, tmp_path):
    """The line naming what was built and the commands actually issued have to agree, or the log
    claims coverage the run does not have."""
    ctx = Ctx(tmp_path, {"--cuda": CUDA_ENTRIES, "--opencl": CUDA_ENTRIES})

    run(ctx)

    # Excluding the --list-devices probe gpudev runs first to plan device selection.
    assert [argv[1] for argv in ctx.commands if argv[1] != "--list-devices"] == [
        gpubuild.BACKEND_FLAGS[b] for b in two_backends.backends
    ]


def test_clpeak_is_asked_for_json_where_the_artifacts_go(two_backends, tmp_path):
    """Into the run directory, so it lands in the bundle as evidence rather than in /tmp."""
    ctx = Ctx(tmp_path, {"--cuda": CUDA_ENTRIES, "--opencl": CUDA_ENTRIES})

    run(ctx)

    for argv in ctx.commands:
        if argv[1] == "--list-devices":
            continue  # the enumeration probe writes no json-file
        assert "--json-file" in argv
        assert argv[argv.index("--json-file") + 1] == str(tmp_path / ("clpeak-%s.json" % (
            "opencl" if argv[1] == "--opencl" else "cuda"
        )))


# --- the runtimes a machine actually has ----------------------------------------
#
# Reported from a real machine with an integrated Intel UHD 630: the benchmark skipped saying it had
# no OpenCL ICD, and its Vulkan was never mentioned at all. Two separate faults. Nothing had ever
# installed an OpenCL runtime for Intel, and the gate asked about OpenCL and CUDA only, so a card
# whose Vulkan driver works was refused without Vulkan being part of the question.

INTEL = {
    "pci_ids": {"vendor": "Intel Corporation [8086]", "device": "UHD Graphics 630 [3e92]"},
    "driver": "i915",
}
AMD_GPU = {
    "pci_ids": {"vendor": "Advanced Micro Devices, Inc. [AMD/ATI] [1002]", "device": "Navi [744c]"},
    "driver": "amdgpu",
}


def test_an_intel_gpu_gets_an_opencl_runtime():
    """``intel-opencl`` from EPEL is what puts /etc/OpenCL/vendors/intel.icd on the machine. Nothing
    installed it before, so an Intel card could never have an OpenCL device to find."""
    packages = gpubuild.required_packages({"gpus": [INTEL]})

    assert "intel-opencl" in packages


def test_every_machine_gets_a_vulkan_driver():
    """``mesa-vulkan-drivers`` is what drops intel_icd.json and radeon_icd.json, and it comes from
    AppStream, which is enabled by default. It is the difference between clpeak's Vulkan backend
    having a device and having none."""
    nvidia = {"pci_ids": {"vendor": "NVIDIA Corporation [10de]"}, "driver": "nvidia"}
    for gpus in ([INTEL], [AMD_GPU], [nvidia], [INTEL, nvidia]):
        packages = gpubuild.required_packages({"gpus": gpus})
        assert "mesa-vulkan-drivers" in packages, gpus
        assert "vulkan-loader" in packages, gpus


def test_an_intel_gpu_is_not_sent_the_cuda_toolkit():
    packages = gpubuild.required_packages({"gpus": [INTEL]})

    assert "cuda-toolkit" not in packages
    assert "rocm-hip-devel" not in packages


def test_the_vulkan_icds_are_found_where_they_live(monkeypatch, tmp_path):
    """The counterpart of ``opencl_icds``. /usr/share/vulkan/icd.d is where mesa-vulkan-drivers puts
    its manifests, confirmed by installing it in a container."""
    icd = tmp_path / "intel_icd.x86_64.json"
    icd.write_text("{}")
    monkeypatch.setattr(gpubuild.glob, "glob", lambda pattern: (
        [str(icd)] if pattern == "/usr/share/vulkan/icd.d/*.json" else []
    ))

    assert gpubuild.vulkan_icds() == [str(icd)]


def test_the_runtime_report_names_every_backend_found_and_missing():
    """Both halves. "Vulkan didn't even attempt to run and wasn't mentioned" is what a reader said
    about the version that only reported what was absent."""
    described = gpu_bench.describe_runtimes(
        {"opencl": True, "vulkan": True, "cuda": False, "rocm": False}
    )

    assert "found opencl, vulkan" in described
    assert "no cuda, rocm" in described


def test_a_machine_with_only_vulkan_is_not_refused(monkeypatch, tmp_path):
    """The Intel case. No OpenCL ICD and no CUDA, and a Vulkan driver that works: the old gate
    refused this machine outright."""
    from alma_certify.config import Config
    from alma_certify.registry import RunContext

    monkeypatch.setattr(gpu_bench, "runtimes_present", lambda: {
        "opencl": False, "vulkan": True, "cuda": False, "rocm": False,
    })
    monkeypatch.setattr(gpubuild, "source_archive", lambda: "/data/clpeak-2.0.18.tar.gz")

    class Pkg:
        def missing(self, packages, repos=(), timeout=900):
            return []

        def ensure(self, packages, repos=(), timeout=900):
            return True

    ctx = RunContext(Config.load("/nonexistent"), str(tmp_path),
                     inventory={"summary": {"gpus": [INTEL]}}, pkg=Pkg())

    assert gpu_bench.Clpeak().applicable(ctx) is None


def test_the_runtimes_are_logged_even_when_the_run_succeeds(two_backends, tmp_path, monkeypatch):
    """So a backend cannot go missing in silence. The reader who found this had no way to tell
    whether Vulkan had been considered."""
    monkeypatch.setattr(gpu_bench, "runtimes_present", lambda: {
        "opencl": False, "vulkan": True, "cuda": True, "rocm": False,
    })
    ctx = Ctx(tmp_path, {"--cuda": CUDA_ENTRIES, "--opencl": CUDA_ENTRIES})

    run(ctx)

    assert "GPU compute runtimes: found cuda, vulkan; no opencl, rocm" in ctx.logged


def test_the_runtime_inventory_asks_about_all_four_backends(monkeypatch):
    """The function itself, not a stub of it. Every test above replaces ``runtimes_present``, so
    deleting Vulkan from inside it changed nothing that failed - which is the very omission this
    whole change is about."""
    monkeypatch.setattr(gpu_bench, "opencl_icds", lambda: ["/etc/OpenCL/vendors/intel.icd"])
    monkeypatch.setattr(gpubuild, "vulkan_icds", lambda: ["/usr/share/vulkan/icd.d/intel_icd.json"])
    monkeypatch.setattr("alma_certify.validate.nvidia.find_nvcc", lambda: None)
    monkeypatch.setattr(gpu_bench.procutil, "find_tool", lambda tool, extra_dirs=(): None)
    monkeypatch.setattr(gpu_bench.glob, "glob", lambda pattern: [])

    found = gpu_bench.runtimes_present()

    assert set(found) == {"opencl", "vulkan", "cuda", "rocm"}
    assert found["opencl"] is True
    assert found["vulkan"] is True, "Vulkan has to be part of the question"
    assert found["cuda"] is False
    assert found["rocm"] is False


def test_rocm_counts_as_a_runtime_wherever_hipcc_lives(monkeypatch):
    """/opt/rocm*/bin/hipcc as well as $PATH: ROCm installs outside it."""
    monkeypatch.setattr(gpu_bench, "opencl_icds", lambda: [])
    monkeypatch.setattr(gpubuild, "vulkan_icds", lambda: [])
    monkeypatch.setattr("alma_certify.validate.nvidia.find_nvcc", lambda: None)
    monkeypatch.setattr(gpu_bench.procutil, "find_tool", lambda tool, extra_dirs=(): None)
    monkeypatch.setattr(gpu_bench.glob, "glob", lambda pattern: ["/opt/rocm-6.2/bin/hipcc"])

    assert gpu_bench.runtimes_present()["rocm"] is True


def test_only_the_unavailable_build_dependencies_are_named(monkeypatch, tmp_path):
    """The AlmaLinux 8 shape, reported from a real machine. Eight of clpeak's build dependencies are
    there and two are not packaged for that release at all, and the skip named all eight - so a
    reader who went looking found ``cmake`` and ``gcc-c++`` present and could not tell which name
    was the real obstacle. Verified against ``almalinux:8`` with EPEL and PowerTools enabled:
    ``glslc`` and ``intel-opencl`` have no package there."""
    from alma_certify.config import Config
    from alma_certify.registry import RunContext

    monkeypatch.setattr(gpu_bench, "runtimes_present", lambda: {
        "opencl": False, "vulkan": True, "cuda": False, "rocm": False,
    })
    monkeypatch.setattr(gpubuild, "source_archive", lambda: "/data/clpeak-2.0.18.tar.gz")

    class Pkg:
        absent = {"glslc", "intel-opencl"}

        def missing(self, packages, repos=(), timeout=900):
            return [p for p in packages if p in self.absent]

        def ensure(self, packages, repos=(), timeout=900):
            return not self.missing(packages, repos=repos, timeout=timeout)

    ctx = RunContext(Config.load("/nonexistent"), str(tmp_path),
                     inventory={"summary": {"gpus": [INTEL]}}, pkg=Pkg())

    reason = gpu_bench.Clpeak().applicable(ctx)

    assert reason is not None
    # glslc is a base build dependency: without it clpeak cannot build, so it is named. intel-opencl
    # is a vendor runtime, not a build dependency, so its absence no longer blocks the build and it
    # is not in the block reason (it is a note instead). Naming the base blocker and only it is what
    # tells the reader which name is the real obstacle.
    assert "glslc" in reason
    assert "intel-opencl" not in reason
    for present in ("cmake", "gcc-c++", "opencl-headers", "vulkan-headers"):
        assert present not in reason, reason


def test_a_missing_vendor_sdk_does_not_block_the_whole_benchmark(monkeypatch, tmp_path):
    """The reported bug: 'clpeak is refusing to build because rocm packages aren't present.' On
    AlmaLinux 8 and 9 EPEL has no ROCm hip/hipcc, so those came back unavailable and the whole
    benchmark skipped, even though Vulkan (radv) could measure the AMD card. A vendor SDK that this
    release does not package must drop only its own backend, not the benchmark."""
    from alma_certify.config import Config
    from alma_certify.registry import RunContext

    # A radv Vulkan runtime is present (the case on el8/el9 once mesa-vulkan-drivers is installed);
    # ROCm is not.
    monkeypatch.setattr(gpu_bench, "runtimes_present", lambda: {
        "opencl": False, "vulkan": True, "cuda": False, "rocm": False,
    })
    monkeypatch.setattr(gpubuild, "source_archive", lambda: "/data/clpeak-2.0.18.tar.gz")

    class Pkg:
        # Exactly the AMD-on-el8/el9 shape: the ROCm build SDK is the only thing not packaged.
        absent = {"rocm-hip-devel", "hipcc"}

        def __init__(self):
            self.logged = []

        def missing(self, packages, repos=(), timeout=900):
            return [p for p in packages if p in self.absent]

        def ensure(self, packages, repos=(), timeout=900):
            return not self.missing(packages, repos=repos, timeout=timeout)

    ctx = RunContext(Config.load("/nonexistent"), str(tmp_path),
                     inventory={"summary": {"gpus": [AMD_GPU]}}, pkg=Pkg())
    said = []
    ctx._log = said.append

    reason = gpu_bench.Clpeak().applicable(ctx)

    assert reason is None, "a missing ROCm SDK must not skip a benchmark Vulkan can run"
    # And the reader is told why there will be no ROCm numbers, rather than left guessing.
    assert any("rocm-hip-devel" in line for line in said)


def test_a_missing_base_dependency_still_blocks(monkeypatch, tmp_path):
    """The other half: cmake or a compiler genuinely absent is a wall, not a note, because then
    nothing builds."""
    from alma_certify.config import Config
    from alma_certify.registry import RunContext

    monkeypatch.setattr(gpu_bench, "runtimes_present", lambda: {
        "opencl": True, "vulkan": True, "cuda": False, "rocm": False,
    })
    monkeypatch.setattr(gpubuild, "source_archive", lambda: "/data/clpeak-2.0.18.tar.gz")

    class Pkg:
        absent = {"cmake"}

        def missing(self, packages, repos=(), timeout=900):
            return [p for p in packages if p in self.absent]

        def ensure(self, packages, repos=(), timeout=900):
            return not self.missing(packages, repos=repos, timeout=timeout)

    ctx = RunContext(Config.load("/nonexistent"), str(tmp_path),
                     inventory={"summary": {"gpus": [AMD_GPU]}}, pkg=Pkg())

    reason = gpu_bench.Clpeak().applicable(ctx)

    assert reason is not None and "cmake" in reason
