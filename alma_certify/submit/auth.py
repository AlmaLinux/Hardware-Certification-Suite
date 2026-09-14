"""Device-code registration and local token storage.

Flow (mirrors Lumina's /api/v1/device/ endpoints, see docs/schema.md):

1. POST /api/v1/device/code  -> {device_code, user_code, verification_uri, ...}
2. Print the URL + code; the operator approves it in any browser.
3. Poll POST /api/v1/device/token until approved; store the submit-scoped
   token at token_path, mode 0600 in a 0700 directory.

Tokens are bound to this machine's hostname server-side, so one authorized
here cannot be used to post results from elsewhere.
"""

from __future__ import annotations

import datetime
import json
import os
import socket
import sys
import time
import urllib.parse
from typing import Optional, Tuple

from .. import __version__
from . import http, qr

# Refuse to start a long run on a token that could expire before it ends.
DEFAULT_MIN_REMAINING = 2 * 60 * 60

# The scope an upload needs. A token without it authenticates fine and is refused at
# ingest, which is a pointless way to end a finished run.
SUBMIT_SCOPE = "submit"


def hostname() -> str:
    return socket.gethostname()


def print_authorization(
    verification_uri: str, user_code: str, complete_uri: str, no_qr: bool = False
) -> None:
    """Print the device-flow prompt at a terminal: where to go, the code, and a QR of the complete
    URI. A bare ``alma-certify register`` and a plain CLI run share this; a resident run shows
    the same three things as widgets instead (see ``RunHooks.authorize``).
    """
    # flush=True matters: the operator has to read the code before the caller blocks on polling, and
    # stdout is block-buffered when piped (tee, logs, wrapper scripts).
    print("\nTo authorize this machine (%s), open:\n" % hostname(), flush=True)
    print("    %s" % verification_uri, flush=True)
    print("\nand enter the code:  %s\n" % user_code, flush=True)
    # A QR of the complete URI, where the terminal can show one that will scan. The URL and code
    # above stay put as the fallback, so nothing is lost when it is skipped (--no-qr, or a terminal
    # that cannot show one).
    code_image = None if no_qr else qr.render_block(complete_uri)
    if code_image:
        print("or scan this to open the approval page (already logged in? just approve):\n",
              flush=True)
        print(code_image, flush=True)
        print("", flush=True)


def _abandoned(hooks) -> Optional[str]:
    """Why polling should stop early, or None to keep waiting."""
    if hooks is None:
        return None
    if hooks.should_stop():
        return "the run was cancelled"
    if hooks.skip_authorization():
        return "you chose to go on without uploading"
    return None


def _wait(seconds: float, hooks) -> Optional[str]:
    """Sleep ``seconds`` between polls, in slices, returning why to give up or None.

    Sliced rather than one ``time.sleep``: the poll interval is whatever the server asked for and
    ``slow_down`` grows it, so a single sleep would make giving up take up to that long to be
    noticed. A driver with nothing to report (the CLI) just sleeps.
    """
    if hooks is None:
        time.sleep(seconds)
        return None
    remaining = seconds
    while remaining > 0:
        why = _abandoned(hooks)
        if why:
            return why
        time.sleep(min(0.25, remaining))
        remaining -= 0.25
    return _abandoned(hooks)


def _report_error(hooks, msg: str) -> None:
    """A registration failure: into the run's log when a run is driving, else stderr."""
    if hooks is not None:
        hooks.log(msg)
    else:
        print(msg, file=sys.stderr)


def register(server: str, token_path: str, quiet: bool = False, no_qr: bool = False,
             hooks=None) -> int:
    """Run the device-authorization flow: show the prompt and poll until approved or timed out.

    ``hooks`` (a ``RunHooks``) routes the prompt and status into a resident run's interface rather
    than the terminal: the URL/code/QR go to ``hooks.authorize`` and are taken back down by
    ``hooks.authorized`` when polling ends. With no hooks it prints, as it always has.
    """
    server = server.rstrip("/")
    status, body = http.post_json(
        server + "/api/v1/device/code",
        {"client_name": "alma-certify %s" % __version__, "hostname": hostname()},
    )
    if status != 200 or "device_code" not in body:
        _report_error(hooks, "registration failed (%s): %s" % (status, body))
        return 2

    device_code = body["device_code"]
    interval = float(body.get("interval", 5))
    expires_in = float(body.get("expires_in", 900))
    verification_uri = body.get("verification_uri", server + "/my/activate/")
    if verification_uri.startswith("/"):
        verification_uri = server + verification_uri
    user_code = body["user_code"]
    # The RFC 8628 "complete" URI carries the code as a query param, so scanning it lands on the
    # approval page with the code already filled in - the operator only logs in and clicks approve.
    # Prefer the server's, construct it from the plain URI + code when an older server omits it.
    complete_uri = body.get("verification_uri_complete") or "%s%scode=%s" % (
        verification_uri,
        "&" if "?" in verification_uri else "?",
        urllib.parse.quote(user_code),
    )
    if complete_uri.startswith("/"):
        complete_uri = server + complete_uri

    waiting = "Waiting for approval (expires in %d minutes)..." % (expires_in // 60)
    if hooks is not None:
        hooks.authorize(verification_uri=verification_uri, user_code=user_code,
                        complete_uri=complete_uri)
        hooks.log(waiting)
    else:
        print_authorization(verification_uri, user_code, complete_uri, no_qr=no_qr)
        print(waiting, flush=True)

    deadline = time.monotonic() + expires_in
    try:
        while time.monotonic() < deadline:
            abandoned = _wait(interval, hooks)
            if abandoned:
                # Given up on, from the front-end, while this waited for an approval that may never
                # come. Fifteen minutes is a long time to hold an interface with no other way out,
                # which is what this did: the poll ran to its deadline whatever it was being told.
                _report_error(hooks, "authorization abandoned: %s" % abandoned)
                return 2
            status, body = http.post_json(
                server + "/api/v1/device/token", {"device_code": device_code}
            )
            if status == 200 and body.get("token"):
                save_token(token_path, body)
                done = ("Authorized. Token stored in %s (expires %s)."
                        % (token_path, body.get("expires_at", "unknown")))
                if hooks is not None:
                    hooks.log(done)
                elif not quiet:
                    print(done, flush=True)
                return 0
            error = body.get("error", "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error in ("expired_token", "access_denied"):
                _report_error(hooks, "registration %s" % error.replace("_", " "))
                return 2
            _report_error(hooks, "unexpected response (%s): %s" % (status, body))
            return 2
        _report_error(hooks, "registration timed out")
        return 2
    finally:
        # However polling ended - approved, denied, or timed out - a run's interface must take the
        # authorization prompt back down. A terminal has nothing to undo.
        if hooks is not None:
            hooks.authorized()


def save_token(token_path: str, body: dict) -> None:
    """Write the submit token so only root can read it.

    The mode passed to ``os.open`` applies only when the file is created, so a token rewritten over
    one left world-readable by an earlier register would have kept the looser mode. It is set again
    explicitly. The directory is created 0700 when this makes it, and left alone when it already
    exists - it may be somewhere the operator chose with ``token_path``, and tightening a directory
    this did not create is not its business. The file is 0600 either way, which is the part that
    matters.
    """
    directory = os.path.dirname(token_path) or "."
    fresh = not os.path.isdir(directory)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if fresh:
        os.chmod(directory, 0o700)
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "token": body["token"],
                "expires_at": body.get("expires_at"),
                "hostname": hostname(),
            },
            fh,
        )
    os.chmod(token_path, 0o600)


def load_token(token_path: str) -> Optional[str]:
    try:
        with open(token_path, encoding="utf-8") as fh:
            return json.load(fh).get("token")
    except (OSError, json.JSONDecodeError):
        return None


def _parse_expiry(value: str) -> Optional[datetime.datetime]:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def token_status(token_path: str) -> Tuple[Optional[str], Optional[float]]:
    """Return (token, seconds_remaining). ``None`` seconds means no expiry
    recorded; a missing or unreadable token gives (None, None)."""
    try:
        with open(token_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None, None
    token = data.get("token")
    if not token:
        return None, None
    # A token stored for a different host will be refused server-side, so
    # treat it as absent rather than letting the run fail at upload time.
    stored_host = data.get("hostname")
    if stored_host and stored_host != hostname():
        return None, None
    expiry = _parse_expiry(str(data.get("expires_at") or ""))
    if expiry is None:
        return token, None
    now = datetime.datetime.now(datetime.timezone.utc)
    return token, (expiry - now).total_seconds()


# Outcomes of asking the server about a stored token. ``UNKNOWN`` is not a synonym for
# invalid: it means nobody could be asked, which must not stop a run.
TOKEN_LIVE = "live"
TOKEN_REJECTED = "rejected"
TOKEN_UNREACHABLE = "unknown"


def check_token(server: str, token: str, *, timeout: float = 10) -> Tuple[str, dict]:
    """Ask the server whether ``token`` still works. Returns (outcome, info).

    The stored ``expires_at`` is a claim about time, not about the token, and the two come
    apart for every reason a token dies early: revoked by its owner, deleted by an admin,
    or the server's database rebuilt underneath it. That last one is what prompted this -
    a run completed against a token that was hours from its stated expiry and had not
    existed since the database was wiped, and the failure landed at upload time with the
    whole run already spent.

    A 401 is the answer to the question. Anything else - connection refused, DNS failure,
    a 502 from a proxy, a server too old to have the endpoint - is *not* an answer, and is
    reported as unreachable so the caller can carry on. Refusing to test hardware because
    the catalog is down would be a worse bug than the one this fixes.
    """
    try:
        status, body = http.get_json(
            server + "/api/v1/token", token=token, timeout=timeout
        )
    except Exception:
        # Any transport failure at all. Deliberately broad: urllib raises a small zoo of
        # exceptions (URLError, socket.timeout, ssl.SSLError, and OSError underneath most
        # of them) and every one of them means the same thing here.
        return TOKEN_UNREACHABLE, {}
    if status == 401:
        return TOKEN_REJECTED, {}
    if status != 200 or not isinstance(body, dict):
        # Reachable but unexpected. Not proof the token is bad, so do not act as if it is.
        return TOKEN_UNREACHABLE, {}
    return TOKEN_LIVE, body


def ensure_token(
    server: str,
    token_path: str,
    *,
    min_remaining: float = DEFAULT_MIN_REMAINING,
    interactive: Optional[bool] = None,
    log=print,
    hooks=None,
) -> Optional[str]:
    """Return a usable token, registering interactively if needed.

    Called before a run starts rather than at upload time: discovering a
    missing or nearly-expired token after a multi-hour benchmark pass would
    waste the whole run.
    """
    explained = False
    token, remaining = token_status(token_path)
    if token and (remaining is None or remaining >= min_remaining):
        # Unexpired by the clock. Now ask the server, because that is the only thing that
        # actually knows - see check_token.
        outcome, info = check_token(server, token)
        if outcome == TOKEN_LIVE:
            if SUBMIT_SCOPE not in (info.get("scopes") or []):
                # Reachable, real, and useless for this: the run would produce a bundle
                # and be refused at upload. Worth saying before the run, not after.
                log(
                    "the stored token is valid but lacks the '%s' scope, so results "
                    "could not be uploaded" % SUBMIT_SCOPE
                )
            else:
                return token
        elif outcome == TOKEN_UNREACHABLE:
            log(
                "could not reach %s to check the stored token; continuing, and the "
                "results can be submitted later if the upload fails" % server
            )
            return token
        else:
            log(
                "the stored submission token is no longer valid on the server - it was "
                "revoked, or the server's records were rebuilt since it was issued"
            )
            # Explained already, so suppress the generic reason below. Without this the
            # next line read "no submission token is stored for this machine", which is
            # false - one is stored, it is just dead - and sends the reader looking in the
            # wrong place.
            token = None
            explained = True

    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()

    if explained:
        pass
    elif token and remaining is not None:
        log(
            "the stored submission token expires in %d minute(s), which may "
            "not outlast this run" % max(0, int(remaining // 60))
        )
    else:
        log("no submission token is stored for this machine")

    if not interactive:
        log(
            "not attached to a terminal, so authorization cannot be requested "
            "here; run `alma-certify register` or pass --no-submit"
        )
        return token if token else None

    if register(server, token_path, quiet=True, hooks=hooks) == 0:
        log("authorized; results will be submitted when the run finishes")
        return load_token(token_path)
    log("authorization did not complete; the run will continue without upload")
    return None
