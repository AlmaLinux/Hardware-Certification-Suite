"""Compression benchmarks on a deterministic generated corpus."""

from __future__ import annotations

import os
import re
import tempfile
import time

from ..data.corpus import write_corpus
from ..registry import REGISTRY, RunContext
from ..result import Direction, Metric
from .base import BenchmarkTest


class _CorpusBench(BenchmarkTest):
    category = "compression"
    default_timeout = 1200

    def setup(self, ctx: RunContext):
        self._workdir = tempfile.mkdtemp(prefix="alma-certify-corpus-")
        size_mb = ctx.config.getint("benchmark", "corpus_size_mb", 512)
        self._corpus = write_corpus(
            os.path.join(self._workdir, "corpus.bin"), size_mb=size_mb
        )
        self._size_mb = size_mb

    def teardown(self, ctx: RunContext):
        import shutil

        shutil.rmtree(getattr(self, "_workdir", ""), ignore_errors=True)


# zstd -b prints a progress-spinner line per iteration and only the final
# one is the measurement, so match all and take the last. The ratio gained an
# "x" prefix in zstd 1.5 ("(x3.063)" vs the older "(3.063)").
_ZSTD_RESULT_RE = re.compile(
    r"\(x?([\d.]+)\)\s*,\s*([\d.]+)\s*MB/s\s*,\s*([\d.]+)\s*MB/s"
)


class ZstdBench(_CorpusBench):
    id = "bench.compress.zstd"
    packages = ("zstd",)
    benchmark_version = "2"  # parser fix; values unchanged

    def run(self, ctx: RunContext):
        # zstd's built-in benchmark: level 3 (fast path) and 19 (heavy path)
        res = ctx.cmd(
            ["zstd", "-b3", "-e3", "-T0", self._corpus],
            timeout=self.default_timeout,
            artifact="zstd.log",
        )
        res19 = ctx.cmd(
            ["zstd", "-b19", "-e19", "-T0", self._corpus],
            timeout=self.default_timeout,
            artifact="zstd.log",
        )
        artifacts = [ctx.rel_artifact("zstd.log")]
        metrics = []
        details = {"corpus_mb": self._size_mb}
        for label, r, primary in (("l3", res, True), ("l19", res19, False)):
            matches = _ZSTD_RESULT_RE.findall(r.stdout + r.stderr)
            if matches:
                ratio, comp_speed, decomp_speed = matches[-1]
                metrics += [
                    Metric("comp_speed_%s" % label, float(comp_speed), "MB/s",
                           Direction.HIGHER, primary=primary),
                    Metric("decomp_speed_%s" % label, float(decomp_speed), "MB/s",
                           Direction.HIGHER),
                ]
                # The ratio is not a metric of this machine. The corpus is
                # generated deterministically and the level is fixed, so every
                # machine on earth returns the same number - three very different
                # systems all reported 6.76 at level 19. Recorded in details for
                # provenance; publishing it as a comparable metric invited
                # readers to rank hardware by a constant.
                details["ratio_%s" % label] = float(ratio)
        if not metrics:
            return self.bench_error("zstd benchmark output not parseable", artifacts)
        return self.bench_result(metrics, details=details, artifacts=artifacts)


class XzBench(_CorpusBench):
    id = "bench.compress.xz"
    packages = ("xz",)

    def run(self, ctx: RunContext):
        out_file = self._corpus + ".xz"
        t0 = time.monotonic()
        res = ctx.cmd(
            ["xz", "-6", "-T0", "-k", "-f", self._corpus],
            timeout=self.default_timeout,
            artifact="xz.log",
        )
        elapsed = time.monotonic() - t0
        artifacts = [ctx.rel_artifact("xz.log")]
        if not res.ok or not os.path.exists(out_file):
            return self.bench_error("xz compression failed", artifacts)
        in_size = os.path.getsize(self._corpus)
        out_size = os.path.getsize(out_file)
        speed = in_size / 1e6 / elapsed
        return self.bench_result(
            [
                Metric("comp_speed", round(speed, 1), "MB/s", Direction.HIGHER,
                       primary=True),
            ],
            # Ratio kept as provenance, not published as a metric: a fixed level
            # over a deterministic corpus gives the same figure everywhere, so
            # ranking machines by it ranks nothing.
            details={
                "corpus_mb": self._size_mb, "level": 6, "threads": "all",
                "ratio": round(in_size / out_size, 3),
            },
            artifacts=artifacts,
        )


REGISTRY.register(ZstdBench)
REGISTRY.register(XzBench)
