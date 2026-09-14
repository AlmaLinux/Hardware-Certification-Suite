"""CLI argument handling: server/config/run-dir overrides and precedence.

Environment-shaped flags are accepted both before and after the subcommand,
because both placements are natural to type and getting it wrong used to
produce an error that blamed the URL instead of explaining.
"""

import pytest

from alma_certify import cli
from alma_certify.config import Config

SERVER = "https://lumina.example.org"


def _parse(argv):
    return cli.build_parser().parse_args(argv)


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["--server", SERVER, "submit", "abc"], id="before-subcommand"),
        pytest.param(["submit", "abc", "--server", SERVER], id="after-subcommand"),
    ],
)
def test_server_accepted_in_either_position(argv, monkeypatch):
    monkeypatch.delenv(cli.SERVER_ENV, raising=False)
    config = cli._config(_parse(argv))
    assert config.get("general", "server") == SERVER


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["--config", "/tmp/x.conf", "list"], id="before-subcommand"),
        pytest.param(["list", "--config", "/tmp/x.conf"], id="after-subcommand"),
    ],
)
def test_config_accepted_in_either_position(argv):
    assert _parse(argv).config == "/tmp/x.conf"


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["--run-dir", "/srv/runs", "collect"], id="before-subcommand"),
        pytest.param(["collect", "--run-dir", "/srv/runs"], id="after-subcommand"),
    ],
)
def test_run_dir_accepted_in_either_position(argv):
    config = cli._config(_parse(argv))
    assert config.get("general", "run_dir") == "/srv/runs"


def test_subcommand_flag_does_not_clobber_global(monkeypatch):
    """The per-subcommand copies use SUPPRESS; an omitted flag there must not
    reset a value that was given globally."""
    monkeypatch.delenv(cli.SERVER_ENV, raising=False)
    config = cli._config(_parse(["--server", SERVER, "submit", "abc"]))
    assert config.get("general", "server") == SERVER


def test_env_var_supplies_server(monkeypatch):
    monkeypatch.setenv(cli.SERVER_ENV, SERVER)
    config = cli._config(_parse(["submit", "abc"]))
    assert config.get("general", "server") == SERVER


def test_flag_beats_env_var(monkeypatch):
    monkeypatch.setenv(cli.SERVER_ENV, "https://wrong.example")
    config = cli._config(_parse(["submit", "abc", "--server", SERVER]))
    assert config.get("general", "server") == SERVER


def test_resolve_server_strips_trailing_slash():
    config = Config.load("/nonexistent")
    config.set("general", "server", SERVER + "/")
    assert cli._resolve_server(config) == SERVER


def test_resolve_server_rejects_url_without_scheme(capsys):
    config = Config.load("/nonexistent")
    config.set("general", "server", "lumina.example.org")
    assert cli._resolve_server(config) is None
    assert "must start with http" in capsys.readouterr().err


def test_resolve_server_explains_how_to_configure(capsys):
    config = Config.load("/nonexistent")
    config.set("general", "server", "")
    assert cli._resolve_server(config) is None
    err = capsys.readouterr().err
    # every documented way of setting it should be named
    assert "--server" in err
    assert cli.SERVER_ENV in err
    assert "alma-certify.conf" in err


# --- what the suite benchmarks, and what it deliberately does not -------------


def test_no_storage_benchmarks_are_registered():
    """Certification compares machines, not drive models.

    A disk figure describes whichever drive happens to be installed, not the
    system under certification, so there is no storage benchmark category. If
    one comes back, this fails rather than quietly publishing IOPS to a
    leaderboard that cannot interpret them.
    """
    from alma_certify.registry import REGISTRY, load_all_tests

    load_all_tests()
    storage_benchmarks = [
        test.id for test in REGISTRY.all()
        if test.run_type == "benchmark" and "storage" in test.id
    ]
    assert storage_benchmarks == []
    assert "storage" not in " ".join(
        test.category for test in REGISTRY.all() if test.run_type == "benchmark"
    )


def test_storage_is_still_validated():
    """Removing the benchmarks does not stop the suite checking a disk works."""
    from alma_certify.registry import REGISTRY, load_all_tests

    load_all_tests()
    validated = {test.id for test in REGISTRY.all() if test.run_type == "validate"}
    assert "validate.storage.smart" in validated
    assert "validate.storage.io-sanity" in validated
    assert "validate.storage.nvme-errorlog" in validated
