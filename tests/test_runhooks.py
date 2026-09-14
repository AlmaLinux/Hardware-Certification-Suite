"""The run/driver seam: RunHooks, TerminalHooks, and the Runner's progress and stop callbacks.

The wider seam that lets the Textual TUI host a run instead of only launching it. The CLI keeps its
behavior because it drives runs with TerminalHooks (covered indirectly by the rest of the suite);
these pin the parts a resident driver relies on.
"""

from alma_certify.config import Config
from alma_certify.registry import RunContext, Test
from alma_certify.result import Severity, Status
from alma_certify.runhooks import RunHooks, TerminalHooks
from alma_certify.runner import Runner
from alma_certify.state import RunState


def _state(tmp_path):
    return RunState.create(str(tmp_path), "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                           {"run_types": ["validate"]})


def _ctx(state):
    return RunContext(Config.load("/nonexistent"), state.run_dir)


class _A(Test):
    id = "validate.fake.a"
    category = "fake"
    run_type = "validate"
    severity = Severity.REQUIRED

    def run(self, ctx):
        return self.result(Status.PASS)


class _B(_A):
    id = "validate.fake.b"


def test_base_hooks_are_headless():
    hooks = RunHooks()
    assert hooks.should_stop() is False
    # The no-ops must not raise: a run driven by the bare base simply reports nowhere.
    hooks.log("x")
    hooks.out("y")
    hooks.status("z")
    hooks.progress(test_id="t", index=1, total=1, status=None)


def test_terminal_hooks_status_prints_but_is_not_logged(tmp_path, capsys):
    """A status line is a transient 'now doing' marker: printed (so a blocking step shows something
    while it runs) but kept out of the run log, which records results, not what is in flight."""
    hooks = TerminalHooks(str(tmp_path))
    hooks.status("keeping the machine awake for the run")

    assert capsys.readouterr().out == "... keeping the machine awake for the run\n"
    assert not (tmp_path / "alma-certify.log").exists()


def test_terminal_hooks_log_is_timestamped_and_written(tmp_path, capsys):
    hooks = TerminalHooks(str(tmp_path))
    hooks.log("collecting inventory")

    printed = capsys.readouterr().out
    assert printed.startswith("[") and "collecting inventory" in printed
    assert "collecting inventory" in (tmp_path / "alma-certify.log").read_text(encoding="utf-8")


def test_terminal_hooks_out_is_plain(tmp_path, capsys):
    TerminalHooks(str(tmp_path)).out("== summary ==")
    assert capsys.readouterr().out == "== summary ==\n"


def test_terminal_hooks_authorize_prints_the_url_and_code(tmp_path, capsys, monkeypatch):
    """The terminal front-end shows the device-flow prompt the way `alma-certify register` does."""
    from alma_certify.submit import auth

    # A QR only where a terminal can show one; suppress it here so the test is about the fallback.
    monkeypatch.setattr(auth.qr, "render_block", lambda data, **kw: None)
    TerminalHooks(str(tmp_path)).authorize(
        verification_uri="https://lumina.example/my/activate/",
        user_code="AAAA-1111",
        complete_uri="https://lumina.example/my/activate/?code=AAAA-1111",
    )
    out = capsys.readouterr().out
    assert "https://lumina.example/my/activate/" in out
    assert "AAAA-1111" in out


def test_runner_emits_a_start_and_finish_for_each_test(tmp_path):
    state = _state(tmp_path)
    events = []
    Runner(state, _ctx(state), log=lambda m: None,
           progress=lambda **kw: events.append(kw)).execute([_A(), _B()])

    starts = [e for e in events if e["status"] is None]
    finishes = [e for e in events if e["status"] is not None]
    assert len(starts) == 2 and len(finishes) == 2
    assert all(e["total"] == 2 for e in events)
    assert {e["status"] for e in finishes} == {"pass"}


def test_should_stop_halts_before_the_next_test(tmp_path):
    """Checked at the boundary: the first test runs, then the stop is honored before the second."""
    state = _state(tmp_path)
    Runner(state, _ctx(state), log=lambda m: None,
           should_stop=lambda: len(state.results()) >= 1).execute([_A(), _B()])

    ran = [r["id"] for r in state.results()]
    assert ran == ["validate.fake.a"], "the second test should not have run after the stop"


# --- questions the run has to stop and ask ---------------------------------------
#
# Reported: the interface hung on a progress bar, with a status line still naming the previous step,
# on a machine that had the NVIDIA drivers but not the CUDA packages. The run had reached the GPU
# setup offer and called ``input()``. Under Textual, stdin belongs to the app, so nothing showed the
# prompt and nothing could answer it. The same command in plain terminal asked and carried on.
#
# So a question is a hook like any other report, and the one ``input()`` left in the engine lives in
# TerminalHooks.


def test_the_base_driver_cannot_ask_and_says_so():
    """None, not False. A headless driver having no way to ask is a different thing from somebody
    answering no, and the callers say which flag to pass only in the first case."""
    assert RunHooks().confirm("install this now?") is None
    RunHooks().note("advice with nowhere to go")  # must not raise


def test_the_engine_never_reads_stdin_itself():
    """The regression guard. A prompt anywhere in the engine is invisible and unanswerable under the
    interface, so the only ``input()`` in the package belongs to the terminal driver."""
    import pathlib

    import alma_certify

    root = pathlib.Path(alma_certify.__file__).parent
    # hostos.confirm is the unsupported-OS question, which is asked before a run exists and so has
    # no hooks to ask through; it takes an injectable reader instead.
    drivers = {"runhooks.py", "hostos.py"}
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if path.name in drivers:
            continue
        source = path.read_text(encoding="utf-8").replace("_input(", "")
        if "input(" in source:
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], "these read stdin directly; ask through RunHooks.confirm instead"


def test_a_terminal_prompt_reads_the_way_it_always_did(monkeypatch):
    """The wording is the CLI's existing wording, built from the labels: the default answer is the
    capitalized one, so a bare Enter is visibly the safe choice."""
    seen = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": seen.append(prompt) or "")

    TerminalHooks().confirm("install this now?")
    TerminalHooks().confirm(
        "Reboot and run again for GPU coverage, or continue this run without the GPU?",
        yes_label="Reboot", no_label="continue", default=True,
    )

    assert seen[0] == "install this now? [y/N] "
    assert seen[1].endswith("[Reboot/continue] ")


def test_a_closed_stdin_is_not_an_answer(monkeypatch):
    """Prompting into a kickstart %post would hang it forever and reading EOF as consent would opt
    somebody into a third-party repository silently. Neither: it reports that it cannot ask."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    assert TerminalHooks().confirm("install this now?") is None


def test_a_typo_takes_the_safe_answer_rather_than_guessing(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    for typed, default, expected in [
        ("maybe", False, False), ("maybe", True, True),
        ("", False, False), ("", True, True),
        ("yes", False, True), ("N", True, False),
    ]:
        monkeypatch.setattr("builtins.input", lambda prompt="", t=typed: t)
        assert TerminalHooks().confirm("go?", default=default) is expected


def test_the_terminal_driver_needs_no_run_directory(capsys):
    """``setup-gpu`` asks the same questions with no run to log into."""
    TerminalHooks().log("standalone")
    TerminalHooks().note("to stderr")
    captured = capsys.readouterr()
    assert "standalone" in captured.out
    assert "to stderr" in captured.err


# --- the step being reported is the step now running ---------------------------------
#
# Reported by a tester: the interface sat on "checking your submission token" long after the token
# had been collected, with the elapsed counter climbing, and read as a hang. It was not the token:
# nothing between that step and the first package install set a status, so the last one set stayed
# on screen through the whole of inventory collection - which is the slowest pre-test step there is
# on a machine with a shelf of drives, at up to a minute of smartctl each.


def test_inventory_collection_says_which_collector_is_running(tmp_path):
    """One line per collector, so a long one is visibly progressing rather than stuck."""
    from alma_certify import inventory

    said = []
    inventory.collect_all(str(tmp_path), status=said.append)

    assert said, "inventory has to report the step it is on"
    assert all(line.startswith("collecting hardware inventory: ") for line in said), said
    named = {line.rsplit(": ", 1)[1] for line in said}
    assert named == set(inventory.COLLECTORS), "every collector, or the gap is where it looks stuck"


def test_inventory_collection_still_works_with_nothing_watching(tmp_path):
    """The CLI passes no status callback, and a survey run driven by the base hooks passes none
    either. Neither may be a reason for inventory to fail."""
    from alma_certify import inventory

    assert "summary" in inventory.collect_all(str(tmp_path))


# --- a cancel before the tests start ---------------------------------------------------
#
# The test loop has checked ``should_stop`` between tests since it existed. Nothing before it did,
# so a run cancelled during authorization, a driver install, or inventory collection went on to
# completion while the interface reported that it was stopping - which is the other half of "I hit
# escape and nothing happened".


class _Stopping(RunHooks):
    def __init__(self):
        self.lines = []

    def log(self, msg):
        self.lines.append(msg)

    def should_stop(self):
        return True


def test_a_cancel_before_the_tests_stops_before_inventory(tmp_path, monkeypatch):
    from alma_certify import cli, inventory

    collected = []
    monkeypatch.setattr(inventory, "collect_all",
                        lambda *a, **kw: collected.append(a) or {"summary": {}})
    state = RunState.create(str(tmp_path), "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                            {"run_types": ["collect"], "submit": False})
    hooks = _Stopping()

    code = cli._execute_tests(state, Config.load("/nonexistent"), ["collect"], hooks=hooks)

    assert code == cli.EXIT_OK, "cancelling is not an error"
    assert collected == [], "inventory is the slow step a cancel most needs to skip"
    assert any("at your request" in line for line in hooks.lines), hooks.lines
