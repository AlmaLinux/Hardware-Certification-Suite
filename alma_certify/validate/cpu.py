"""CPU validation: short verified computation smoke, not endurance.

Also the home of ``validate.cpu.flags``, which measures nothing and asserts
nothing - it exists so the advertised feature set appears in the run's results
where people read them, instead of only inside the inventory blob.
"""

from __future__ import annotations

from ..registry import REGISTRY, RunContext, Test
from ..result import Severity, Status

# Feature flags worth naming individually, grouped by what they tell a reader.
#
# The point is not to be exhaustive - the full list is reported too. It is that a
# 159-entry alphabetical list buries the handful of facts that change what a
# machine can be used for, and nobody scans for "sev_snp" by eye. Anything absent
# from a group is simply left out of the report rather than listed as missing:
# these are capabilities, and a CPU is not defective for lacking AMX.
#
# Both architectures are covered in one table. aarch64 reports its features under
# a different lscpu heading but the same normalized field, and the names do not
# collide with the x86 ones.
NOTABLE_FLAGS = {
    "virtualization": ("vmx", "svm", "ept", "npt", "vnmi", "tdt"),
    "confidential_computing": (
        "sme", "sev", "sev_es", "sev_snp", "tdx_guest", "tme", "sgx", "sgx_lc",
    ),
    "crypto_acceleration": (
        "aes", "vaes", "pclmulqdq", "vpclmulqdq", "sha_ni", "sha1_ni", "sha512",
        "sha2", "sha3", "crc32", "gfni",
    ),
    "vector_extensions": (
        "avx", "avx2", "avx512f", "avx512bw", "avx512vl", "avx512dq",
        "avx512_vnni", "avx_vnni", "avx512_bf16", "amx_tile", "amx_bf16",
        "amx_int8", "sve", "sve2", "asimd", "asimdhp",
    ),
    "speculation_controls": (
        "ibpb", "ibrs", "stibp", "ssbd", "md_clear", "flush_l1d",
        "arch_capabilities", "ibrs_enhanced", "bhi_ctrl", "rrsba_ctrl",
    ),
    "timing_and_scheduling": (
        "constant_tsc", "nonstop_tsc", "tsc_deadline_timer", "aperfmperf",
        "rdtscp", "pcid", "invpcid", "hwp", "hwp_epp", "cppc",
    ),
}

# stress-ng exit codes (mined from the old suite's cpu/run_test.sh)
STRESS_NG_EXIT = {
    0: "success",
    1: "error during setup or general failure",
    2: "one or more stressors failed",
    3: "out of resources (memory/disk)",
    4: "stressor not implemented on this platform",
    5: "stressor was killed by a signal",
    6: "stressor exited by sys_exit()",
    7: "metrics were untrustworthy",
}


class CpuFunctional(Test):
    id = "validate.cpu.functional"
    category = "cpu"
    run_type = "validate"
    severity = Severity.REQUIRED
    default_timeout = 900
    packages = ("stress-ng",)
    repos = ("epel",)

    def run(self, ctx: RunContext):
        duration = ctx.config.getint("validate", "cpu_smoke_seconds", 60)
        timeout = ctx.config.timeout_for(self.id, self.default_timeout)
        res = ctx.cmd(
            [
                "stress-ng", "--cpu", "0", "--cpu-method", "all",
                "--verify", "--metrics", "-t", "%ds" % duration,
            ],
            timeout=timeout,
            artifact="stress-ng.log",
        )
        artifacts = [ctx.rel_artifact("stress-ng.log")]
        if res.timed_out:
            return self.result(
                Status.ERROR, reason="stress-ng exceeded its timeout", artifacts=artifacts
            )
        if res.returncode != 0:
            reason = STRESS_NG_EXIT.get(res.returncode, "unknown exit %d" % res.returncode)
            return self.result(
                Status.FAIL,
                reason="stress-ng: %s" % reason,
                details={"exit_code": res.returncode},
                artifacts=artifacts,
            )
        return self.result(
            Status.PASS,
            details={"duration_s": duration, "methods": "all", "verified": True},
            artifacts=artifacts,
        )


class CpuFlagsInfo(Test):
    """Report the CPU's advertised feature flags. Informational, never a verdict.

    The flags were already collected into ``inventory.cpus[].flags``, and that is
    still where the canonical copy lives - but the inventory is an opaque JSON blob
    on a run page, while results are the thing people actually read, filter, and
    compare. Collected-but-invisible is why this was reported as missing.

    Read from the inventory rather than by running ``lscpu`` again. A second
    invocation could disagree with the report over a hotplug or a governor change
    mid-run, and then a run would carry two different answers to the same question
    with nothing to say which was right.

    Never fails, and never gates a verdict: ``report.verdict`` skips
    informational results. Absent features are not listed, because these are
    capabilities and a CPU is not defective for lacking AMX.
    """

    id = "validate.cpu.flags"
    category = "cpu"
    run_type = "validate"
    severity = Severity.INFORMATIONAL
    default_timeout = 30

    def run(self, ctx: RunContext):
        cpus = (ctx.inventory or {}).get("cpus") or []
        flags = list((cpus[0] or {}).get("flags") or []) if cpus else []
        if not flags:
            # Nothing was measured, which is not a finding. A CPU that advertises
            # no flags, or an lscpu that could not be run, both land here.
            return self.result(
                Status.SKIP,
                reason="no CPU feature flags were reported by this system",
            )

        present = set(flags)
        notable = {
            group: [flag for flag in members if flag in present]
            for group, members in NOTABLE_FLAGS.items()
        }
        notable = {group: found for group, found in notable.items() if found}

        return self.result(
            Status.PASS,
            reason="%d CPU feature flags reported" % len(flags),
            details={
                "count": len(flags),
                # Grouped highlights first, because they are the part a reader
                # can use; the full list is for reference and for diffing two
                # machines.
                "notable": notable,
                "flags": flags,
            },
        )


REGISTRY.register(CpuFunctional)
REGISTRY.register(CpuFlagsInfo)
