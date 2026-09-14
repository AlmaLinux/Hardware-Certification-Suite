"""Token storage, expiry awareness, and the pre-run authorization check.

The point of these: a multi-hour benchmark pass must not discover at upload
time that its token expired or belongs to another machine.
"""

import datetime
import json
import os

import pytest

from alma_certify.submit import auth
from alma_certify.submit import http as submit_http


def _write(path, **fields):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(fields, fh)


def _in(seconds):
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=seconds
    )
    return when.isoformat().replace("+00:00", "Z")


def test_missing_token_file(tmp_path):
    token, remaining = auth.token_status(str(tmp_path / "absent.json"))
    assert token is None and remaining is None


def test_unreadable_token_file_is_treated_as_absent(tmp_path):
    path = tmp_path / "token.json"
    path.write_text("not json at all")
    assert auth.token_status(str(path)) == (None, None)


def test_remaining_seconds_are_reported(tmp_path):
    path = tmp_path / "token.json"
    _write(path, token="abc", expires_at=_in(3600), hostname=auth.hostname())
    token, remaining = auth.token_status(str(path))
    assert token == "abc"
    assert 3500 < remaining <= 3600


def test_expired_token_reports_negative_remaining(tmp_path):
    path = tmp_path / "token.json"
    _write(path, token="abc", expires_at=_in(-60), hostname=auth.hostname())
    _, remaining = auth.token_status(str(path))
    assert remaining < 0


def test_token_stored_for_another_host_is_ignored(tmp_path):
    """The server binds tokens to a host, so one copied from another machine
    would be refused at upload time. Treat it as absent instead."""
    path = tmp_path / "token.json"
    _write(path, token="abc", expires_at=_in(3600), hostname="some-other-box")
    assert auth.token_status(str(path)) == (None, None)


def test_token_without_expiry_is_usable(tmp_path):
    path = tmp_path / "token.json"
    _write(path, token="abc", hostname=auth.hostname())
    token, remaining = auth.token_status(str(path))
    assert token == "abc" and remaining is None


def test_save_token_is_owner_only(tmp_path):
    path = tmp_path / "sub" / "token.json"
    auth.save_token(str(path), {"token": "secret", "expires_at": _in(600)})
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    # the host it was issued for is recorded for later comparison
    with open(path, encoding="utf-8") as fh:
        assert json.load(fh)["hostname"] == auth.hostname()


def test_a_directory_it_creates_is_owner_only_too(tmp_path):
    """A 0600 file under a 0755 directory still lets anybody list the token's name, its size, and
    when it was last written - which says whether this machine is registered and roughly when."""
    path = tmp_path / "fresh" / "token.json"

    auth.save_token(str(path), {"token": "secret", "expires_at": _in(600)})

    assert oct(os.stat(path.parent).st_mode & 0o777) == "0o700"


def test_rewriting_over_a_loose_token_tightens_it(tmp_path):
    """The mode passed to ``os.open`` applies only when the file is created. Registering again over
    a token left world-readable - by hand, or by a version that wrote it less carefully - kept the
    looser mode, so the fix silently did not apply to the machines that needed it."""
    path = tmp_path / "token.json"
    path.write_text("{}", encoding="utf-8")
    os.chmod(path, 0o644)

    auth.save_token(str(path), {"token": "secret", "expires_at": _in(600)})

    assert oct(os.stat(path).st_mode & 0o777) == "0o600"


def test_a_directory_it_did_not_create_is_left_alone(tmp_path):
    """``token_path`` is configurable, so the directory may be one the operator picked for their own
    reasons. The file is ours to lock down; the directory around it is not ours to re-permission."""
    directory = tmp_path / "chosen"
    directory.mkdir(mode=0o755)
    os.chmod(directory, 0o755)

    auth.save_token(str(directory / "token.json"), {"token": "s", "expires_at": _in(600)})

    assert oct(os.stat(directory).st_mode & 0o777) == "0o755"
    assert oct(os.stat(directory / "token.json").st_mode & 0o777) == "0o600"


def test_the_token_is_not_configuration(tmp_path):
    """It lives under /var/cache, not /etc: a credential the machine fetches for itself and can
    fetch again with `alma-certify register`. Keeping it out of /etc also keeps it out of
    configuration backups and etckeeper history."""
    from alma_certify.config import DEFAULT_TOKEN_PATH

    assert DEFAULT_TOKEN_PATH == "/var/cache/alma-certify/token.json"
    assert not DEFAULT_TOKEN_PATH.startswith("/etc")


def test_the_shipped_config_points_at_the_same_place():
    """Two places name it, and a package whose config file disagreed with its own default would
    write the token somewhere the code says it does not."""
    import pathlib

    from alma_certify.config import DEFAULT_TOKEN_PATH

    conf = pathlib.Path(__file__).resolve().parent.parent / "alma-certify.conf"
    assert "token_path = %s" % DEFAULT_TOKEN_PATH in conf.read_text(encoding="utf-8")


def test_the_package_ships_that_directory_locked_down():
    """rpm has to create it at 0700 and own it. Left to the first `register` to make, it would
    exist only after a successful registration, and would not be removed on uninstall.

    Skipped when the spec is not beside the tests: the source tarball deliberately leaves
    ``packaging/`` out, and this suite runs from that tarball in the package's own ``%check``. A
    unit test that failed the build because a packaging file was missing would be reporting on the
    tarball's contents rather than on the code.
    """
    import pathlib
    import re

    spec_path = (pathlib.Path(__file__).resolve().parent.parent
                 / "packaging" / "alma-certify.spec")
    if not spec_path.is_file():
        pytest.skip("no spec here; this runs from a source tarball that omits packaging/")
    spec = spec_path.read_text(encoding="utf-8")

    assert re.search(r"install -d %\{buildroot\}%\{_localstatedir\}/cache/%\{name\}", spec)
    assert re.search(
        r"%dir %attr\(0700,root,root\) %\{_localstatedir\}/cache/%\{name\}", spec
    )


def test_the_package_owns_the_token_without_shipping_one():
    """%ghost, so rpm records the path, its mode, and its owner without putting a file in the
    payload. Without it the token is a stray nobody owns: `rpm -qf` on it answers nothing, and
    erasing the package leaves a live credential behind on the machine.

    0600 on the ghost entry as well as in the code that writes it, because they answer different
    questions - one is what rpm will report and enforce, the other is what actually lands on disk
    the first time somebody registers.
    """
    import pathlib
    import re

    spec_path = (pathlib.Path(__file__).resolve().parent.parent
                 / "packaging" / "alma-certify.spec")
    if not spec_path.is_file():
        pytest.skip("no spec here; this runs from a source tarball that omits packaging/")
    spec = spec_path.read_text(encoding="utf-8")

    assert re.search(
        r"%ghost %attr\(0600,root,root\) %\{_localstatedir\}/cache/%\{name\}/token\.json",
        spec,
    ), "the token is not a %ghost, so nothing owns it or removes it"


# --- ensure_token -------------------------------------------------------------


def test_healthy_token_is_reused_without_registering(tmp_path, live_token):
    """``live_token`` stubs the server check. Without it this passed for the wrong
    reason: "https://x" does not resolve, so the check reported *unreachable* and the
    token was reused as a fallback rather than because anything confirmed it."""
    path = tmp_path / "token.json"
    _write(path, token="good", expires_at=_in(6 * 3600), hostname=auth.hostname())
    calls = []
    original = auth.register
    auth.register = lambda *a, **kw: calls.append(a) or 0
    try:
        result = auth.ensure_token("https://x", str(path), interactive=True,
                                   log=lambda m: None)
    finally:
        auth.register = original
    assert result == "good"
    assert calls == []          # no prompt for a healthy token


def test_nearly_expired_token_triggers_registration(tmp_path):
    """Under the two-hour floor the token might not outlast the run."""
    path = tmp_path / "token.json"
    _write(path, token="stale", expires_at=_in(600), hostname=auth.hostname())
    messages = []

    def fake_register(server, token_path, quiet=False, hooks=None):
        auth.save_token(token_path, {"token": "fresh", "expires_at": _in(43200)})
        return 0

    original = auth.register
    auth.register = fake_register
    try:
        result = auth.ensure_token("https://x", str(path), interactive=True,
                                   log=messages.append)
    finally:
        auth.register = original
    assert result == "fresh"
    assert any("expires in" in m for m in messages)


def test_non_interactive_never_blocks_on_a_prompt(tmp_path):
    """In CI there is no operator to approve anything, so say so and let the
    run proceed rather than hanging."""
    path = tmp_path / "token.json"
    messages = []
    called = []
    original = auth.register
    auth.register = lambda *a, **kw: called.append(1) or 0
    try:
        result = auth.ensure_token("https://x", str(path), interactive=False,
                                   log=messages.append)
    finally:
        auth.register = original
    assert result is None
    assert called == []
    assert any("--no-submit" in m for m in messages)


def test_failed_registration_lets_the_run_continue(tmp_path):
    path = tmp_path / "token.json"
    messages = []
    original = auth.register
    auth.register = lambda *a, **kw: 2      # operator denied or timed out
    try:
        result = auth.ensure_token("https://x", str(path), interactive=True,
                                   log=messages.append)
    finally:
        auth.register = original
    assert result is None
    assert any("continue without upload" in m for m in messages)


# --- asking the server, not the clock -----------------------------------------
#
# Reported from real use: the local CLI held a token from before the server's database was
# rebuilt. Its stored ``expires_at`` was hours away, so the pre-run check passed, the
# validation run completed, and the upload failed at the very end with the whole run spent.
#
# ``expires_at`` is a claim about *time*. It says nothing about whether the token still
# exists, and the two come apart whenever one dies early: revoked by its owner, deleted by
# an admin, or its records wiped. So the pre-run check now asks the server.
#
# The rule that keeps this from being a regression in itself: a *rejection* is an answer and
# acts on it; anything else - no route to the host, a proxy 502, a server too old to have
# the endpoint - is not an answer, and must never stop somebody testing hardware.



@pytest.fixture
def stub_get(monkeypatch):
    """Replace the HTTP GET with a canned (status, body) and record the calls."""
    def install(status, body):
        calls = []

        def fake_get(url, token=None, timeout=10):
            calls.append({"url": url, "token": token, "timeout": timeout})
            if isinstance(body, Exception):
                raise body
            return status, body

        monkeypatch.setattr(auth.http, "get_json", fake_get)
        return calls
    return install


@pytest.fixture
def live_token(stub_get):
    """The common case: the server confirms the token and it can submit."""
    return stub_get(200, {"valid": True, "scopes": ["submit"], "username": "someone"})


# --- check_token in isolation --------------------------------------------------


def test_a_200_means_live(stub_get):
    stub_get(200, {"valid": True, "scopes": ["submit"]})

    outcome, info = auth.check_token("https://c", "tok")

    assert outcome == auth.TOKEN_LIVE
    assert info["scopes"] == ["submit"]


def test_a_401_means_rejected(stub_get):
    stub_get(401, {"detail": "Invalid or expired token."})

    outcome, _ = auth.check_token("https://c", "tok")

    assert outcome == auth.TOKEN_REJECTED


def test_a_server_error_is_not_a_rejection(stub_get):
    """A 502 from a proxy says nothing about the token. Treating it as a rejection would
    throw away a working credential and prompt for a new one on every hiccup."""
    stub_get(502, {"raw": "<html>bad gateway</html>"})

    outcome, _ = auth.check_token("https://c", "tok")

    assert outcome == auth.TOKEN_UNREACHABLE


def test_a_404_is_not_a_rejection(stub_get):
    """A server older than this endpoint answers 404. The token may be perfectly good."""
    stub_get(404, {})

    assert auth.check_token("https://c", "tok")[0] == auth.TOKEN_UNREACHABLE


def test_a_transport_failure_is_not_a_rejection(stub_get):
    stub_get(0, OSError("connection refused"))

    assert auth.check_token("https://c", "tok")[0] == auth.TOKEN_UNREACHABLE


def test_a_timeout_is_not_a_rejection(stub_get):
    import socket

    stub_get(0, socket.timeout("timed out"))

    assert auth.check_token("https://c", "tok")[0] == auth.TOKEN_UNREACHABLE


def test_the_check_asks_the_right_endpoint_with_the_token(stub_get):
    calls = stub_get(200, {"valid": True, "scopes": ["submit"]})

    auth.check_token("https://catalog.example", "the-token")

    assert calls[0]["url"] == "https://catalog.example/api/v1/token"
    assert calls[0]["token"] == "the-token"


def test_the_check_does_not_hang_a_run(stub_get):
    """Ten seconds, not the thirty a submission gets. Nobody should wait half a minute to
    be told their hardware can be tested."""
    calls = stub_get(200, {"valid": True, "scopes": ["submit"]})

    auth.check_token("https://c", "tok")

    assert calls[0]["timeout"] <= 10


# --- through the pre-run check -------------------------------------------------


def test_a_server_rejected_token_is_treated_as_absent(tmp_path, stub_get):
    """The reported bug. Unexpired by the clock, gone from the server, so the pre-run
    check has to notice and offer to fix it *before* the run rather than after."""
    stub_get(401, {"detail": "Invalid or expired token."})
    path = tmp_path / "token.json"
    _write(path, token="ghost", expires_at=_in(6 * 3600), hostname=auth.hostname())
    messages = []

    def fake_register(server, token_path, quiet=False, hooks=None):
        auth.save_token(token_path, {"token": "fresh", "expires_at": _in(43200)})
        return 0

    original = auth.register
    auth.register = fake_register
    try:
        result = auth.ensure_token("https://x", str(path), interactive=True,
                                   log=messages.append)
    finally:
        auth.register = original

    assert result == "fresh"
    assert any("no longer valid on the server" in m for m in messages)


def test_a_rejected_token_says_why_when_nobody_can_be_asked(tmp_path, stub_get):
    """Non-interactive, so there is no prompt to offer - the message is the whole value.
    "your token expired" would be a lie, and would send somebody looking at clocks."""
    stub_get(401, {})
    path = tmp_path / "token.json"
    _write(path, token="ghost", expires_at=_in(6 * 3600), hostname=auth.hostname())
    messages = []

    result = auth.ensure_token("https://x", str(path), interactive=False,
                               log=messages.append)

    assert result is None
    joined = " ".join(messages)
    assert "no longer valid on the server" in joined
    assert "revoked" in joined or "rebuilt" in joined


def test_an_unreachable_server_does_not_block_the_run(tmp_path, stub_get):
    """The rule that keeps this fix from becoming a worse bug. A laptop off the network,
    or a catalog that is down, must not stop anybody testing hardware - the run still
    produces a bundle that can be uploaded later."""
    stub_get(0, OSError("no route to host"))
    path = tmp_path / "token.json"
    _write(path, token="good", expires_at=_in(6 * 3600), hostname=auth.hostname())
    messages = []
    calls = []
    original = auth.register
    auth.register = lambda *a, **kw: calls.append(a) or 0
    try:
        result = auth.ensure_token("https://x", str(path), interactive=True,
                                   log=messages.append)
    finally:
        auth.register = original

    assert result == "good", "a working token was thrown away over a network blip"
    assert calls == [], "it should not prompt for a new token it cannot verify"
    assert any("could not reach" in m for m in messages)


def test_a_token_without_the_submit_scope_is_flagged_before_the_run(tmp_path, stub_get):
    """Real, unexpired, and useless for this: ingest refuses it. Better said before the
    run than discovered after it."""
    stub_get(200, {"valid": True, "scopes": ["read"]})
    path = tmp_path / "token.json"
    _write(path, token="readonly", expires_at=_in(6 * 3600), hostname=auth.hostname())
    messages = []
    original = auth.register
    auth.register = lambda *a, **kw: 1          # registration declined
    try:
        auth.ensure_token("https://x", str(path), interactive=False,
                          log=messages.append)
    finally:
        auth.register = original

    assert any("scope" in m for m in messages)


def test_an_expired_token_never_asks_the_server(tmp_path, stub_get):
    """No point. The clock already answered, and a machine registering a fresh token is
    often the one that cannot reach the old one's server anyway."""
    calls = stub_get(200, {"valid": True, "scopes": ["submit"]})
    path = tmp_path / "token.json"
    _write(path, token="stale", expires_at=_in(60), hostname=auth.hostname())
    original = auth.register
    auth.register = lambda *a, **kw: 1
    try:
        auth.ensure_token("https://x", str(path), interactive=False,
                          log=lambda m: None)
    finally:
        auth.register = original

    assert calls == []


def test_no_stored_token_never_asks_the_server(tmp_path, stub_get):
    calls = stub_get(200, {"valid": True, "scopes": ["submit"]})

    auth.ensure_token("https://x", str(tmp_path / "absent.json"),
                      interactive=False, log=lambda m: None)

    assert calls == []


def test_the_get_helper_sends_a_bearer_header():
    """``get_json`` is new, and the header is the entire point of it."""
    import urllib.request

    seen = {}

    class FakeResponse:
        status = 200

        def read(self):
            return b'{"valid": true}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    original = urllib.request.urlopen

    def fake_urlopen(req, timeout=None, context=None):
        seen["headers"] = dict(req.header_items())
        seen["method"] = req.get_method()
        return FakeResponse()

    urllib.request.urlopen = fake_urlopen
    try:
        status, body = submit_http.get_json("https://c/api/v1/token", token="abc")
    finally:
        urllib.request.urlopen = original

    assert status == 200 and body == {"valid": True}
    assert seen["method"] == "GET"
    assert seen["headers"].get("Authorization") == "Bearer abc"
