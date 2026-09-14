"""Being root, or becoming root through sudo.

Every command reads or writes something only root can: the run directory is 0750 root:root because
it holds serial numbers and DMI UUIDs, the token lives in /etc, and the tests read DMI, SMART, and
PCI configuration space. So there is one gate, at the one entry point, rather than a check at the
top of each command - which is what there was, and it covered the commands that change the machine
while leaving the read-only ones to fail later as a permission error or, worse, as an empty list of
runs on a machine full of them.

"Root" includes root reachable through sudo. Somebody who typed the command without it has not made
a mistake worth retyping. But the elevation is offered, never taken: a tool that re-runs itself with
more privilege than it was given is doing something nobody typed.

These carry the ``gate`` marker, so they see the real uid rather than the assume_root fixture every
other test in the suite gets.
"""

import pytest

from alma_certify import cli, elevate

pytestmark = pytest.mark.gate


def _not_root(monkeypatch, *, sudo=True, terminal=True, answer=True):
    monkeypatch.setattr(elevate, "is_root", lambda: False)
    monkeypatch.setattr(elevate, "sudo_available", lambda: sudo)
    monkeypatch.delenv(elevate.ELEVATED_ENV, raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: terminal)
    monkeypatch.setattr("sys.stdout.isatty", lambda: terminal)
    monkeypatch.setattr("builtins.input", lambda prompt="": "" if answer else "n")
    execs = []
    monkeypatch.setattr(elevate, "elevate", lambda args: execs.append(list(args)))
    return execs


# --- what the gate covers --------------------------------------------------------


@pytest.mark.parametrize("command", [
    "collect", "survey", "validate", "benchmark", "run", "resume", "setup-gpu", "tui",
    "runs", "list", "report", "bundle", "register", "submit",
])
def test_every_subcommand_is_behind_it(command, monkeypatch, capsys):
    """No exceptions. A command that slipped past would fail later and less clearly, and `list` is
    in here too so the rule is one sentence rather than a list to keep in sync."""
    execs = _not_root(monkeypatch, answer=False)
    argv = [command]
    if command in ("resume", "report", "bundle", "submit"):
        argv.append("9f3c1a2e-1111-4111-8111-111111111111")

    assert cli.main(argv) == cli.EXIT_USAGE, "%s ran without root" % command
    assert "must run as root" in capsys.readouterr().err
    assert execs == [], "declining must not elevate"


def test_help_and_version_need_nothing(monkeypatch, capsys):
    """They answer from the parser and touch no machine state. argparse exits during parsing, which
    is before the gate; this pins that it stays that way."""
    _not_root(monkeypatch)

    for flag in ("--help", "--version"):
        with pytest.raises(SystemExit) as exit_info:
            cli.main([flag])
        assert exit_info.value.code == 0, flag


def test_a_bare_command_with_no_terminal_still_prints_help(monkeypatch, capsys):
    """The compatibility guarantee: a script calling the bare command gets the help it always got,
    and help needs no privilege. Nothing is gated because nothing is going to run."""
    _not_root(monkeypatch, terminal=False)
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: False)

    assert cli.main([]) == cli.EXIT_USAGE
    assert "usage:" in capsys.readouterr().out


def test_a_bare_command_at_a_terminal_is_gated(monkeypatch):
    """Because it opens the interface, which hosts runs."""
    execs = _not_root(monkeypatch, answer=False)
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: True)
    opened = []
    monkeypatch.setattr(cli, "cmd_tui", lambda args: opened.append(args) or cli.EXIT_OK)

    assert cli.main([]) == cli.EXIT_USAGE
    assert not opened
    assert execs == []


# --- becoming root ----------------------------------------------------------------


def test_it_asks_before_elevating_and_yes_is_the_default(monkeypatch, capsys):
    """Yes is the default because the person typed a command that needs root; they are not being
    talked into anything, they are being told what it takes."""
    execs = _not_root(monkeypatch, answer=True)

    cli.main(["runs"])

    assert execs == [["runs"]], "the same command, re-run as root"
    assert "must run as root" in capsys.readouterr().err


def test_declining_stops_and_says_what_to_type(monkeypatch, capsys):
    execs = _not_root(monkeypatch, answer=False)

    assert cli.main(["validate", "--scope", "cpu"]) == cli.EXIT_USAGE
    assert execs == []
    assert "sudo alma-certify validate --scope cpu" in capsys.readouterr().err


def test_nothing_is_escalated_where_nobody_can_be_asked(monkeypatch, capsys):
    """A kickstart %post, a CI runner, a cron job. Prompting into a closed stdin would hang it
    forever, and elevating unasked because nobody objected is worse than refusing."""
    execs = _not_root(monkeypatch, terminal=False)

    assert cli.main(["collect"]) == cli.EXIT_USAGE
    assert execs == []
    err = capsys.readouterr().err
    assert "no terminal" in err
    assert "sudo alma-certify collect" in err


def test_no_sudo_on_the_machine_is_said_plainly(monkeypatch, capsys):
    execs = _not_root(monkeypatch, sudo=False)

    assert cli.main(["runs"]) == cli.EXIT_USAGE
    assert execs == []
    assert "sudo is not installed" in capsys.readouterr().err


def test_it_does_not_ask_twice(monkeypatch, capsys):
    """If an elevated re-run arrives here still not root, something is wrong that asking again will
    not fix. Sudo's own env_reset usually drops the marker, so the uid check is the real guard and
    this only matters where a site has turned that off - but a loop of password prompts is a bad
    enough failure to be worth the belt."""
    execs = _not_root(monkeypatch)
    monkeypatch.setenv(elevate.ELEVATED_ENV, "1")

    assert cli.main(["runs"]) == cli.EXIT_USAGE
    assert execs == []
    assert "did not produce root" in capsys.readouterr().err


def test_being_root_asks_nothing(monkeypatch):
    monkeypatch.setattr(elevate, "is_root", lambda: True)
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": pytest.fail("root should not be asked anything"),
    )
    monkeypatch.setattr(cli, "cmd_runs", lambda args: cli.EXIT_OK)

    assert cli.main(["runs"]) == cli.EXIT_OK


# --- the command it re-runs -------------------------------------------------------


def test_the_relaunch_does_not_trust_argv_zero(monkeypatch):
    """The installed launcher runs ``python -I -c '...' "$@"``, so ``sys.argv[0]`` is the string
    "-c" and re-running "the command" would re-run nothing. It is rebuilt from the interpreter and
    the package's own location instead.

    sudo is stubbed present rather than assumed: a minimal AlmaLinux container has no sudo, and
    this is about the shape of the command, not about the machine running the tests.
    """
    monkeypatch.setattr(elevate.shutil, "which", lambda name: "/usr/bin/sudo")
    argv = elevate.relaunch_argv(["validate", "--scope", "cpu"])

    assert argv[:2] == ["sudo", "--"], "-- so a flag of ours is never read as one of sudo's"
    assert "-I" in argv, "a tool about to run as root must not take sys.path from the environment"
    assert argv[-3:] == ["validate", "--scope", "cpu"]
    assert "-c" in argv and "from alma_certify.cli import main" in argv[argv.index("-c") + 1]


def test_there_is_no_relaunch_without_sudo(monkeypatch):
    monkeypatch.setattr(elevate.shutil, "which", lambda name: None)

    assert elevate.relaunch_argv(["runs"]) is None


def test_the_relaunch_replaces_this_process(monkeypatch):
    """execvp, not a subprocess: there is nothing for this process to do afterwards, and handing the
    terminal straight over means sudo's password prompt, the run's output, and Ctrl-C all behave as
    though the person had typed the sudo themselves."""
    called = {}
    monkeypatch.delenv(elevate.ELEVATED_ENV, raising=False)
    monkeypatch.setattr(elevate.shutil, "which", lambda name: "/usr/bin/sudo")

    def fake_execvp(file, argv):
        called["file"] = file
        called["argv"] = argv

    elevate.elevate(["runs"], execvp=fake_execvp)

    assert called["file"] == "sudo"
    assert called["argv"][0] == "sudo"
    import os
    assert os.environ[elevate.ELEVATED_ENV] == "1", "marked, so a re-entry cannot loop"
