"""``register`` and the QR it now shows for device authorization.

The plain URL and code stay the primary path (a QR is additive), so the tests here pin two things:
the complete URI the QR encodes is built correctly - preferring the server's, constructed from the
plain URI and code when an older server omits it - and the QR is printed only as an extra above the
waiting message, never in place of the URL and code.
"""

import os

import pytest

from alma_certify.submit import auth


@pytest.fixture
def no_wait(monkeypatch):
    """Don't actually sleep between polls."""
    monkeypatch.setattr(auth.time, "sleep", lambda *_: None)


def _responder(code_body, token_body=None):
    """A ``post_json`` stub: the device/code call first, then an approved token."""
    calls = {"n": 0}

    def post_json(url, payload, **kwargs):
        if url.endswith("/device/code"):
            return 200, code_body
        calls["n"] += 1
        return 200, token_body or {"token": "T", "expires_at": "2099-01-01T00:00:00Z"}

    return post_json


def test_the_qr_encodes_a_uri_built_from_the_code_when_the_server_omits_it(
    monkeypatch, no_wait, tmp_path, capsys
):
    seen = {}
    monkeypatch.setattr(auth.qr, "render_block", lambda data, **kw: seen.setdefault("data", data))
    monkeypatch.setattr(auth.http, "post_json", _responder({
        "device_code": "dc", "user_code": "ABCD-1234",
        "verification_uri": "https://lumina.almalinux.dev/activate/",
    }))

    assert auth.register("https://lumina.almalinux.dev", str(tmp_path / "tok.json")) == 0

    assert seen["data"] == "https://lumina.almalinux.dev/activate/?code=ABCD-1234"
    out = capsys.readouterr().out
    assert "https://lumina.almalinux.dev/activate/" in out and "ABCD-1234" in out


def test_the_qr_prefers_the_servers_complete_uri(monkeypatch, no_wait, tmp_path):
    seen = {}
    monkeypatch.setattr(auth.qr, "render_block", lambda data, **kw: seen.setdefault("data", data))
    monkeypatch.setattr(auth.http, "post_json", _responder({
        "device_code": "dc", "user_code": "ABCD-1234",
        "verification_uri": "https://lumina.almalinux.dev/activate/",
        "verification_uri_complete": "https://lumina.almalinux.dev/activate/?code=ABCD-1234&x=1",
    }))

    auth.register("https://lumina.almalinux.dev", str(tmp_path / "tok.json"))

    assert seen["data"] == "https://lumina.almalinux.dev/activate/?code=ABCD-1234&x=1"


def test_the_qr_is_printed_above_the_wait_when_one_is_drawn(
    monkeypatch, no_wait, tmp_path, capsys
):
    monkeypatch.setattr(auth.qr, "render_block", lambda data, **kw: "QR-BLOCK-HERE")
    monkeypatch.setattr(auth.http, "post_json", _responder({
        "device_code": "dc", "user_code": "ABCD-1234",
        "verification_uri": "https://lumina.almalinux.dev/activate/",
    }))

    auth.register("https://lumina.almalinux.dev", str(tmp_path / "tok.json"))

    out = capsys.readouterr().out
    assert "QR-BLOCK-HERE" in out
    assert "scan" in out.lower()
    # The URL and code still precede it: the QR is the extra, not the replacement.
    assert out.index("ABCD-1234") < out.index("QR-BLOCK-HERE")


def test_no_qr_suppresses_the_qr_without_touching_the_url_and_code(
    monkeypatch, no_wait, tmp_path, capsys
):
    """--no-qr (and ALMA_CERTIFY_NO_QR) skip the QR entirely - render_block is never even asked -
    while the URL and code, the reliable path, are printed exactly as always."""
    called = {"n": 0}
    monkeypatch.setattr(auth.qr, "render_block",
                        lambda data, **kw: called.__setitem__("n", called["n"] + 1) or "QR")
    monkeypatch.setattr(auth.http, "post_json", _responder({
        "device_code": "dc", "user_code": "ABCD-1234",
        "verification_uri": "https://lumina.almalinux.dev/activate/",
    }))

    auth.register("https://lumina.almalinux.dev", str(tmp_path / "tok.json"), no_qr=True)

    out = capsys.readouterr().out
    assert called["n"] == 0, "render_block must not be called when the QR is turned off"
    assert "QR" not in out
    assert "https://lumina.almalinux.dev/activate/" in out and "ABCD-1234" in out


def test_registration_completes_and_stores_the_token_without_a_qr(
    monkeypatch, no_wait, tmp_path
):
    monkeypatch.setattr(auth.qr, "render_block", lambda data, **kw: None)  # no terminal
    monkeypatch.setattr(auth.http, "post_json", _responder({
        "device_code": "dc", "user_code": "ABCD-1234",
        "verification_uri": "https://lumina.almalinux.dev/activate/",
    }))
    token_path = tmp_path / "tok.json"

    assert auth.register("https://lumina.almalinux.dev", str(token_path)) == 0
    assert os.path.exists(token_path)
    assert auth.load_token(str(token_path)) == "T"


# --- giving up on an approval that never comes -------------------------------------
#
# Reported by a tester: the interface showed the code, the token was collected, and then it froze -
# no button worked and escape did nothing. The half of that which lives here is the poll loop.
# It ran to the code's deadline (fifteen minutes by default) no matter what the front-end was
# being told, so there was nothing a front-end could do to end it: a run cancelled during
# authorization stayed in this loop, and a dialog with no control on it stayed on top of the run
# screen for the whole of it.


class _Driver:
    """The two things the poll loop now asks its driver about, and a record of what it was told."""

    def __init__(self, stop=False, skip=False):
        self._stop = stop
        self._skip = skip
        self.said = []
        self.authorized_called = 0

    def should_stop(self):
        return self._stop

    def skip_authorization(self):
        return self._skip

    def log(self, msg):
        self.said.append(msg)

    def authorize(self, **kwargs):
        pass

    def authorized(self):
        self.authorized_called += 1


def _pending(code_body):
    """A server that never approves: every token poll says authorization_pending."""
    def post_json(url, payload, **kwargs):
        if url.endswith("/device/code"):
            return 200, code_body
        return 400, {"error": "authorization_pending"}
    return post_json


CODE = {
    "device_code": "dc", "user_code": "ABCD-1234", "interval": 5, "expires_in": 900,
    "verification_uri": "https://lumina.almalinux.dev/activate/",
}


def test_a_cancelled_run_does_not_sit_out_the_device_code(monkeypatch, no_wait, tmp_path):
    """Cancel during authorization ends the poll instead of waiting fifteen minutes for a code
    nobody is going to approve."""
    driver = _Driver(stop=True)
    monkeypatch.setattr(auth.http, "post_json", _pending(CODE))

    assert auth.register("https://lumina.almalinux.dev", str(tmp_path / "tok.json"),
                         hooks=driver) == 2

    assert any("cancelled" in line for line in driver.said), driver.said
    assert driver.authorized_called == 1, "the prompt has to come down however polling ended"


def test_skipping_the_approval_is_not_cancelling_the_run(monkeypatch, no_wait, tmp_path):
    """The other way out of the dialog: give up on uploading, keep testing the hardware. It ends
    the same poll and says a different thing, because the run carries on after it."""
    driver = _Driver(skip=True)
    monkeypatch.setattr(auth.http, "post_json", _pending(CODE))

    assert auth.register("https://lumina.almalinux.dev", str(tmp_path / "tok.json"),
                         hooks=driver) == 2

    assert any("without uploading" in line for line in driver.said), driver.said


def _fake_clock(monkeypatch):
    """A clock that only moves when something sleeps, so the poll deadline is reached in no real
    time at all. Without it these tests would wait out the code's real lifetime."""
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds
        slept.append(seconds)

    slept = []
    monkeypatch.setattr(auth.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(auth.time, "sleep", sleep)
    return slept


def test_the_wait_between_polls_is_sliced_so_a_cancel_is_noticed(monkeypatch, tmp_path):
    """A single sleep of the server's interval would make giving up take up to that long to be
    seen, and ``slow_down`` grows the interval. The wait is sliced instead."""
    slept = _fake_clock(monkeypatch)
    monkeypatch.setattr(auth.http, "post_json", _pending(CODE))

    auth.register("https://lumina.almalinux.dev", str(tmp_path / "tok.json"),
                  hooks=_Driver())

    assert slept, "it still waits between polls"
    assert max(slept) <= 0.25, "no single sleep may outlast a cancel's patience"


def test_a_terminal_run_still_sleeps_the_whole_interval(monkeypatch, tmp_path):
    """No hooks means the CLI, which has nothing to cancel with: unchanged behavior."""
    slept = _fake_clock(monkeypatch)
    monkeypatch.setattr(auth.http, "post_json", _pending(CODE))
    monkeypatch.setattr(auth.qr, "render_block", lambda *a, **k: "")

    auth.register("https://lumina.almalinux.dev", str(tmp_path / "tok.json"))

    assert slept and set(slept) == {5}, slept
