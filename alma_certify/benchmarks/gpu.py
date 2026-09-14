"""GPU benchmarks: compute across every backend clpeak finds, plus CUDA host-device transfer.

Three benchmarks were removed rather than packaged: hashcat, glmark2, and vkmark. The suite's
maintainers own the package for anything it needs, and none of the three earned that. None of the
four was in EPEL or in any AlmaLinux repository, so clpeak is being packaged either way and the
question was only which others to carry with it.

**clpeak covers the compute stacks.** It is not the OpenCL-only tool it once was: current versions
run OpenCL, CUDA, ROCm/HIP, Vulkan, and oneAPI/SYCL, and by default run every test on every backend
they find. So one package reaches every vendor runtime the suite cares about, which is what makes
dropping the rest defensible rather than merely cheaper.

**glmark2 and vkmark** were the graphics pair, and what they measured is the one thing clpeak does
not: rasterization, the present path, and the driver's graphics half. clpeak's Vulkan support is a
*compute* backend, cooperative-matrix tests rather than triangles. That gap is deliberate. This
program certifies that hardware works on AlmaLinux, and for the machines it sees that means
compute.

Neither had ever produced a number in any case. Their shared gate required a DRM render node *and*
a ``DISPLAY`` or ``WAYLAND_DISPLAY``, which no headless server has, and vkmark was invoked with
``--headless``, which is not how it selects a window system. It does have a headless module; the
flag was simply wrong, so it could not have scored on a desktop either.

**hashcat** measured sustained integer throughput where clpeak measures synthetic peak, which is a
thin difference to buy with a password-cracking tool on a public leaderboard. Its EPEL 9 branch was
retired by a previous maintainer, so the packaging burden is not hypothetical.
"""

from __future__ import annotations

import glob
import json

from .. import gpudev, hwquery, procutil
from ..registry import REGISTRY, RunContext
from ..result import Direction, Metric
from .base import BenchmarkTest, gpu_driver_info


def opencl_icds() -> list:
    """The installed OpenCL vendor ICDs.

    Its own function so a test can replace *this* rather than ``glob.glob``. Patching the stdlib
    reaches every other caller in the process, which is how a test meaning to fake an empty ICD
    directory also faked the result of looking for the bundled clpeak source, and then asserted
    against the wrong skip reason.
    """
    return glob.glob("/etc/OpenCL/vendors/*.icd")


def runtimes_present() -> dict:
    """Which GPU compute runtimes this machine can actually reach, by backend.

    One place that answers "is there anything here to measure with", covering every backend clpeak
    can build rather than the two the first version happened to check.
    """
    from .. import gpubuild
    from ..validate.nvidia import find_nvcc

    return {
        "opencl": bool(opencl_icds()),
        "vulkan": bool(gpubuild.vulkan_icds()),
        "cuda": find_nvcc() is not None,
        "rocm": bool(procutil.find_tool("hipcc") or glob.glob("/opt/rocm*/bin/hipcc")),
    }


def describe_runtimes(found: dict) -> str:
    """The runtime inventory in words, present and absent alike.

    Both halves on purpose. "Vulkan was never mentioned" is exactly what a reader said about the
    version that only reported what was missing, and a backend nobody names is a backend nobody
    knows to install a driver for.
    """
    here = [name for name, ok in sorted(found.items()) if ok]
    missing = [name for name, ok in sorted(found.items()) if not ok]
    parts = []
    if here:
        parts.append("found %s" % ", ".join(here))
    if missing:
        parts.append("no %s" % ", ".join(missing))
    return "; ".join(parts) or "nothing to check"


class _GpuBench(BenchmarkTest):
    category = "gpu"
    default_timeout = 900
    tool = ""

    def applicable(self, ctx: RunContext):
        if not gpu_driver_info(ctx):
            return hwquery.accelerator_skip_reason(ctx.summary)
        if procutil.which(self.tool) is None:
            installed = ctx.pkg.ensure(self.packages, repos=self.repos) if ctx.pkg else False
            if not installed or procutil.which(self.tool) is None:
                return "%s is not available from configured repos" % self.tool
        return self.capability_check(ctx)

    def capability_check(self, ctx: RunContext):
        """Return a skip reason when the tool is installed but cannot run
        here. Subclasses override; the default has no extra requirement."""
        return None


# What this benchmark records, by clpeak's canonical test tag, with the name for the run log.
#
# The portable set only: the precisions any GPU can be asked for, the three memory paths, and launch
# latency, so a metric name means the same measurement on every backend. clpeak's vendor-specific
# tests (sixteen CUDA ``wmma_*`` variants, cublas, rocblas, mfma, coopmat, onemkl, and OpenCL's
# ``integer_compute_fast``, ``_char``, and ``_short``) mean nothing across vendors and would put
# hundreds of rows on every run, so they stay in the clpeak JSON kept as a run artifact.
# ``bfloat16_compute`` is absent from OpenCL and simply produces no row there.
_TESTS = (
    ("single_precision_compute", "single-precision"),
    ("double_precision_compute", "double-precision"),
    ("half_precision_compute", "half-precision"),
    ("mixed_precision_compute", "mixed-precision"),
    ("bfloat16_compute", "bfloat16"),
    ("integer_compute", "integer"),
    ("integer_compute_int8_dp", "int8 dot product"),
    ("global_memory_bandwidth", "global memory"),
    ("local_memory_bandwidth", "local memory"),
    ("image_memory_bandwidth", "image memory"),
    ("transfer_bandwidth", "host transfer"),
    ("kernel_launch_latency", "kernel launch latency"),
)
_LABELS = dict(_TESTS)

# clpeak's unit strings, in this suite's spelling, and which way is better. ``us`` is the only one
# where less is more, and getting it wrong would rank the slowest machine in the catalog first.
_UNITS = {
    "gflops": ("GFLOPS", Direction.HIGHER),
    "tflops": ("TFLOPS", Direction.HIGHER),
    "gops": ("GOPS", Direction.HIGHER),
    "tops": ("TOPS", Direction.HIGHER),
    "gbps": ("GB/s", Direction.HIGHER),
    "us": ("us", Direction.LOWER),
}

# The figure a feed row leads with. Single-precision compute is the one number everybody recognizes
# for a GPU, and the row names the backend it came from, so nothing pretends a CUDA figure and an
# OpenCL one are the same measurement.
_PRIMARY_TEST = "single_precision_compute"


def _mark_primary(metrics):
    """Mark exactly one metric primary: the highest single-precision-compute figure across every
    device and backend.

    With more than one GPU there are several candidates for the headline number (one per device per
    backend), and the fastest is the honest one to feature. The rest stay non-primary so a result
    still carries at most one primary metric, which is the invariant the rest of the suite relies
    on. No candidate (a machine whose cards reported no single-precision figure) leaves none.
    """
    candidates = [m for m in metrics if m.name.endswith("_" + _PRIMARY_TEST)]
    if candidates:
        max(candidates, key=lambda m: m.value).primary = True


class Clpeak(_GpuBench):
    """Compute peaks, once per backend, from a clpeak built on this machine.

    **Built here rather than installed.** clpeak's backends are compiled in, and which ones are
    useful depends on the card in the machine, so one prebuilt binary would either omit CUDA on an
    NVIDIA box or carry SDK dependencies no AMD box can satisfy. ``gpubuild`` installs the SDKs and
    lets clpeak's own CMake decide what it can find.

    **One invocation per backend**, which is what makes the numbers mean something. A single
    multi-backend run prints figures for every backend it found, and the parser here used to take
    whichever appeared first: on a machine with CUDA and OpenCL both present, one number was
    recorded under ``sp_compute`` with nothing saying which stack produced it, and a leaderboard
    then compared an NVIDIA CUDA figure against an AMD OpenCL one as though they were the same
    measurement. Running each backend on its own makes every metric unambiguous by construction,
    without depending on how clpeak lays out a combined report.

    The CPU backend is off at configure time, so nothing here has to filter it out.
    """

    id = "bench.gpu.clpeak"
    tool = "clpeak"
    # Nothing to install: clpeak is in no repository and is built from the bundled source. The
    # build's own dependencies are installed by ``applicable`` below, which is where they can be
    # decided from the hardware present.
    packages = ()
    repos = ()
    default_timeout = 1800

    def applicable(self, ctx: RunContext):
        from .. import gpubuild

        if not gpu_driver_info(ctx):
            return hwquery.accelerator_skip_reason(ctx.summary)
        if gpubuild.source_archive() is None:
            return (
                "no clpeak source shipped in alma_certify/data, so there is nothing to build here. "
                "Nothing about this machine: run 'make clpeak-in-tree' to fetch it here, or "
                "install the package, which ships it"
            )
        wanted = gpubuild.required_packages(ctx.summary)
        # CRB as well as EPEL, named rather than relied on. ``ocl-icd-devel`` ships the link-time
        # libOpenCL.so and lives in CRB, which is disabled by default; ROCm comes from EPEL.
        # ``missing`` rather than ``ensure``: it returns the names that are still absent, and that
        # is the whole point of it returning a list. Naming ``wanted`` instead reported "could not
        # install what clpeak needs to build: cmake, gcc-c++, opencl-headers, ..." on an AlmaLinux 8
        # machine where cmake and gcc-c++ were fine and two of the eight genuinely are not packaged.
        # A reader cannot act on that: they went looking for the packages, found them, and had no
        # way to tell which name was the real problem.
        unavailable = (
            set(ctx.pkg.missing(wanted, repos=("epel", "crb"))) if ctx.pkg else set(wanted)
        )

        # Asked after the install attempt, because on an Intel card the install is what provides
        # the runtime (``intel-opencl`` drops intel.icd, ``mesa-vulkan-drivers`` drops
        # intel_icd.json): a UHD 630 machine gated first and refused the whole benchmark for want
        # of an ICD nothing had installed. Asked before the install's result is reported, because
        # "nothing to measure with" beats a list of packages that could not be fetched, and on an
        # NVIDIA box it names ``setup-gpu``.
        found = runtimes_present()
        if not any(found.values()):
            reason = "no GPU compute runtime on this machine: " + describe_runtimes(found)
            from ..validate.nvidia import nvidia_gpus

            if nvidia_gpus(ctx.summary):
                reason += "; run 'alma-certify setup-gpu', which installs the driver and CUDA"
            return reason
        # Only the base toolchain blocks the build. A vendor SDK that this release does not package
        # (ROCm's hip/hipcc on AlmaLinux 8 and 9, where EPEL carries it only on 10) means clpeak
        # builds without that one backend, not that it builds nothing. Blocking the whole benchmark
        # on it was the reported bug: an AMD card that Vulkan could benchmark got no benchmark at
        # all because rocm-hip-devel was absent. clpeak's CMake drops a backend whose SDK it cannot
        # find, so the missing SDK is a note here, not a wall.
        core_missing = sorted(unavailable.intersection(gpubuild.BASE_BUILD_PACKAGES))
        if core_missing:
            return "clpeak cannot be built here: %s not available in the configured repos" % (
                ", ".join(core_missing)
            )
        sdk_missing = sorted(unavailable.difference(gpubuild.BASE_BUILD_PACKAGES))
        if sdk_missing:
            ctx.log(
                "note: %s not available in the configured repos, so the matching GPU backend is "
                "not built; clpeak still measures every backend whose runtime is present"
                % ", ".join(sdk_missing)
            )
        return None

    def run(self, ctx: RunContext):
        from .. import gpubuild

        built = gpubuild.build(ctx)
        artifacts = [
            ctx.rel_artifact("clpeak-cmake.log"), ctx.rel_artifact("clpeak-build.log"),
        ]
        if built is None:
            return self.bench_error(
                "clpeak did not build on this machine; see the cmake and build logs", artifacts
            )
        if not built.backends:
            return self.bench_error(
                "clpeak built with no GPU backend enabled, so there is nothing it can measure "
                "here; see the cmake log for what it looked for and did not find",
                artifacts,
            )
        # Said out loud, and in the run log, because until now nothing told anybody which APIs this
        # measured. The binary is compiled here from what the hardware supports, so it differs
        # between machines, and a bare "PASS bench.gpu.clpeak (412.3s)" left the operator to open an
        # artifact to find out whether their CUDA stack had been exercised at all.
        ctx.log("GPU compute runtimes: %s" % describe_runtimes(runtimes_present()))
        ctx.log("clpeak %s built with: %s" % (built.version, ", ".join(built.backends)))
        if built.disabled:
            # clpeak's own reasons. "ROCm/HIP package not found" is actionable; the absence of a
            # line is not, and an operator who wanted a ROCm number is owed the difference.
            ctx.log("clpeak backends not built: %s" % ", ".join(
                "%s (%s)" % (backend, reason)
                for backend, reason in sorted(built.disabled.items())
            ))
        try:
            metrics, per_backend = self._measure(ctx, built, artifacts)
        finally:
            gpubuild.cleanup(built)
        if not metrics:
            return self.bench_error(
                "clpeak produced no parseable results on any of: %s"
                % ", ".join(built.backends),
                artifacts,
            )
        return self.bench_result(
            metrics,
            details={
                "driver_info": gpu_driver_info(ctx),
                # What was built and what each backend reported, so a reader can tell a CUDA
                # figure from an OpenCL one and knows which stacks were available at all.
                "clpeak_version": built.version,
                "backends_built": built.backends,
                # And what was not built, with the reason, so the report answers the same question
                # the run log does. A leaderboard row with no ROCm figure is otherwise
                # indistinguishable from a machine where ROCm failed.
                "backends_not_built": built.disabled,
                "per_backend": per_backend,
            },
            artifacts=artifacts,
        )

    def _measure(self, ctx: RunContext, built, artifacts: list):
        """Run each backend, on the devices ``gpudev`` selected, and record every portable test.

        By default one representative per distinct model is benchmarked; with ``--all-gpus`` every
        device is, one clpeak invocation each, stamped with an ordinal so identical cards submit as
        individual results. See ``alma_certify.gpudev`` for how the plan is built (and its safe
        fallback to running every device when enumeration is not possible).
        """
        from .. import gpubuild

        all_gpus = bool(
            ctx.config.getbool("benchmark", "all_gpus", False)
            if getattr(ctx, "config", None) else False
        )
        plan = gpudev.plan(built.binary, ctx.cmd, built.backends, all_gpus=all_gpus)
        # The names clpeak's enumeration tags as CPU/software, so a figure on a CPU OpenCL runtime
        # whose device name is a bare brand string (pocl, Intel's) can be dropped even though the
        # results JSON carries no type. Read once; the rasterizers are also caught by name below.
        software = gpudev.software_device_names(built.binary, ctx.cmd)

        metrics = []
        per_backend = {}
        total = sum(len(plan[backend]) for backend in built.backends)
        step = 0
        for backend in built.backends:
            flag = gpubuild.BACKEND_FLAGS[backend]
            invocations = plan[backend]
            devices_seen = []
            by_device = {}
            for inv_index, invocation in enumerate(invocations):
                step += 1
                # A distinct log/report per invocation so a per-device run does not overwrite the
                # last; the plain name is kept when a backend has only one, so single-GPU artifacts
                # read as before.
                suffix = "" if len(invocations) == 1 else "-dev%d" % inv_index
                log = "clpeak-%s%s.log" % (backend, suffix)
                report = "clpeak-%s%s.json" % (backend, suffix)
                report_path = ctx.artifact_path(report)
                # Before it runs, not after. Each invocation is several minutes, so the operator
                # should see which one is busy rather than guess from the elapsed time.
                ctx.log("  [%d/%d] clpeak %s%s" % (
                    step, total, flag,
                    " " + " ".join(invocation.device_args) if invocation.device_args else "",
                ))
                res = ctx.cmd(
                    [built.binary, flag, *invocation.device_args, "--json-file", report_path],
                    timeout=self.default_timeout,
                    artifact=log,
                )
                artifacts.append(ctx.rel_artifact(log))
                entries = self._read_report(report_path)
                if entries:
                    # The whole of clpeak's own output, kept as evidence. Everything this module
                    # does not promote to a metric is here, which is what makes the curation safe.
                    artifacts.append(ctx.rel_artifact(report))
                elif not res.ok:
                    ctx.log("  %s: exited %d and wrote no results; see %s"
                            % (backend, res.returncode, log))
                per_device, order = self._figures(entries, software)
                for device in order:
                    bucket = per_device[device]
                    # The details key carries the ordinal when there is one, so two identical cards
                    # under --all-gpus do not overwrite each other in the evidence.
                    label = device if invocation.ordinal is None \
                        else "%s #%d" % (device, invocation.ordinal)
                    by_device[label] = {
                        "measured": {tag: dict(zip(("value", "unit", "variant"), row))
                                     for tag, row in bucket["measured"].items()},
                        "not_measured": bucket["unmeasured"],
                    }
                    if label not in devices_seen:
                        devices_seen.append(label)
                    for tag, (value, unit, _variant) in bucket["measured"].items():
                        display, direction = _UNITS.get(unit, (unit, Direction.INFO))
                        metrics.append(Metric(
                            "%s_%s" % (backend, tag), value, display, direction,
                            device=device, device_ordinal=invocation.ordinal,
                        ))
                self._log_backend(ctx, backend, per_device, order, log)
            per_backend[backend] = {"devices": devices_seen, "by_device": by_device}
        _mark_primary(metrics)
        return metrics, per_backend

    def _log_backend(self, ctx, backend, per_device, order, log):
        """Every test that ran inside this API, per device, and every one that did not.

        A table rather than a sentence. The first version put all eleven figures on one line of
        three hundred characters, which wraps into porridge in any terminal. One row per test
        answers "which of clpeak's tests ran" by construction, and can be read down the numbers;
        with more than one device a heading names each so the two cards' figures are not confused.
        """
        if not any(b["measured"] or b["unmeasured"] for b in per_device.values()):
            ctx.log("  %s: no results in its output; see %s" % (backend, log))
            return
        multi = len(order) > 1
        for device in order:
            bucket = per_device[device]
            measured, unmeasured = bucket["measured"], bucket["unmeasured"]
            ctx.log("  %s%s: %d of %d tests measured" % (
                backend, " [%s]" % device if multi else "",
                len(measured), len(measured) + len(unmeasured),
            ))
            for tag, label in _TESTS:
                if tag in measured:
                    value, unit, _variant = measured[tag]
                    # ``%s`` for the value rather than ``%g``, which rounds to six significant
                    # digits: 89234.12 GFLOPS printed as 89234.1 while the recorded metric kept the
                    # other digit, and a log that disagrees with the report is worse than one that
                    # says less.
                    ctx.log("    %-22s %12s %s" % (
                        label, value, _UNITS.get(unit, (unit, None))[0],
                    ))
                elif tag in unmeasured:
                    # With clpeak's own reason, which is the difference between "this card has no
                    # fp64" and "the run went wrong". A missing row is otherwise a mystery.
                    ctx.log("    %-22s %s" % (label, "not measured: %s" % unmeasured[tag]))

    @staticmethod
    def _read_report(path: str) -> list:
        """clpeak's own JSON results, or an empty list if there are none to read.

        **Read rather than scraped.** This used to regex the human-readable output, which cost the
        run two things. The layout is not an interface: the same habit read CMake's backend summary
        and got every enabled backend wrong when the wording changed. And it could only find what a
        pattern had been written for, so a run measured a dozen tests per backend and recorded three
        of them, one of which was the wrong number - the patterns matched the first ``float :`` line
        after each heading, which is the *narrowest* vector width, while clpeak exists to report the
        peak. On OpenCL, where compute runs at five widths, that meant the scalar figure was being
        published as this card's single-precision compute.

        The JSON is one entry per (backend, device, test, variant), with a value or a status and a
        reason. Tolerant of an absent or malformed file, because a benchmark that cannot parse its
        own output should report that it measured nothing rather than end the run.
        """
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            return []
        entries = payload.get("entries") if isinstance(payload, dict) else None
        return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []

    @staticmethod
    def _figures(entries: list, software_names=frozenset()):
        """Per device: the peak per test (across variants), and why a test has none.

        ``software_names`` is the set of device names clpeak's enumeration tagged as CPU/software,
        used to drop a CPU OpenCL runtime whose result names a bare CPU brand no marker would catch.

        **Keyed by device, not collapsed across them.** A machine with an Intel iGPU and an NVIDIA
        dGPU keeps both cards' figures; the older version took the peak across devices and kept only
        the faster card's number, discarding the other. Distinguishing them is the whole point of
        recording per model downstream. clpeak keys its own results by device *name*, so two
        physically identical cards already collapse into one entry inside a single invocation - that
        is fine here (they give the same number), and the ``--all-gpus`` path handles telling
        identical cards apart by running them one at a time.

        **The peak across variants, not the first one**, still holds within a device: clpeak runs
        each test at several vector widths and the interesting figure is the best of them, which is
        why a tool called clpeak exists. The winning width is kept beside the value.

        Returns ``(per_device, order)``: ``per_device[device]`` carries ``"measured"``
        ({tag: (value, unit, variant)}) and ``"unmeasured"`` ({tag: reason}); ``order`` is the
        devices as first seen.
        """
        per_device = {}
        order = []
        for entry in entries:
            device = entry.get("device") or ""
            if device in software_names or gpudev.is_software_device(device):
                # A CPU device - a software rasterizer (llvmpipe and friends, caught by name) or a
                # CPU OpenCL runtime (caught by its enumerated type via ``software_names``). clpeak
                # benchmarks it like any other device, but recording it would publish CPU compute as
                # a graphics result. The raw JSON keeps it as evidence; it is only kept out of the
                # curated figures, so no metric, no per-device row, and no log line names it a GPU.
                continue
            if device not in per_device:
                per_device[device] = {"measured": {}, "unmeasured": {}}
                order.append(device)
            measured = per_device[device]["measured"]
            unmeasured = per_device[device]["unmeasured"]
            tag = entry.get("test")
            if tag not in _LABELS:
                continue
            value = entry.get("value")
            if value is None:
                # ``unsupported`` on a card without fp64, ``skipped``, or ``error`` with clpeak's
                # own explanation. Kept only while nothing has measured the test on this device, so
                # one variant failing does not contradict another variant succeeding.
                if tag not in measured:
                    unmeasured[tag] = (
                        entry.get("reason") or entry.get("status") or "no reason given"
                    )
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            best = measured.get(tag)
            if best is None or value > best[0]:
                measured[tag] = (value, entry.get("unit") or "", entry.get("metric") or "")
            unmeasured.pop(tag, None)
        return per_device, order


REGISTRY.register(Clpeak)


class CudaBandwidth(_GpuBench):
    """Host and device transfer rates, from the same probe the validation uses.

    The validation asks whether a transfer completes and clears a floor no sane link misses. This
    asks how fast, which is the comparable number: it is the one figure that separates a card in a
    x16 slot from the same card in a x4 slot, and that difference is invisible in every other
    metric on the page.

    Pinned host memory, one untimed warm-up pass, and a fixed 256 MiB buffer, all decided in
    ``data/cudaprobe.cu`` so the validation and the benchmark cannot drift into measuring
    different things.
    """

    id = "bench.gpu.cuda-bandwidth"
    tool = "nvcc"
    # Nothing to install: the CUDA toolkit comes from NVIDIA's own repository under their license,
    # and a certification suite must not add a third-party repository to a machine on its own
    # initiative. ``applicable`` skips with a reason when it is absent.
    packages = ()
    repos = ()
    default_timeout = 600

    def applicable(self, ctx: RunContext):
        from ..validate.nvidia import nvidia_gpus, vendor_driver_not_bound

        if not nvidia_gpus(ctx.summary):
            return "no NVIDIA GPU detected"
        if not gpu_driver_info(ctx):
            return hwquery.accelerator_skip_reason(ctx.summary)
        # nouveau counts as a bound driver above, so without this the probe builds, runs, and
        # reports an error about a card nothing here could have reached. Same skip as the
        # validation, which shares the helper.
        held = vendor_driver_not_bound(ctx.summary)
        if held:
            return held
        from ..validate.nvidia import find_nvcc

        if find_nvcc() is None:
            from ..validate.nvidia import _TOOLKIT_HINT

            return (
                "no CUDA compiler found, so there is no CUDA toolkit to build the probe with; "
                + _TOOLKIT_HINT
            )
        return None

    def run(self, ctx: RunContext):
        from ..validate.nvidia import compile_probe, parse_probe

        binary, log = compile_probe(ctx)
        artifacts = [ctx.rel_artifact("nvcc.log")]
        if binary is None:
            from ..validate.nvidia import incomplete_toolkit

            return self.bench_error(
                "the CUDA runtime headers and library are not installed, only the compiler"
                if incomplete_toolkit(log)
                else "the CUDA compiler could not build the bundled probe",
                artifacts,
            )
        res = ctx.cmd(
            [binary, "bandwidth", "0"],
            timeout=self.default_timeout,
            artifact="cudaprobe-bandwidth.log",
        )
        artifacts.append(ctx.rel_artifact("cudaprobe-bandwidth.log"))
        if not res.ok:
            values = parse_probe(res.stdout)
            return self.bench_error(
                values.get("error", "the CUDA bandwidth probe failed"), artifacts
            )
        values = parse_probe(res.stdout)
        metrics = []
        # Host to device is primary. It is the direction that gates real work, because feeding the
        # card is what a training or inference job spends its time on.
        for key, label, primary in (
            ("host_to_device_gib_per_s", "host_to_device", True),
            ("device_to_host_gib_per_s", "device_to_host", False),
            ("device_to_device_gib_per_s", "device_to_device", False),
        ):
            if key in values:
                metrics.append(Metric(label, float(values[key]), "GiB/s",
                                      Direction.HIGHER, primary=primary))
        if not metrics:
            return self.bench_error("the probe reported no transfer rates", artifacts)
        return self.bench_result(
            metrics,
            details={"driver_info": gpu_driver_info(ctx), "probe": values},
            artifacts=artifacts,
        )


REGISTRY.register(CudaBandwidth)
