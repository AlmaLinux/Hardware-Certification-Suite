"""Crypto benchmarks via openssl speed."""

from __future__ import annotations

import re

from ..registry import REGISTRY, RunContext
from ..result import Direction, Metric
from .base import BenchmarkTest


def _last_column_kbytes(stdout: str, row_prefix: str):
    """Parse the summary table row; the last column is the largest block size."""
    for line in stdout.splitlines():
        if line.lower().startswith(row_prefix.lower()):
            columns = re.findall(r"([\d.]+)k", line)
            if columns:
                return float(columns[-1])
    return None


class _OpensslEvp(BenchmarkTest):
    category = "crypto"
    default_timeout = 300
    packages = ("openssl",)
    algo = ""
    row = ""

    def run(self, ctx: RunContext):
        res = ctx.cmd(
            ["openssl", "speed", "-evp", self.algo],
            timeout=self.default_timeout,
            artifact="openssl.log",
        )
        artifacts = [ctx.rel_artifact("openssl.log")]
        kbytes = _last_column_kbytes(res.stdout, self.row)
        if kbytes is None:
            return self.bench_error("openssl speed output not parseable", artifacts)
        return self.bench_result(
            [Metric("throughput", round(kbytes / 1000.0, 1), "MB/s", Direction.HIGHER,
                    primary=True)],
            details={"algorithm": self.algo, "block_size": "16KiB"},
            artifacts=artifacts,
        )


class OpensslAesGcm(_OpensslEvp):
    id = "bench.crypto.openssl-aes256gcm"
    algo = "aes-256-gcm"
    row = "AES-256-GCM"


class OpensslSha256(_OpensslEvp):
    id = "bench.crypto.openssl-sha256"
    algo = "sha256"
    row = "SHA256"


def parse_rsa_line(output: str, bits: int = 4096):
    """Return (signs/s, verifies/s) from `openssl speed rsa<bits>` output.

    The column count is version-dependent: OpenSSL 3.0 prints two timing
    columns (sign, verify) then two rates, while 3.4+ adds encrypt/decrypt,
    giving four of each. The rates are always the trailing bare numbers and
    sign/s comes first, so parse positionally from the end instead of
    hard-coding a column count.
    """
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("rsa"):
            continue
        if "%d bits" % bits not in stripped and "rsa%d" % bits not in stripped:
            continue
        # drop the label and the timing columns (which carry an "s" suffix)
        rates = [
            token for token in stripped.split()
            if re.fullmatch(r"[\d.]+", token) and "." in token
        ]
        # the leading number is the key size when written as "rsa 4096 bits"
        rates = [r for r in rates if float(r) != float(bits)]
        if len(rates) >= 2:
            return float(rates[0]), float(rates[1])
    return None


class OpensslRsa4096(BenchmarkTest):
    id = "bench.crypto.openssl-rsa4096"
    category = "crypto"
    default_timeout = 300
    packages = ("openssl",)
    benchmark_version = "2"  # parser fix for OpenSSL 3.4+ column layout

    def run(self, ctx: RunContext):
        res = ctx.cmd(
            ["openssl", "speed", "rsa4096"],
            timeout=self.default_timeout,
            artifact="openssl-rsa.log",
        )
        artifacts = [ctx.rel_artifact("openssl-rsa.log")]
        parsed = parse_rsa_line(res.stdout + res.stderr, bits=4096)
        if parsed is None:
            return self.bench_error("openssl rsa4096 output not parseable", artifacts)
        signs, verifies = parsed
        return self.bench_result(
            [
                Metric("signs_per_sec", signs, "ops/s", Direction.HIGHER,
                       primary=True),
                Metric("verifies_per_sec", verifies, "ops/s", Direction.HIGHER),
            ],
            artifacts=artifacts,
        )


REGISTRY.register(OpensslAesGcm)
REGISTRY.register(OpensslSha256)
REGISTRY.register(OpensslRsa4096)
