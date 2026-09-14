"""``cmd_tui``: reaching the guided interface, and handing off the command it returns.

None of this needs the interface itself. The front-end is stubbed through ``_select_tui`` so these
pin the CLI's half of the contract: when it opens the interface, how it carries the global options
into what the interface hands back, the root check on that command, and the exit codes. The
interface's own navigation is covered in test_textual_tui.py.
"""

import argparse

import pytest

from alma_certify import cli


class FakeFrontend:
    """Stands in for ``alma_certify_tui``: reports itself drawable, returns a fixed argv."""

    def __init__(self, argv=None):
        self._argv = argv
        self.started_with = None

    def unavailable_reason(self):
        return None

    def start(self, **kwargs):
        self.started_with = kwargs
        return self._argv


# --- reaching it -----------------------------------------------------------------


def test_a_bare_command_without_a_terminal_still_prints_help(monkeypatch, capsys):
    """The compatibility guarantee. A script or a kickstart %post that calls the bare command has no
    terminal, so it sees exactly what it always did."""
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: False)

    assert cli.main([]) == cli.EXIT_USAGE
    assert "usage:" in capsys.readouterr().out


def test_a_bare_command_at_a_terminal_opens_the_interface(monkeypatch):
    opened = []
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: True)
    monkeypatch.setattr(cli, "cmd_tui", lambda args: opened.append(args) or cli.EXIT_OK)

    assert cli.main([]) == cli.EXIT_OK
    assert opened


def test_the_command_refuses_politely_without_a_terminal(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: False)

    assert cli.cmd_tui(argparse.Namespace()) == cli.EXIT_USAGE
    assert "needs a terminal" in capsys.readouterr().err


def test_a_terminal_it_cannot_draw_on_still_gets_the_commands(monkeypatch, capsys):
    """When no interface can draw here (not installed, or an unusable terminal), the reader still
    gets the plain commands rather than a traceback."""
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: True)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "_select_tui", lambda: (None, "the guided interface is not installed"))

    code = cli.cmd_tui(argparse.Namespace())
    out = capsys.readouterr()

    assert code == cli.EXIT_USAGE
    assert "not installed" in out.err
    for command in ("alma-certify validate", "alma-certify benchmark",
                    "alma-certify run", "alma-certify runs"):
        assert command in out.out


# --- the hand-off ----------------------------------------------------------------


def handoff(monkeypatch, argv, **namespace):
    """Run ``cmd_tui`` with the interface stubbed, and record what it hands to ``main``."""
    recorded = []
    fake = FakeFrontend(argv)
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: True)
    monkeypatch.setattr(cli, "_select_tui", lambda: (fake, None))
    monkeypatch.setattr(cli, "main", lambda handed: recorded.append(handed) or cli.EXIT_OK)
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    defaults = {"config": None, "server": None, "run_dir": None}
    defaults.update(namespace)
    code = cli.cmd_tui(argparse.Namespace(**defaults))
    return code, recorded


def test_the_command_shown_is_the_command_handed_off(monkeypatch, capsys):
    code, recorded = handoff(monkeypatch, ["run", "--scope", "gpu"])

    assert code == cli.EXIT_OK
    assert recorded == [["run", "--scope", "gpu"]]
    assert "$ alma-certify run --scope gpu" in capsys.readouterr().out


def test_the_globals_it_was_started_with_are_carried_through(monkeypatch, capsys):
    """Without this the interface listed runs from ``--run-dir`` and handed off a command that
    looked in the default one, so "Resume it" on a run it had shown ended in "run not found"."""
    code, recorded = handoff(
        monkeypatch, ["resume", "9f3c1a2e"], run_dir="/srv/runs", server="https://lumina.example",
    )

    assert recorded == [[
        "--server", "https://lumina.example", "--run-dir", "/srv/runs", "resume", "9f3c1a2e",
    ]]
    assert "--run-dir /srv/runs" in capsys.readouterr().out
    assert code == cli.EXIT_OK


def test_nothing_is_handed_off_when_nobody_asked_for_anything(monkeypatch):
    code, recorded = handoff(monkeypatch, None)

    assert code == cli.EXIT_OK
    assert recorded == []


@pytest.mark.gate
def test_the_interface_requires_root(monkeypatch, capsys):
    """The interface hosts the run in-process, so it needs root like everything else.

    The check used to be cmd_tui's own, which meant the interface and the commands had two of them
    saying different things. It is one gate at the entry point now, so this pins that the interface
    is behind it and that nothing starts when it is refused.
    """
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: True)
    monkeypatch.setattr(cli.elevate, "is_root", lambda: False)
    monkeypatch.setattr(cli.elevate, "sudo_available", lambda: False)
    opened = []
    monkeypatch.setattr(cli, "cmd_tui", lambda args: opened.append(args) or cli.EXIT_OK)

    code = cli.main(["tui"])
    err = capsys.readouterr().err

    assert code == cli.EXIT_USAGE
    assert "must run as root" in err
    assert not opened, "nothing should start when the interface is refused for lack of root"


def test_interrupting_the_interface_reads_the_same_from_both_entry_points(monkeypatch, capsys):
    """Exit codes are a documented contract. The bare command dispatched outside the guard, so
    Ctrl-C printed a traceback and died by signal there, while ``alma-certify tui`` said
    "interrupted"."""
    def interrupt(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_interactive_terminal", lambda: True)
    monkeypatch.setattr(cli, "cmd_tui", interrupt)

    for argv in ([], ["tui"]):
        assert cli.main(argv) == cli.EXIT_ERROR
        assert "interrupted" in capsys.readouterr().err


# --- terminal detection ----------------------------------------------------------


def test_both_streams_have_to_be_a_terminal(monkeypatch):
    """``alma-certify | tee`` has a terminal on stdin and a pipe on stdout, and a full-screen
    interface drawn into a pipe is nobody's idea of a log."""
    class Stream:
        def __init__(self, tty):
            self._tty = tty

        def isatty(self):
            return self._tty

    for stdin, stdout, expected in ((True, True, True), (True, False, False),
                                    (False, True, False), (False, False, False)):
        monkeypatch.setattr(cli.sys, "stdin", Stream(stdin))
        monkeypatch.setattr(cli.sys, "stdout", Stream(stdout))
        assert cli._interactive_terminal() is expected


def test_a_closed_stream_is_not_a_terminal(monkeypatch):
    class Closed:
        def isatty(self):
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(cli.sys, "stdin", Closed())
    monkeypatch.setattr(cli.sys, "stdout", Closed())

    assert cli._interactive_terminal() is False


def test_a_stream_that_is_not_there_is_not_a_terminal(monkeypatch, capsys):
    """``sys.stdin`` is None under pythonw and some supervisors, and ``None.isatty()`` was an
    AttributeError and exit 1 where the contract says help and exit 3."""
    monkeypatch.setattr(cli.sys, "stdin", None)
    monkeypatch.setattr(cli.sys, "stdout", None)

    assert cli._interactive_terminal() is False
    assert cli.main([]) == cli.EXIT_USAGE


def test_a_stream_whose_isatty_raises_oserror_is_not_a_terminal(monkeypatch):
    class Hostile:
        def isatty(self):
            raise OSError("detached")

    monkeypatch.setattr(cli.sys, "stdin", Hostile())
    monkeypatch.setattr(cli.sys, "stdout", Hostile())

    assert cli._interactive_terminal() is False


# --- the report summary the interface's pager shows (a CLI helper, not the interface) ---


def test_the_summary_lines_are_lines():
    """One of them held an embedded newline. ``print`` swallows the difference; a pager draws it as
    one row and wipes the frame border."""
    rep = {"results": [
        {"id": "validate.cpu.flags", "status": "pass", "run_type": "validate",
         "severity": "required"},
    ]}

    lines = cli._summary_lines(rep)

    assert lines, "there should be something to show"
    assert not any("\n" in line for line in lines)


def test_the_printed_summary_is_unchanged_by_that(capsys):
    """The stdout it produces has to be byte-identical to what it printed before the split."""
    rep = {"results": [
        {"id": "validate.net.link", "status": "fail", "run_type": "validate",
         "severity": "required", "reason": "no carrier"},
    ]}

    cli._print_summary(rep)

    assert capsys.readouterr().out.startswith("\n== summary ==\n")
