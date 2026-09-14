"""Memory validation: short verified stress pass + hardware error counters."""

from __future__ import annotations

import glob
from typing import Dict

from .. import hwquery, procutil
from ..registry import REGISTRY, RunContext, Test
from ..result import Severity, Status


def _edac_counters() -> Dict[str, int]:
    counters: Dict[str, int] = {}
    for path in glob.glob("/sys/devices/system/edac/mc/mc*/[cu]e_count"):
        value = procutil.read_file(path, "0")
        try:
            counters[path] = int(value or 0)
        except ValueError:
            counters[path] = 0
    return counters


class MemoryFunctional(Test):
    id = "validate.memory.functional"
    category = "memory"
    run_type = "validate"
    severity = Severity.REQUIRED
    default_timeout = 900
    packages = ("stress-ng",)
    repos = ("epel",)

    def run(self, ctx: RunContext):
        duration = ctx.config.getint("validate", "memory_smoke_seconds", 60)
        timeout = ctx.config.timeout_for(self.id, self.default_timeout)
        before = _edac_counters()
        res = ctx.cmd(
            [
                "stress-ng", "--vm", "2", "--vm-bytes", "50%",
                "--verify", "-t", "%ds" % duration,
            ],
            timeout=timeout,
            artifact="stress-ng-vm.log",
        )
        after = _edac_counters()
        artifacts = [ctx.rel_artifact("stress-ng-vm.log")]

        new_errors = {
            k: after[k] - before.get(k, 0) for k in after if after[k] > before.get(k, 0)
        }
        if res.timed_out:
            return self.result(Status.ERROR, reason="stress-ng exceeded its timeout",
                               artifacts=artifacts)
        if res.returncode != 0:
            return self.result(
                Status.FAIL,
                reason="memory verify errors (stress-ng exit %d)" % res.returncode,
                artifacts=artifacts,
            )
        if new_errors:
            return self.result(
                Status.FAIL,
                reason="EDAC memory errors increased during test",
                details={"new_errors": new_errors},
                artifacts=artifacts,
            )
        return self.result(
            Status.PASS,
            details={"duration_s": duration, "edac_monitored": bool(before)},
            artifacts=artifacts,
        )


class EdacCounters(Test):
    id = "validate.memory.edac"
    category = "memory"
    run_type = "validate"
    severity = Severity.CONDITIONAL
    default_timeout = 60

    def applicable(self, ctx: RunContext):
        if not hwquery.has_edac():
            return "no EDAC memory controller driver loaded"
        return None

    def run(self, ctx: RunContext):
        counters = _edac_counters()
        nonzero = {k: v for k, v in counters.items() if v > 0}
        ue_nonzero = {k: v for k, v in nonzero.items() if k.endswith("ue_count")}
        if ue_nonzero:
            return self.result(
                Status.FAIL,
                reason="uncorrectable memory errors recorded",
                details={"counters": nonzero},
            )
        if nonzero:
            return self.result(
                Status.PASS,
                reason="correctable errors present (not gating)",
                details={"counters": nonzero},
            )
        return self.result(Status.PASS, details={"monitored": len(counters)})


REGISTRY.register(MemoryFunctional)
REGISTRY.register(EdacCounters)
