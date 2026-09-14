"""Memory benchmarks: bandwidth (bundled streamish.c) and latency (memlat.c)."""

from __future__ import annotations

import os
import re
import shutil
import tempfile

from ..registry import REGISTRY, RunContext
from ..result import Direction, Metric
from .base import BenchmarkTest
from .stressng import misc_metric

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def compile_micro(ctx: RunContext, source: str, extra_flags=()):
    """Compile a bundled C micro-benchmark; returns the binary path or None."""
    workdir = tempfile.mkdtemp(prefix="alma-certify-bench-")
    binary = os.path.join(workdir, os.path.splitext(source)[0])
    res = ctx.cmd(
        ["gcc", "-O3"] + list(extra_flags) + [
            "-o", binary, os.path.join(_DATA_DIR, source),
        ],
        timeout=120,
        artifact="gcc-%s.log" % source,
    )
    return binary if res.ok else None


class MemoryBandwidth(BenchmarkTest):
    id = "bench.mem.bandwidth"
    category = "memory"
    default_timeout = 900
    packages = ("gcc",)

    def run(self, ctx: RunContext):
        binary = compile_micro(ctx, "streamish.c", ["-fopenmp"])
        if not binary:
            return self.bench_error("could not compile the bandwidth micro-benchmark")
        try:
            res = ctx.cmd([binary], timeout=self.default_timeout, artifact="streamish.log")
        finally:
            shutil.rmtree(os.path.dirname(binary), ignore_errors=True)
        artifacts = [ctx.rel_artifact("streamish.log")]
        values = {}
        for line in res.stdout.splitlines():
            m = re.match(r"(copy|scale|add|triad):\s*([\d.]+)", line)
            if m:
                values[m.group(1)] = float(m.group(2))
        if "triad" not in values:
            return self.bench_error("bandwidth benchmark produced no results", artifacts)
        metrics = [
            Metric("%s_bandwidth" % name, value, "MB/s", Direction.HIGHER,
                   primary=(name == "triad"))
            for name, value in sorted(values.items())
        ]
        return self.bench_result(metrics, details={"threads": os.cpu_count()},
                                 artifacts=artifacts)


class MemoryLatency(BenchmarkTest):
    id = "bench.mem.latency"
    category = "memory"
    default_timeout = 600
    packages = ("gcc",)

    def run(self, ctx: RunContext):
        binary = compile_micro(ctx, "memlat.c")
        if not binary:
            return self.bench_error("could not compile the latency micro-benchmark")
        try:
            res = ctx.cmd([binary], timeout=self.default_timeout, artifact="memlat.log")
        finally:
            shutil.rmtree(os.path.dirname(binary), ignore_errors=True)
        artifacts = [ctx.rel_artifact("memlat.log")]
        m = re.search(r"latency_ns:\s*([\d.]+)", res.stdout)
        if not m:
            return self.bench_error("latency benchmark produced no result", artifacts)
        return self.bench_result(
            [Metric("latency_64m", float(m.group(1)), "ns", Direction.LOWER, primary=True)],
            details={"working_set_mb": 64},
            artifacts=artifacts,
        )


class StressNgStream(BenchmarkTest):
    """Cross-check for the bundled bandwidth micro (different code path)."""

    id = "bench.mem.stressng-stream"
    category = "memory"
    # v1 never matched the memory-rate line and published sys time as MB/s.
    benchmark_version = "2"
    default_timeout = 300
    packages = ("stress-ng",)
    repos = ("epel",)

    def run(self, ctx: RunContext):
        res = ctx.cmd(
            ["stress-ng", "--stream", "0", "--metrics", "-t", "30s"],
            timeout=self.default_timeout,
            artifact="stress-ng-stream.log",
        )
        artifacts = [ctx.rel_artifact("stress-ng-stream.log")]
        output = res.stdout + res.stderr
        # stress-ng aggregates the measurement across instances in its miscellaneous metrics, and
        # reworded those lines between the versions the supported releases ship. Both wordings are
        # offered per rate, newest first:
        #
        #   0.19 (AlmaLinux 9/10):  stream  3288.47 MB per sec memory read rate (harmonic mean ...)
        #   0.15 (AlmaLinux 8):     stream  2317.88 memory read rate (MB per sec) (geometic mean)
        #
        # Matching only the 0.19 wording reported no memory rate on AlmaLinux 8, which is the same
        # version-drift the metrics-table log kind had.
        read_rate = misc_metric(
            output, "stream",
            ("MB per sec memory read rate", "memory read rate (MB per sec)"),
        )
        write_rate = misc_metric(
            output, "stream",
            ("MB per sec memory write rate", "memory write rate (MB per sec)"),
        )
        compute = misc_metric(
            output, "stream",
            ("Mflop per sec (double precision) compute rate", "memory rate (Mflop per sec)"),
        )
        if read_rate is None:
            # Nothing here can be converted into a memory rate. The table only
            # carries bogo-ops, which is not MB/s at any column, and stress-ng
            # says so itself: "run duration too short to reliably determine
            # memory rate".
            short = "run duration too short" in output
            return self.bench_error(
                "stress-ng reported no memory rate"
                + (" - its run was too short to measure one" if short else ""),
                artifacts,
            )
        metrics = [
            Metric("memory_read_rate", read_rate, "MB/s", Direction.HIGHER,
                   primary=True),
        ]
        if write_rate is not None:
            metrics.append(
                Metric("memory_write_rate", write_rate, "MB/s", Direction.HIGHER)
            )
        if compute is not None:
            metrics.append(Metric("compute_rate", compute, "Mflop/s", Direction.HIGHER))
        return self.bench_result(metrics, artifacts=artifacts)


REGISTRY.register(MemoryBandwidth)
REGISTRY.register(MemoryLatency)
REGISTRY.register(StressNgStream)
