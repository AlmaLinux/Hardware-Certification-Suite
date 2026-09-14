"""Certificate verification, and the one flag that turns it off.

A dev or staging catalog serves a self-signed certificate, and the suite refusing to talk to one at
all means the people testing the suite against a dev server cannot use the suite. So there is a way
to switch verification off, and these tests pin the two things that matter about it: it is off only
when somebody asked, and it says so when it is.
"""

import ssl

import pytest

from alma_certify import cli
from alma_certify.config import Config
from alma_certify.submit import http

SERVER = "https://lumina.dev.example"


@pytest.fixture(autouse=True)
def restore_verification():
    """Verification is process-wide state, so a test that turns it off must not hand that to the
    next test in the file - or, worse, to a test in another file that submits something."""
    before = http.verifying()
    yield
    http.allow_self_signed(not before)


def _config(argv, path="/nonexistent"):
    args = cli.build_parser().parse_args(argv)
    args.config = path
    return cli._config(args)


# --- what the ssl context actually does ---------------------------------------


def test_verifying_by_default():
    ctx = http._context()
    assert http.verifying() is True
    assert ctx.check_hostname is True
    assert ctx.verify_mode is ssl.CERT_REQUIRED


def test_allowing_self_signed_stops_verifying():
    http.allow_self_signed(True)
    ctx = http._context()
    assert http.verifying() is False
    assert ctx.check_hostname is False
    assert ctx.verify_mode is ssl.CERT_NONE


def test_it_goes_back_on():
    """Not a symmetry exercise: ``_config`` calls the setter on every invocation, so a long-lived
    process that ran one command with the flag and the next without it must verify again."""
    http.allow_self_signed(True)
    http.allow_self_signed(False)
    assert http.verifying() is True
    assert http._context().verify_mode is ssl.CERT_REQUIRED


@pytest.mark.parametrize("send", [
    pytest.param(lambda p: http.get_json("https://c/api/v1/token", token="abc"), id="get_json"),
    pytest.param(lambda p: http.post_json("https://c/api/v1/runs", {"a": 1}), id="post_json"),
    # The bundle upload, which is the request this whole feature exists for: a submission to a
    # catalog serving its own certificate.
    pytest.param(
        lambda p: http.post_multipart("https://c/api/v1/runs", {"run": "1"}, "bundle", str(p)),
        id="post_multipart",
    ),
])
def test_the_context_reaches_the_request(send, monkeypatch, tmp_path):
    """The link that makes any of the above matter. Building a context nobody passes to ``urlopen``
    leaves every request verifying while the flag, the config key, and the warning all say it is
    not - and there is no failure to notice, only a connection that still refuses the dev server.
    """
    seen = {}

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None, context=None):
        seen["context"] = context
        return FakeResponse()

    monkeypatch.setattr(http.urllib.request, "urlopen", fake_urlopen)
    bundle = tmp_path / "bundle.tar.zst"
    bundle.write_bytes(b"not really a bundle")

    send(bundle)
    assert seen["context"].verify_mode is ssl.CERT_REQUIRED

    http.allow_self_signed(True)
    send(bundle)
    assert seen["context"].verify_mode is ssl.CERT_NONE
    assert seen["context"].check_hostname is False


# --- getting there from the command line --------------------------------------


@pytest.mark.parametrize("argv", [
    pytest.param(["--allow-self-signed", "--server", SERVER, "submit", "abc"],
                 id="before-subcommand"),
    pytest.param(["submit", "abc", "--server", SERVER, "--allow-self-signed"],
                 id="after-subcommand"),
])
def test_flag_accepted_in_either_position(argv, monkeypatch):
    monkeypatch.delenv(cli.SERVER_ENV, raising=False)
    config = _config(argv)
    assert config.getbool("general", "allow_self_signed") is True
    assert http.verifying() is False


def test_off_without_the_flag(monkeypatch):
    monkeypatch.delenv(cli.SERVER_ENV, raising=False)
    config = _config(["submit", "abc", "--server", SERVER])
    assert config.getbool("general", "allow_self_signed") is False
    assert http.verifying() is True


def test_every_subcommand_that_takes_a_server_takes_this_too():
    """The two belong together: somebody pointing the suite at a dev catalog names the server on the
    same command line, and finding the flag rejected there would read as the flag not existing.

    Derived from the parser rather than listed here, so a subcommand that gains ``--server`` later
    fails this until it gains the other one too.
    """
    parser = cli.build_parser()
    subparsers = parser._subparsers._group_actions[0].choices
    both = {"--server", "--allow-self-signed"}
    for name, p in list(subparsers.items()) + [("alma-certify itself", parser)]:
        options = {o for action in p._actions for o in action.option_strings}
        assert options & both in (set(), both), name


@pytest.mark.parametrize("argv", [
    pytest.param(["--server", SERVER, "--allow-self-signed", "run"], id="globals-in-front"),
    pytest.param(["run", "--server", SERVER, "--allow-self-signed"], id="on-the-subcommand"),
])
def test_a_whole_run_can_be_pointed_at_a_dev_catalog(argv, monkeypatch):
    """The likeliest command by far: somebody certifying a machine against the dev catalog. Both
    placements, because ``run`` used to take neither and only the first spelling worked."""
    monkeypatch.delenv(cli.SERVER_ENV, raising=False)
    config = _config(argv)
    assert config.get("general", "server") == SERVER
    assert http.verifying() is False


def test_config_file_option_is_honoured(tmp_path, monkeypatch):
    """The flag is for one command; the config file is for a machine that always talks to a dev
    catalog, which is the case this exists to serve."""
    monkeypatch.delenv(cli.SERVER_ENV, raising=False)
    path = tmp_path / "alma-certify.conf"
    path.write_text("[general]\nserver = %s\nallow_self_signed = true\n" % SERVER)
    config = _config(["submit", "abc"], path=str(path))
    assert config.getbool("general", "allow_self_signed") is True
    assert http.verifying() is False


def test_a_config_file_that_says_no_leaves_it_on(tmp_path, monkeypatch):
    monkeypatch.delenv(cli.SERVER_ENV, raising=False)
    path = tmp_path / "alma-certify.conf"
    path.write_text("[general]\nallow_self_signed = false\n")
    _config(["submit", "abc"], path=str(path))
    assert http.verifying() is True


# --- saying so ----------------------------------------------------------------


def test_it_warns_every_run():
    """Every run, not once in the documentation. This gets turned on for an afternoon against a dev
    server and then lives in a config file for a year; a line in the output is what notices."""
    config = Config.load("/nonexistent")
    config.set("general", "server", SERVER)
    config.set("general", "allow_self_signed", "true")
    err = cli._apply_tls_policy(config)
    assert "verification is off" in err
    # Names the flag and the config key, so the warning says how to undo itself, and the server, so
    # it is obvious when the setting outlived the server it was for.
    assert "--allow-self-signed" in err
    assert "allow_self_signed" in err
    assert SERVER in err


def test_the_command_line_shows_the_warning_on_stderr(capsys):
    _config(["submit", "abc", "--server", SERVER, "--allow-self-signed"])
    assert "verification is off" in capsys.readouterr().err


def test_the_tls_warning_reaches_a_resident_run(tmp_path, monkeypatch):
    """_config printed it to stderr, which a resident run never shows."""
    monkeypatch.setattr(cli.hostos, "detect",
                        lambda *a, **k: cli.hostos.HostOS("almalinux", "10.1", "AlmaLinux 10.1"))
    notes = []

    class H(cli.RunHooks):
        def note(self, text=""):
            notes.append(text)

    cli.prepare_run(["collect", "--allow-self-signed", "--run-dir", str(tmp_path)], H())
    assert any("verification is off" in n for n in notes), notes


def test_it_says_nothing_when_verifying():
    config = Config.load("/nonexistent")
    config.set("general", "server", SERVER)
    assert cli._apply_tls_policy(config) is None
