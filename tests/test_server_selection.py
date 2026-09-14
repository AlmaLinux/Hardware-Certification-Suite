"""Which catalog a run submits to, and how to point it somewhere else.

Production is ``https://catalog.almalinux.org`` and it is a built-in default, not something the
operator has to know: a machine with no configuration file still uploads to the right place. The
staging catalog is one flag away, because "developing against the pre-release server" is the one
alternative anybody actually uses and a URL retyped from memory eventually gets a typo.
"""


import pytest

from alma_certify import cli
from alma_certify.config import DEFAULT_SERVER, DEV_SERVER


def _server(argv, env=None, monkeypatch=None):
    """The server a command resolves to, with no config file in the way."""
    if monkeypatch is not None:
        monkeypatch.delenv(cli.SERVER_ENV, raising=False)
        if env is not None:
            monkeypatch.setenv(cli.SERVER_ENV, env)
    args = cli.build_parser().parse_args(argv)
    args.config = "/nonexistent"
    return cli._config(args).get("general", "server")


def test_the_production_catalog_is_the_default(monkeypatch):
    """No flag, no environment, no config file: it still knows where results go."""
    assert DEFAULT_SERVER == "https://catalog.almalinux.org"
    assert _server(["validate"], monkeypatch=monkeypatch) == DEFAULT_SERVER


def test_the_shipped_config_agrees_with_the_built_in_default():
    """Two places name the production catalog, and a package whose config file disagreed with its
    own default would send results somewhere the code says it does not."""
    import pathlib

    conf = pathlib.Path(__file__).resolve().parent.parent / "alma-certify.conf"
    assert "server = %s" % DEFAULT_SERVER in conf.read_text(encoding="utf-8")


def test_dev_selects_the_staging_catalog(monkeypatch):
    assert DEV_SERVER == "https://lumina.almalinux.dev"
    assert _server(["validate", "--dev"], monkeypatch=monkeypatch) == DEV_SERVER


def test_dev_works_before_the_subcommand_too(monkeypatch):
    """Both placements, like every other environment-shaped option: either is a reasonable thing to
    type, and the guided interface carries it in front of the command it builds."""
    assert _server(["--dev", "validate"], monkeypatch=monkeypatch) == DEV_SERVER


@pytest.mark.parametrize("command", ["collect", "survey", "validate", "benchmark", "run",
                                     "register", "submit"])
def test_every_command_that_talks_to_a_server_takes_it(command):
    argv = [command, "--dev"] + (["9f3c1a2e"] if command == "submit" else [])
    assert cli.build_parser().parse_args(argv).dev is True


def test_an_explicit_server_outranks_dev(monkeypatch):
    """So naming a third catalog is never ambiguous. Giving both is not an error - the specific
    answer just wins over the shorthand."""
    got = _server(["validate", "--dev", "--server", "https://staging.example"],
                  monkeypatch=monkeypatch)
    assert got == "https://staging.example"


def test_dev_outranks_the_environment(monkeypatch):
    """The flag is typed at the command and the variable is left over from a shell. A login profile
    exporting the production URL must not quietly beat the flag that asked for staging."""
    got = _server(["validate", "--dev"], env="https://catalog.almalinux.org",
                  monkeypatch=monkeypatch)
    assert got == DEV_SERVER


def test_the_environment_still_beats_the_default(monkeypatch):
    got = _server(["validate"], env="https://elsewhere.example", monkeypatch=monkeypatch)
    assert got == "https://elsewhere.example"


def test_the_interface_carries_dev_but_does_not_offer_it(monkeypatch):
    """Same rule as --allow-self-signed: which catalog you submit to is not a run choice, so it is
    not a control in the guided interface. ``alma-certify --dev tui`` still works, and the resident
    run it hosts submits to the staging catalog."""
    import argparse

    started = {}

    class _Frontend:
        def unavailable_reason(self):
            return None

        def start(self, **kwargs):
            started.update(kwargs)
            return None

    monkeypatch.setattr(cli, "_interactive_terminal", lambda: True)
    monkeypatch.setattr(cli, "_select_tui", lambda: (_Frontend(), None))
    monkeypatch.setattr(cli.elevate, "is_root", lambda: True)

    cli.cmd_tui(argparse.Namespace(config=None, server=None, run_dir=None, dev=True))

    assert "--dev" in started["carried"]
