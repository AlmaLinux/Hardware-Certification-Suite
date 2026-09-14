"""Shared plumbing for benchmark tests."""

from __future__ import annotations

from typing import Any, List, Optional

from .. import hwquery
from ..registry import RunContext, Test
from ..result import Metric, Status, TestResult


class BenchmarkTest(Test):
    run_type = "benchmark"
    severity = None  # benchmarks have no gating severity
    benchmark_version = "1"  # bump when the workload definition changes

    def bench_result(
        self,
        metrics: List[Metric],
        details: Optional[dict] = None,
        artifacts: Optional[List[str]] = None,
        **kw: Any,
    ) -> TestResult:
        details = dict(details or {})
        details["benchmark_version"] = self.benchmark_version
        return self.result(
            Status.PASS, metrics=metrics, details=details,
            artifacts=artifacts or [], **kw
        )

    def bench_error(self, reason: str, artifacts: Optional[List[str]] = None) -> TestResult:
        details = {"benchmark_version": self.benchmark_version}
        return self.result(
            Status.ERROR, reason=reason, details=details, artifacts=artifacts or []
        )


def gpu_driver_info(ctx: RunContext) -> Optional[dict]:
    """driver_info for GPU benchmark details; None when no accelerator has a driver.

    GPU metrics without driver identification are meaningless for the
    leaderboards, so GPU benchmarks refuse to run without this.

    Accelerators only: a baseboard-management display adapter (an ASPEED or Matrox VGA console)
    enumerates as a display-class device with a bound ``ast``/``mgag200`` driver, but it is not a
    GPU. Counting it here is what let clpeak fire on a headless server and then fail against the CPU
    software rasterizer, the only device its runtimes could actually find.
    """
    gpus = hwquery.accelerator_gpus(ctx.summary)
    described = [
        {
            # ``pci_ids`` and ``smi_name`` verbatim, because naming the part is the server's
            # decision and this is a benchmark detail rather than a catalog entry.
            #
            # This read ``model`` and ``vendor``, neither of which is a key on a reported GPU any
            # more: the collector stopped flattening them so that a naming rule which turned out
            # wrong could be corrected for bundles already submitted. So every GPU benchmark has
            # been recording two nulls where the card's identity should be, on a leaderboard whose
            # whole rule is that a GPU metric without driver identification is meaningless.
            "pci_ids": g.get("pci_ids") or {},
            "smi_name": g.get("smi_name"),
            "driver": g.get("driver"),
            "driver_version": g.get("driver_version"),
            "runtime": g.get("runtime", {}),
            "vbios": g.get("vbios"),
        }
        for g in gpus
        if g.get("driver")
    ]
    return {"gpus": described} if described else None
