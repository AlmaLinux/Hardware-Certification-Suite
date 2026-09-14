"""Run-plan execution: ordering, timeouts, resume, package prep."""

from __future__ import annotations

import datetime
import time
import traceback
from typing import Callable, Dict, List, Optional

from . import procutil
from .registry import RunContext, Test
from .result import Severity, Status
from .state import RunState


def utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Runner:
    def __init__(
        self,
        state: RunState,
        ctx: RunContext,
        log: Callable[[str], None],
        cooldown_seconds: float = 0,
        *,
        progress: Optional[Callable[..., None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ):
        self.state = state
        self.ctx = ctx
        self.log = log
        self.cooldown_seconds = cooldown_seconds
        # A driver's progress display and Cancel, both optional; a plain caller passes neither and
        # gets a no-op and a run that never stops early, which is the CLI's behavior.
        self._progress = progress or (lambda **_: None)
        self._should_stop = should_stop or (lambda: False)

    def prepare_packages(self, tests: List[Test]) -> Dict[str, List[str]]:
        """Install the union of required packages.

        Returns ``{test_id: [packages still missing]}`` so the caller can
        report exactly which package was unavailable instead of a vague
        "could not be installed".
        """
        unavailable: Dict[str, List[str]] = {}
        by_repos: Dict[tuple, List[Test]] = {}
        for test in tests:
            if not test.packages:
                continue
            by_repos.setdefault(tuple(sorted(test.repos)), []).append(test)
        for repos, group in by_repos.items():
            packages = sorted({p for t in group for p in t.packages})
            self.log("installing packages: %s" % ", ".join(packages))
            still_missing = set(self.ctx.pkg.missing(packages, repos=repos))
            if not still_missing:
                continue
            for t in group:
                own = sorted(still_missing.intersection(t.packages))
                if own:
                    unavailable[t.id] = own
        return unavailable

    def execute(
        self,
        tests: List[Test],
        skip_pkg_failed: Optional[Dict[str, List[str]]] = None,
    ) -> None:
        skip_pkg_failed = skip_pkg_failed or {}
        runnable = [t for t in tests if not self.state.is_completed(t.id)]
        already = len(tests) - len(runnable)
        if already:
            self.log("resume: %d test(s) already completed, skipping" % already)

        for i, test in enumerate(runnable):
            # Checked at the test boundary, not mid-test: a driver's Cancel stops the run cleanly
            # between tests rather than killing one part way and recording a false failure.
            if self._should_stop():
                self.log("stopped before %s at your request" % test.id)
                break
            self.ctx._current_test_id = test.id
            self._progress(test_id=test.id, index=i + 1, total=len(runnable), status=None)
            started = utcnow_iso()
            t0 = time.monotonic()

            if test.id in skip_pkg_failed:
                reason = "package(s) unavailable in the configured repos: %s" % (
                    ", ".join(skip_pkg_failed[test.id])
                )
                # A tool this platform cannot supply is "not applicable", not
                # a defect - except for a required validation test, where an
                # un-runnable check is a real gap in certification evidence.
                if test.run_type == "validate" and test.severity == Severity.REQUIRED:
                    result = test.result(Status.ERROR, reason=reason)
                else:
                    result = test.result(Status.SKIP, reason=reason)
                self.log("%s %s (%s)" % (result.status.upper(), test.id, reason))
            else:
                skip_reason = None
                try:
                    skip_reason = test.applicable(self.ctx)
                except Exception as exc:  # applicability must never crash the run
                    skip_reason = "applicability check failed: %s" % exc
                if skip_reason is not None:
                    result = test.result(Status.SKIP, reason=skip_reason)
                    self.log("SKIP %s (%s)" % (test.id, skip_reason))
                else:
                    self.log(
                        "[%d/%d] running %s" % (i + 1, len(runnable), test.id)
                    )
                    result = self._run_one(test)

            result.started_at = started
            if result.duration_s is None:
                result.duration_s = round(time.monotonic() - t0, 2)
            self.state.record_result(result.to_dict())
            self.log("%s %s (%.1fs)" % (result.status.upper(), test.id, result.duration_s))
            self._progress(test_id=test.id, index=i + 1, total=len(runnable),
                           status=result.status)

            if (
                self.cooldown_seconds
                and result.status not in (Status.SKIP,)
                and i + 1 < len(runnable)
            ):
                time.sleep(self.cooldown_seconds)
        self.ctx._current_test_id = None

    def _run_one(self, test: Test):
        try:
            test.setup(self.ctx)
        except Exception as exc:
            return test.result(Status.ERROR, reason="setup failed: %s" % exc)
        try:
            result = test.run(self.ctx)
        except KeyboardInterrupt:
            raise
        except procutil.CommandNotFound as exc:
            # The tool is not installed at all. That is a platform gap, not a crash, so say which
            # command was missing rather than surfacing an opaque "unhandled exception".
            #
            # For a *required* validation test it is an error, which is the policy this file
            # already states a few lines up for a package the platform cannot supply: "an
            # un-runnable check is a real gap in certification evidence". That was applied to a
            # missing package and not to a missing command, and the difference is invisible from
            # outside: reported as three required GPU checks skipping with "nvcc not present" on a
            # machine that had it, under a summary reading "test verdict: PASS" and exiting 0.
            #
            # A skip is not a pass, and nothing downstream could tell.
            reason = "command not found: %s" % exc
            if test.run_type == "validate" and test.severity == Severity.REQUIRED:
                result = test.result(Status.ERROR, reason=reason)
            else:
                result = test.result(Status.SKIP, reason=reason)
        except Exception as exc:
            tb_path = self.ctx.artifact_path("traceback.txt", test_id=test.id)
            with open(tb_path, "w", encoding="utf-8") as fh:
                fh.write(traceback.format_exc())
            result = test.result(
                Status.ERROR,
                reason="unhandled exception: %s" % exc,
                artifacts=[self.ctx.rel_artifact("traceback.txt", test_id=test.id)],
            )
        finally:
            try:
                test.teardown(self.ctx)
            except Exception as exc:
                self.log("warning: teardown of %s failed: %s" % (test.id, exc))
        return result
