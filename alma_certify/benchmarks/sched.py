"""Scheduler benchmark: stress-ng context switching.

hackbench (from rt-tests) used to live here too, but rt-tests does not ship the hackbench binary
on every AlmaLinux release, so it recorded on only a fraction of runs. stress-ng's ``--switch``
measures the same thing - context-switch throughput - and installs everywhere from EPEL, so it is
the one scheduler benchmark now.
"""

from __future__ import annotations

from ..registry import REGISTRY, RunContext
from ..result import Direction, Metric
from .base import BenchmarkTest
from .stressng import metrc_row, misc_metric


class StressNgSwitch(BenchmarkTest):
    id = "bench.sched.stressng-switch"
    category = "scheduler"
    benchmark_version = "2"  # v1 published sys time as the switch rate
    default_timeout = 300
    packages = ("stress-ng",)
    repos = ("epel",)

    def run(self, ctx: RunContext):
        res = ctx.cmd(
            ["stress-ng", "--switch", "0", "--metrics", "-t", "30s"],
            timeout=self.default_timeout,
            artifact="stress-ng-switch.log",
        )
        artifacts = [ctx.rel_artifact("stress-ng-switch.log")]
        output = res.stdout + res.stderr
        row = metrc_row(output, "switch")
        if row is None:
            return self.bench_error("stress-ng switch metrics not found", artifacts)
        metrics = [
            Metric("ctx_switch_rate", row["bogo_ops_per_sec"], "ops/s",
                   Direction.HIGHER, primary=True),
        ]
        # stress-ng measures the thing directly. A rate derived from bogo-ops is
        # a proxy for it, so publish both and let the latency be the detail.
        latency = misc_metric(output, "switch", "nanosecs per context switch")
        if latency is not None:
            metrics.append(
                Metric("ctx_switch_latency", latency, "ns", Direction.LOWER)
            )
        return self.bench_result(
            metrics,
            artifacts=artifacts,
            details={"bogo_ops": row["bogo_ops"], "real_time_s": row["real_time"]},
        )


REGISTRY.register(StressNgSwitch)
