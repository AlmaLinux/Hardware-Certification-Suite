"""A required check that did not run is not a pass, and has to look like it.

Reported from a real run: three required GPU checks skipped because the suite could not find a
compiler that was installed, under a summary reading

    skip  3
    test verdict: PASS
      (the required tests passed; certification still needs review)

and an exit code of 0. Every part of that was wrong about the same thing. The required tests did not
pass, they did not run; the itemized list covered only fail and error, so nothing said which three
or why; and any CI or wrapper keying off the exit code saw success.

The policy is not new. ``runner.py`` already applies it to a package the platform cannot supply, in
a comment saying "an un-runnable check is a real gap in certification evidence". It was applied to a
missing package and not to a missing command.
"""


from alma_certify import cli, procutil
from alma_certify import report as report_mod
from alma_certify.config import Config
from alma_certify.registry import RunContext, Test
from alma_certify.result import Severity, Status
from alma_certify.runner import Runner
from alma_certify.state import RunState


class NeedsAMissingTool(Test):
    id = "validate.fake.needs-tool"
    category = "cpu"
    run_type = "validate"
    severity = Severity.REQUIRED

    def run(self, ctx: RunContext):
        raise procutil.CommandNotFound("definitely-not-a-tool")


class OptionalNeedsAMissingTool(NeedsAMissingTool):
    id = "validate.fake.optional-tool"
    severity = Severity.CONDITIONAL


class Passes(Test):
    id = "validate.fake.passes"
    category = "cpu"
    run_type = "validate"
    severity = Severity.REQUIRED

    def run(self, ctx: RunContext):
        return self.result(Status.PASS, reason="fine")


def _run_one(tmp_path, cls):
    """One test through the real runner, so the exception handling under test is the real one."""
    state = RunState.create(
        str(tmp_path), "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", {"run_types": ["validate"]},
    )
    runner = Runner(state, RunContext(Config.load("/nonexistent"), state.run_dir),
                    log=lambda msg: None)
    runner.execute([cls()])
    return {r["id"]: r for r in state.results()}


# --- the runner ----------------------------------------------------------------


def test_a_required_check_whose_tool_is_missing_is_an_error(tmp_path):
    """The policy this file already states for a missing package, applied to a missing command."""
    results = _run_one(tmp_path, NeedsAMissingTool)

    assert results[NeedsAMissingTool.id]["status"] == Status.ERROR
    assert "definitely-not-a-tool" in results[NeedsAMissingTool.id]["reason"]


def test_an_optional_check_whose_tool_is_missing_still_skips(tmp_path):
    """A machine with no BMC should not fail for having no BMC. That is what conditional means, and
    the distinction is the whole reason this is not a blanket change."""
    results = _run_one(tmp_path, OptionalNeedsAMissingTool)

    assert results[OptionalNeedsAMissingTool.id]["status"] == Status.SKIP


# --- the summary ---------------------------------------------------------------


def _report(results):
    return {
        "results": [
            {"id": r["id"], "status": r["status"], "run_type": "validate",
             "severity": r.get("severity"), "reason": r.get("reason", "")}
            for r in results
        ]
    }


def test_a_skipped_required_check_is_itemized(capsys):
    """One digit next to the word "skip" is not a report. The reasons are already recorded."""
    rep = _report([
        {"id": "validate.gpu.cuda-vectoradd", "status": "skip", "severity": "required",
         "reason": "no CUDA compiler found"},
        {"id": "validate.cpu.functional", "status": "pass", "severity": "required"},
    ])

    cli._print_summary(rep)

    out = capsys.readouterr().out
    assert "SKIP: validate.gpu.cuda-vectoradd - no CUDA compiler found" in out


def test_the_verdict_line_does_not_claim_the_required_tests_passed(capsys):
    """They did not. They did not run, which is a different thing and the thing worth saying."""
    rep = _report([
        {"id": "validate.gpu.cuda-vectoradd", "status": "skip", "severity": "required",
         "reason": "no CUDA compiler found"},
        {"id": "validate.cpu.functional", "status": "pass", "severity": "required"},
    ])

    cli._print_summary(rep)

    out = capsys.readouterr().out
    assert "test verdict: PASS" in out, "nothing failed, so the verdict itself is unchanged"
    assert "the required tests passed" not in out
    assert "did not run" in out
    assert "holes" in out


def test_a_conditional_skip_is_not_itemized(capsys):
    """A machine with no BMC skips several checks every run. Listing those would bury the ones that
    matter, which is how the original itemization got its scope right in the first place."""
    rep = _report([
        {"id": "validate.ipmi.bmc", "status": "skip", "severity": "conditional",
         "reason": "no BMC present"},
        {"id": "validate.cpu.functional", "status": "pass", "severity": "required"},
    ])

    cli._print_summary(rep)

    out = capsys.readouterr().out
    assert "validate.ipmi.bmc" not in out
    assert "the required tests passed" in out


def test_a_clean_run_still_reads_as_before(capsys):
    rep = _report([{"id": "validate.cpu.functional", "status": "pass", "severity": "required"}])

    cli._print_summary(rep)

    out = capsys.readouterr().out
    assert "the required tests passed; certification still needs review" in out
    assert "SKIP" not in out


# --- the exit code -------------------------------------------------------------


def test_the_verdict_helper_still_ignores_skips():
    """Unchanged on purpose. ``verdict`` answers "did anything fail", and a skip is not a failure.
    The fix is that a required tool-missing skip is now an error, so it reaches this as one."""
    assert report_mod.verdict(_report([
        {"id": "x", "status": "skip", "severity": "required", "reason": ""},
    ])) is True


def test_an_error_reaches_the_exit_code(tmp_path):
    """Which is what any CI or wrapper keys off, and what reported success before."""
    results = _run_one(tmp_path, NeedsAMissingTool)

    statuses = {r["status"] for r in results.values()}

    assert Status.ERROR in statuses
    # cli._finish maps an error status to EXIT_ERROR; that mapping is what this asserts against.
    assert cli.EXIT_ERROR == 2
