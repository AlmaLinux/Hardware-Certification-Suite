"""``--debug``: the raw output of every tool the suite runs.

Tracing sits in ``procutil.run_cmd`` because that is the single place every tool
goes through, so inventory collection, package installs, and every test are covered
at once and no caller opts in.

Two design points the tests pin because they are easy to break and expensive to
lose:

- **The command is announced before it runs.** A hung command produces no output
  to print afterwards, and knowing which one you are waiting on is the most useful
  thing a trace can say at that moment. See
  ``test_the_command_is_announced_before_it_runs``.
- **The trace goes to stderr.** ``alma-certify report --json`` writes JSON to stdout,
  so a trace there would corrupt anything piping it, and keeping them apart is
  what makes ``alma-certify validate --debug 2> trace.log`` work.
"""


import pytest

from alma_certify import cli, procutil


@pytest.fixture(autouse=True)
def _debug_off():
    """Tracing is process-global, so a test that enables it must not leak into the
    next one."""
    yield
    procutil.set_debug(None)


def _traced(argv, label=None, **kwargs):
    lines = []
    procutil.set_debug(lines.append, label=label)
    try:
        return lines, procutil.run_cmd(argv, **kwargs)
    finally:
        procutil.set_debug(None)


def _joined(lines):
    return "\n".join(lines)


# --- off by default -----------------------------------------------------------


def test_nothing_is_traced_by_default():
    """The flag is opt-in. Without it there must be no output and no sink."""
    assert procutil.debug_enabled() is False

    result = procutil.run_cmd(["echo", "quiet"])

    assert result.stdout.strip() == "quiet"


def test_set_debug_none_turns_it_back_off():
    procutil.set_debug(lambda line: None)
    assert procutil.debug_enabled() is True

    procutil.set_debug(None)

    assert procutil.debug_enabled() is False


# --- what the trace contains --------------------------------------------------


def test_the_command_is_shown_shell_quoted():
    """So the line can be pasted into a shell as-is. An argument with spaces
    printed bare would be two arguments when copied."""
    lines, _ = _traced(["echo", "two words"])

    assert "$ echo 'two words'" in _joined(lines)


def test_stdout_is_shown():
    lines, _ = _traced(["echo", "the-output"])

    assert "--- stdout ---" in _joined(lines)
    assert "  the-output" in lines


def test_stderr_is_shown_separately():
    """Which stream a message came out of is often the whole diagnosis."""
    lines, _ = _traced(["sh", "-c", "echo out; echo err >&2"])

    text = _joined(lines)
    assert "--- stdout ---" in text
    assert "--- stderr ---" in text
    assert "  out" in lines and "  err" in lines


def test_the_exit_code_is_shown():
    lines, _ = _traced(["sh", "-c", "exit 7"])

    assert "exit 7" in _joined(lines)


def test_silence_is_stated_rather_than_left_blank():
    """"Produced nothing" and "we did not capture it" look identical otherwise."""
    lines, _ = _traced(["true"])

    assert "  (no output)" in lines


def test_the_duration_is_shown():
    """A real elapsed time, not the ``0.00s`` a missing measurement would print.

    ``"in 0."`` matched ``in 0.00s``, so this passed with the measurement removed.
    Note ``"in 0.0" not in`` would be wrong: 0.051s renders as ``in 0.05s``.
    """
    lines, _ = _traced(["sh", "-c", "sleep 0.05"])

    text = _joined(lines)
    assert "in 0." in text
    assert "in 0.00s" not in text, "no time was actually measured"


def test_a_timeout_is_called_out():
    """A test that hangs is a common thing to debug, and the exit code alone
    (-9, killed) does not say it was a timeout rather than a crash."""
    lines, result = _traced(["sh", "-c", "sleep 5"], timeout=0.3)

    assert result.timed_out
    assert "TIMED OUT" in _joined(lines)


def test_the_timeout_is_shown_up_front():
    """So a long wait can be recognized as expected rather than a hang."""
    lines, _ = _traced(["true"], timeout=90)

    assert "(timeout 90s)" in _joined(lines)


def test_a_missing_command_is_traced():
    """A missing tool is the reason behind a great many skips. Without this the
    trace would simply have no entry for it, which reads as "never ran"."""
    lines = []
    procutil.set_debug(lines.append)

    with pytest.raises(procutil.CommandNotFound):
        procutil.run_cmd(["definitely-not-a-real-command-xyz"])

    assert "command not found" in _joined(lines)


# --- ordering: announced before execution -------------------------------------


def test_the_command_is_announced_before_it_runs(tmp_path):
    """The point of tracing a hang.

    Proven rather than assumed: the traced command creates a file, and the sink
    records whether that file exists at the moment the ``$`` line is emitted. If
    the announcement happened after execution, the file would already be there.
    """
    marker = tmp_path / "ran"
    seen_at_announce = {}

    def sink(line):
        if line.startswith("$ "):
            seen_at_announce["existed"] = marker.exists()

    procutil.set_debug(sink)
    procutil.run_cmd(["sh", "-c", "touch %s" % marker])

    assert marker.exists(), "the command did not actually run"
    assert seen_at_announce["existed"] is False, "announced only after running"


# --- labels -------------------------------------------------------------------


def test_each_command_is_labelled_with_the_current_test():
    """Otherwise the trace is a flat list and matching a command to its test means
    counting."""
    current = {"id": "validate.cpu.functional"}
    lines = []
    procutil.set_debug(lines.append, label=lambda: current["id"])

    procutil.run_cmd(["true"])
    current["id"] = "validate.memory.dmi"
    procutil.run_cmd(["true"])

    text = _joined(lines)
    assert "[validate.cpu.functional] $ true" in text
    assert "[validate.memory.dmi] $ true" in text


def test_no_label_means_no_brackets():
    """Inventory collection and package installs are not tests, so labelling them
    with an empty pair of brackets would invent a context."""
    lines, _ = _traced(["true"])

    assert "$ true" in _joined(lines)
    assert "[]" not in _joined(lines)


def test_a_broken_label_provider_does_not_break_the_run():
    """Debugging output must never be the thing that fails a run."""
    def explode():
        raise RuntimeError("boom")

    lines, result = _traced(["echo", "fine"], label=explode)

    assert result.ok
    assert "$ echo fine" in _joined(lines)


# --- duration is always measured ---------------------------------------------


def test_duration_is_recorded_even_with_tracing_off():
    """It is on CmdResult, not just in the trace, so any caller can use it.

    Sleeps, because ``>= 0.0`` was satisfied by the dataclass default of 0.0:
    replacing the real measurement at ``procutil.py:180`` with ``duration_s=0.0``
    left all 393 tests green.
    """
    result = procutil.run_cmd(["sh", "-c", "sleep 0.05"])

    assert result.duration_s >= 0.05, "the default 0.0 would satisfy a >= 0 check"
    assert isinstance(result.duration_s, float)


def test_a_hand_built_result_still_works():
    """The field is defaulted, so existing constructions are unaffected."""
    result = procutil.CmdResult(argv=["x"], returncode=0, stdout="", stderr="")

    assert result.duration_s == 0.0
    assert result.ok


# --- the CLI ------------------------------------------------------------------


@pytest.mark.parametrize(
    "command", ["collect", "validate", "benchmark", "run", "resume"]
)
def test_the_flag_is_on_every_command_that_runs_tools(command):
    argv = [command, "abc", "--debug"] if command == "resume" else [command, "--debug"]

    args = cli.build_parser().parse_args(argv)

    assert args.debug is True


@pytest.mark.parametrize("command", ["list", "report", "bundle"])
def test_the_flag_is_absent_where_it_would_do_nothing(command):
    """These run no tools. A flag that silently changed nothing would be worse than
    no flag."""
    argv = [command] if command == "list" else [command, "abc"]

    args = cli.build_parser().parse_args(argv)

    assert not hasattr(args, "debug")


class _State:
    def __init__(self, meta=None):
        self.meta = meta or {}


def test_the_trace_goes_to_stderr_not_stdout(capsys):
    """``report --json`` writes JSON to stdout, and ``2> trace.log`` has to work."""
    cli._enable_debug(_State())

    procutil.run_cmd(["echo", "traced"])

    captured = capsys.readouterr()
    assert "$ echo traced" in captured.err
    assert captured.out == "", "the trace would corrupt piped stdout"


def test_redact_gets_a_warning(capsys):
    """--redact is about report.json. The trace is raw tool output, so serials it
    would have stripped are visible - worth saying before someone pastes one into
    a bug report."""
    cli._enable_debug(_State({"redact": True}))

    err = capsys.readouterr().err
    assert "not redacted" in err


def test_without_redact_there_is_no_warning(capsys):
    cli._enable_debug(_State())

    assert "not redacted" not in capsys.readouterr().err


def test_the_label_follows_the_run_context(capsys):
    """The context is what carries the current test, so wiring it wrongly would
    lose every label."""
    class _Ctx:
        current_test_id = "validate.cpu.functional"

    cli._enable_debug(_State(), _Ctx())

    procutil.run_cmd(["true"])

    assert "[validate.cpu.functional]" in capsys.readouterr().err


def test_the_run_context_exposes_the_current_test():
    """``current_test_id`` is public so the tracer does not reach into a private
    attribute. The Runner sets the underlying value."""
    from alma_certify.registry import RunContext

    ctx = RunContext(
        config=None, run_dir="/tmp", inventory={}, pkg=None, peer=None,
        log=lambda msg: None,
    )
    assert ctx.current_test_id is None

    ctx._current_test_id = "validate.cpu.functional"

    assert ctx.current_test_id == "validate.cpu.functional"
