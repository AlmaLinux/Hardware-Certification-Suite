"""CPU benchmarks: sysbench events/s (single + all threads), stress-ng matrix."""

from __future__ import annotations

import os
import re

from ..registry import REGISTRY, RunContext
from ..result import Direction, Metric
from .base import BenchmarkTest
from .stressng import metrc_row

_EVENTS_RE = re.compile(r"events per second:\s*([\d.]+)")
# stress-ng --metrics row: "metrc: [pid] matrix <ops> <real> <usr+sys> <ops/s real> ..."


class _SysbenchCpu(BenchmarkTest):
    category = "cpu"
    default_timeout = 300
    packages = ("sysbench",)
    repos = ("epel",)
    threads = "1"

    def run(self, ctx: RunContext):
        threads = str(os.cpu_count() or 1) if self.threads == "all" else self.threads
        res = ctx.cmd(
            ["sysbench", "cpu", "--time=30", "--threads=%s" % threads, "run"],
            timeout=self.default_timeout,
            artifact="sysbench.log",
        )
        artifacts = [ctx.rel_artifact("sysbench.log")]
        m = _EVENTS_RE.search(res.stdout)
        if not res.ok or not m:
            return self.bench_error("sysbench did not report events/s", artifacts)
        return self.bench_result(
            [Metric("events_per_sec", float(m.group(1)), "events/s",
                    Direction.HIGHER, primary=True)],
            details={"threads": int(threads)},
            artifacts=artifacts,
        )


class SysbenchSingle(_SysbenchCpu):
    id = "bench.cpu.sysbench-single"
    threads = "1"


class SysbenchMulti(_SysbenchCpu):
    id = "bench.cpu.sysbench-multi"
    threads = "all"


class StressNgMatrix(BenchmarkTest):
    id = "bench.cpu.stressng-matrix"
    category = "cpu"
    # v1 published the stressor's sys time as its bogo-ops rate, so v1 numbers
    # are not a slower version of this measurement, they are a different
    # quantity. Ranking them together would put 0.13 beside 14,710.
    benchmark_version = "2"
    default_timeout = 300
    packages = ("stress-ng",)
    repos = ("epel",)

    def run(self, ctx: RunContext):
        res = ctx.cmd(
            ["stress-ng", "--matrix", "0", "--metrics", "-t", "60s"],
            timeout=self.default_timeout,
            artifact="stress-ng-matrix.log",
        )
        artifacts = [ctx.rel_artifact("stress-ng-matrix.log")]
        row = metrc_row(res.stdout + res.stderr, "matrix")
        if row is None:
            return self.bench_error("stress-ng did not report matrix metrics", artifacts)
        return self.bench_result(
            [Metric("bogo_ops_per_sec", row["bogo_ops_per_sec"], "bogo-ops/s",
                    Direction.HIGHER, primary=True)],
            artifacts=artifacts,
            details={"bogo_ops": row["bogo_ops"], "real_time_s": row["real_time"]},
        )


REGISTRY.register(SysbenchSingle)
REGISTRY.register(SysbenchMulti)
REGISTRY.register(StressNgMatrix)
