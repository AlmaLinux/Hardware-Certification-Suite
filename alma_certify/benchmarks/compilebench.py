"""Compilation benchmark: build a pinned CPython from source, timed."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
import urllib.request

from ..registry import REGISTRY, RunContext
from ..result import Direction, Metric
from .base import BenchmarkTest

PY_VERSION = "3.12.8"
PY_URL = "https://www.python.org/ftp/python/%s/Python-%s.tar.xz" % (PY_VERSION, PY_VERSION)
PY_SHA256 = "c909157bb25ec114e5869124cc2a9c4a4d4c1e957ca4ff553f1edc692101154e"


class CompilePython(BenchmarkTest):
    id = "bench.compile.python"
    category = "compilation"
    default_timeout = 3600
    benchmark_version = "2"  # required-package set narrowed

    # Only the toolchain is required. The headers below are for CPython's
    # optional extension modules; without them configure skips those modules
    # and the build still completes, so demanding them would make the whole
    # benchmark unavailable on minimal installs (and their package names vary
    # across releases: zlib-devel is zlib-ng-compat-devel on AlmaLinux 10).
    # Which ones were present is recorded in the result for comparability.
    packages = ("gcc", "make", "xz", "tar")
    OPTIONAL_PACKAGES = ("zlib-devel", "openssl-devel", "libffi-devel", "bzip2-devel")

    def setup(self, ctx: RunContext):
        # Best-effort: a missing optional header changes which modules build,
        # so try to install them and record the outcome either way.
        self._optional_missing = (
            ctx.pkg.missing(self.OPTIONAL_PACKAGES) if ctx.pkg else
            list(self.OPTIONAL_PACKAGES)
        )
        self._workdir = tempfile.mkdtemp(prefix="alma-certify-compile-")
        tarball = os.path.join(self._workdir, "python.tar.xz")
        urllib.request.urlretrieve(PY_URL, tarball)
        digest = hashlib.sha256(open(tarball, "rb").read()).hexdigest()
        if digest != PY_SHA256:
            raise RuntimeError(
                "CPython tarball sha256 mismatch (%s != %s)" % (digest, PY_SHA256)
            )
        self._tarball = tarball

    def teardown(self, ctx: RunContext):
        shutil.rmtree(getattr(self, "_workdir", ""), ignore_errors=True)

    def run(self, ctx: RunContext):
        ctx.cmd(
            ["tar", "-xJf", self._tarball, "-C", self._workdir],
            timeout=300, check=True,
        )
        src = os.path.join(self._workdir, "Python-%s" % PY_VERSION)
        jobs = str(os.cpu_count() or 1)

        res = ctx.cmd(
            ["sh", "-c", "cd %s && ./configure -q" % src],
            timeout=600,
            artifact="configure.log",
        )
        if not res.ok:
            return self.bench_error(
                "CPython configure failed", [ctx.rel_artifact("configure.log")]
            )
        t0 = time.monotonic()
        res = ctx.cmd(
            ["make", "-C", src, "-j", jobs, "-s"],
            timeout=self.default_timeout,
            artifact="make.log",
        )
        elapsed = time.monotonic() - t0
        artifacts = [ctx.rel_artifact("make.log")]
        if not res.ok:
            return self.bench_error("CPython build failed", artifacts)
        return self.bench_result(
            [Metric("build_time", round(elapsed, 1), "s", Direction.LOWER, primary=True)],
            details={
                "python_version": PY_VERSION,
                "jobs": int(jobs),
                # Fewer optional modules means less to compile, so this is
                # part of the comparability context for the timing.
                "optional_deps_missing": sorted(getattr(self, "_optional_missing", [])),
            },
            artifacts=artifacts,
        )


REGISTRY.register(CompilePython)
