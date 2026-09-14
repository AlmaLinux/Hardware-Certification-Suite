"""Resume journal and runner semantics."""

import pytest

from alma_certify import procutil
from alma_certify.config import Config
from alma_certify.registry import Registry, RunContext, Test
from alma_certify.result import Severity, Status
from alma_certify.runner import Runner
from alma_certify.state import RunState


def make_state(tmp_path, run_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"):
    return RunState.create(str(tmp_path), run_id, {"run_types": ["validate"]})


def make_ctx(state):
    return RunContext(Config.load("/nonexistent"), state.run_dir)


class Passes(Test):
    id = "validate.fake.passes"
    category = "fake"
    run_type = "validate"
    severity = Severity.REQUIRED

    def run(self, ctx):
        return self.result(Status.PASS)


class Crashes(Test):
    id = "validate.fake.crashes"
    category = "fake"
    run_type = "validate"

    def run(self, ctx):
        raise RuntimeError("boom")


class Skips(Test):
    id = "validate.fake.skips"
    category = "fake"
    run_type = "validate"

    def applicable(self, ctx):
        return "hardware not present"

    def run(self, ctx):
        raise AssertionError("must not run")


def test_runner_records_pass_skip_and_error(tmp_path):
    state = make_state(tmp_path)
    runner = Runner(state, make_ctx(state), log=lambda m: None)
    runner.execute([Passes(), Crashes(), Skips()])

    results = {r["id"]: r for r in state.results()}
    assert results["validate.fake.passes"]["status"] == "pass"
    assert results["validate.fake.crashes"]["status"] == "error"
    assert "boom" in results["validate.fake.crashes"]["reason"]
    assert results["validate.fake.skips"]["status"] == "skip"
    assert results["validate.fake.skips"]["reason"] == "hardware not present"


def test_resume_skips_completed(tmp_path):
    state = make_state(tmp_path)
    runner = Runner(state, make_ctx(state), log=lambda m: None)
    runner.execute([Passes()])

    # reload as a resume would
    reloaded = RunState.load(str(tmp_path), state.run_id)
    assert reloaded.is_completed("validate.fake.passes")

    ran = []

    class Sentinel(Passes):
        id = "validate.fake.passes"

        def run(self, ctx):
            ran.append(1)
            return self.result(Status.PASS)

    Runner(reloaded, make_ctx(reloaded), log=lambda m: None).execute([Sentinel()])
    assert ran == []  # not re-run


def test_state_find_by_prefix(tmp_path):
    state = make_state(tmp_path)
    found = RunState.find(str(tmp_path), state.run_id[:8])
    assert found.run_id == state.run_id
    with pytest.raises(FileNotFoundError):
        RunState.find(str(tmp_path), "ffffffff")


def test_registry_selection():
    reg = Registry()

    class Inter(Passes):
        id = "validate.fake.inter"
        interactive = True

    for cls in (Passes, Inter):
        reg.register(cls)

    default = {c.id for c in reg.select("validate")}
    assert default == {"validate.fake.passes"}
    with_flags = {c.id for c in reg.select("validate", interactive=True)}
    assert with_flags == {"validate.fake.passes", "validate.fake.inter"}
    only = {c.id for c in reg.select("validate", only=["validate.fake.passes"])}
    assert only == {"validate.fake.passes"}


def test_registry_rejects_duplicates():
    reg = Registry()
    reg.register(Passes)
    with pytest.raises(ValueError):
        reg.register(Passes)


# --- unavailable tooling is a skip, not an error -----------------------------


class NeedsMissingCommand(Test):
    id = "bench.fake.missing-tool"
    category = "fake"
    run_type = "benchmark"
    severity = None

    def run(self, ctx):
        raise procutil.CommandNotFound("some-tool")


class RequiredValidate(Passes):
    id = "validate.fake.required"
    severity = Severity.REQUIRED
    packages = ("nonexistent-pkg",)


class OptionalBenchmark(Passes):
    id = "bench.fake.optional"
    run_type = "benchmark"
    severity = None
    packages = ("nonexistent-pkg",)


def test_missing_command_skips_instead_of_erroring(tmp_path):
    """A tool the platform does not ship is a coverage gap, not a crash. It
    must not surface as 'unhandled exception'."""
    state = make_state(tmp_path)
    Runner(state, make_ctx(state), log=lambda m: None).execute([NeedsMissingCommand()])

    result = state.results()[0]
    assert result["status"] == "skip"
    assert "command not found" in result["reason"]
    assert "some-tool" in result["reason"]


def test_unavailable_package_skips_benchmark_but_errors_required_validate(tmp_path):
    """An optional benchmark that cannot be installed is skipped; a required
    validation test that cannot run is a genuine certification gap."""
    state = make_state(tmp_path)
    ctx = make_ctx(state)

    class FakePkg:
        def missing(self, packages, repos=(), timeout=900):
            return list(packages)

        def ensure(self, packages, repos=(), timeout=900):
            return False

    ctx.pkg = FakePkg()
    runner = Runner(state, ctx, log=lambda m: None)
    tests = [RequiredValidate(), OptionalBenchmark()]
    unavailable = runner.prepare_packages(tests)
    assert unavailable == {
        "validate.fake.required": ["nonexistent-pkg"],
        "bench.fake.optional": ["nonexistent-pkg"],
    }
    runner.execute(tests, skip_pkg_failed=unavailable)

    results = {r["id"]: r for r in state.results()}
    assert results["bench.fake.optional"]["status"] == "skip"
    assert results["validate.fake.required"]["status"] == "error"
    # the reason names the package, so a failed run is self-diagnosing
    for r in results.values():
        assert "nonexistent-pkg" in r["reason"]
