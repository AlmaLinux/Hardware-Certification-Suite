"""Submit a finished run bundle to Lumina."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import urllib.parse
from typing import Callable, Optional

from .. import bundle as bundle_mod
from . import auth, http

RETRIES = 3

_RULE = "=" * 70

# Where a submitter finds their own runs, and what the row says when one is still
# owed something. Named exactly as the page renders them, so the instruction can
# be followed by eye rather than guessed at: the card is "My validation runs", a
# draft's status reads "Awaiting submitter details", and its action link is
# "Finish submission".
DASHBOARD_PATH = "/my/"
DASHBOARD_TABLE = "My validation runs"
DASHBOARD_ACTION = "Finish submission"
# The status badge Lumina shows for a draft. Its own constant because it is the
# server's wording, not ours: it comes from TestRun.STATUS_CHOICES, and if it ever
# changes there this is the line that has to follow.
_DRAFT_STATUS_LABEL = "Awaiting submitter details"


def _dashboard_url(url: str, server: Optional[str]) -> str:
    """The submitter's dashboard, on the same host as the run page.

    Derived from ``web_url`` in preference to the configured ``server`` because
    the two can differ - a server behind a proxy reports its public name, which is
    the one that will actually work in a browser. Falls back to ``server``, and to
    the bare path if neither is usable, which is still followable.
    """
    for candidate in (url, server):
        if not candidate:
            continue
        parts = urllib.parse.urlsplit(candidate)
        if parts.scheme and parts.netloc:
            return urllib.parse.urlunsplit(
                (parts.scheme, parts.netloc, DASHBOARD_PATH, "", "")
            )
    return DASHBOARD_PATH


def _stderr(text: str) -> None:
    print(text, file=sys.stderr)


def print_outcome(body: dict, server: Optional[str] = None, out=None) -> None:
    """Say what the server did with the bundle, and what is still owed.

    Three things used to make a finished run read as *finished* when it was not:
    the ``PASS`` verdict, the word "accepted", and a URL. Each is true, none of
    them means certified, and the one line that said so came last, after two that
    looked conclusive - so it got skipped.

    So: a neutral first line, and then, when something is outstanding, a block
    that leads with what is outstanding rather than mentioning it as a footnote.
    "uploaded" instead of "accepted", because what the server accepted was the
    bundle, not the certification.

    ``out`` is where each line goes; it defaults to ``print`` (the CLI), and a resident run passes
    ``hooks.out`` so the link and the action-required notice land in the interface, not behind it.
    """
    out = out or print
    status_value = str(body.get("status") or "")
    url = body.get("web_url") or body.get("uuid") or ""
    prefix = "already on the server" if body.get("duplicate") else "uploaded"

    out("\n%s: %s" % (prefix, url))

    # Devices the server unticked by default per its exclusion rules (a BMC display adapter, an
    # onboard iGPU). Said here so it is not a surprise in review; a reviewer can still include them.
    excluded = body.get("excluded_components") or []
    if excluded:
        out("\nnot attached as components (excluded by rule; a reviewer can include them):")
        for item in excluded:
            kind = item.get("kind") or "device"
            name = item.get("component") or "a device"
            reason = item.get("reason") or ""
            out("  - %s %s: %s" % (kind, name, reason))

    if status_value == "draft":
        out(_RULE)
        out(" ACTION REQUIRED - this run is NOT submitted for review yet")
        out(_RULE)
        out(
            "A PASS verdict means the tests passed on this machine. It does not\n"
            "mean the hardware is certified: that takes a reviewer, and a reviewer\n"
            "cannot start until the listing details only you can supply are filled\n"
            "in - the marketing name, a description, and a link to the spec sheet.\n"
            "\n"
            "  1. open  %s\n"
            "  2. add the listing details\n"
            '  3. press "Submit for review"\n'
            "\n"
            "Lost the link? It is also on your dashboard, so you never have to keep\n"
            "this terminal output:\n"
            "\n"
            "  %s\n"
            '  under "%s", listed as "%s"\n'
            '  with "%s" in the Action column.\n'
            "\n"
            "Nothing is reviewed, certified, or published until you do. Until then\n"
            "this run is visible only to you."
            % (url, _dashboard_url(url, server), DASHBOARD_TABLE,
               _DRAFT_STATUS_LABEL, DASHBOARD_ACTION)
        )
        out(_RULE)
    elif status_value == "quarantined":
        # Unreachable from this version, which refuses to submit a non-AlmaLinux
        # run at all. Handled anyway: the server can return it, and reporting
        # "uploaded" and stopping would leave the person believing it counted.
        out(_RULE)
        out(" NOT ACCEPTED AS EVIDENCE - the report says this was not AlmaLinux")
        out(_RULE)
        out(
            "The run is stored for a reviewer to look at, but it cannot certify an\n"
            "AlmaLinux release and will not appear publicly. If the operating\n"
            "system was misreported, say so on the run page above."
        )
        out(_RULE)
    elif status_value == "pending":
        out("Queued for review. Nothing further is needed from you.")
    elif status_value:
        out("Server-side status: %s" % status_value)


def submit_run(
    server: str,
    run_dir: str,
    token: Optional[str],
    token_path: str,
    pre_release: Optional[bool] = None,
    publish_after: Optional[str] = None,
    support_from_minor: Optional[int] = None,
    anonymous: Optional[bool] = None,
    on_success: Optional[Callable[[dict], None]] = None,
    out=None,
    err=None,
) -> int:
    """Upload a run. ``0`` on success, and then ``on_success`` gets the server's reply.

    ``out`` carries the result and ``err`` the failures and retries. Both default to the terminal
    (stdout, stderr), and a resident run passes its hooks for both: the success link already went
    through ``out``, but a rejected token or a server refusal went to a stderr the interface never
    shows, so an upload could fail with nothing on screen to say so.

    A callback rather than a richer return value, because the return value is an exit code two
    callers already pass straight up, and a callback is also the only thing that can distinguish
    *this* success from a caller that later sees 0 and has no idea what was uploaded or where it
    went. It is handed the parsed body, so what gets recorded about a submission is the caller's
    business and this module stays ignorant of run state.
    """
    out = out or print
    server = server.rstrip("/")
    err = err or _stderr
    if token is None:
        token = auth.load_token(token_path)
    if not token:
        err(
            "no token available - run `alma-certify register` first or pass --token"
        )
        return 3

    with tempfile.TemporaryDirectory(prefix="alma-certify-submit-") as tmp:
        bundle_path = bundle_mod.bundle_run(
            run_dir, os.path.join(tmp, "bundle.tar.zst")
        )
        fields = {}
        if pre_release is not None:
            fields["pre_release"] = "true" if pre_release else "false"
        if publish_after:
            fields["publish_after"] = publish_after
        # Sent alongside the bundle as well as riding inside its report metadata, so
        # ``alma-certify submit`` can correct a value the run was started without. Same
        # arrangement as the two above, and the server prefers the explicit field.
        if support_from_minor is not None:
            fields["support_from_minor"] = str(support_from_minor)
        # Left out entirely unless asked for, rather than sent as a false. The server reads
        # an absent field as "no instruction" and lets the submitter's account-wide setting
        # decide; a false would silently override it for every run from that machine.
        if anonymous is not None:
            fields["anonymous"] = "true" if anonymous else "false"

        for attempt in range(1, RETRIES + 1):
            status, body = http.post_multipart(
                server + "/api/v1/results/",
                fields=fields,
                file_field="bundle",
                file_path=bundle_path,
                token=token,
            )
            if status in (200, 201):
                if on_success is not None:
                    # Before the outcome is printed, so a run whose upload succeeded is recorded as
                    # submitted even if something in that reporting goes wrong afterwards. The
                    # evidence is on the server either way, and that is the fact worth keeping.
                    on_success(body if isinstance(body, dict) else {})
                print_outcome(body, server=server, out=out)
                return 0
            if status == 401:
                err("token rejected - run `alma-certify register` again")
                return 3
            if status == 409:
                err(
                    "conflict: this run id exists on the server with different "
                    "content: %s" % body
                )
                return 2
            if status in (400, 413):
                err("rejected (%s): %s" % (status, body))
                return 2
            # 5xx / transient: retry with backoff
            if attempt < RETRIES:
                wait = 5 * attempt
                err(
                    "server error (%s), retrying in %ds..." % (status, wait)
                )
                time.sleep(wait)
        err("giving up after %d attempts (%s): %s" % (RETRIES, status, body))
        return 2


def submit_survey(
    server: str,
    run_dir: str,
    token: Optional[str],
    token_path: str,
    on_success: Optional[Callable[[dict], None]] = None,
    out=None,
    err=None,
) -> int:
    """Upload an inventory-only run to the hardware-survey endpoint. ``0`` on success.

    A survey submission is not certification evidence: it carries no pre-release or
    publish-after fields, creates no reviewable draft, and needs nothing from the
    submitter afterwards. So this is ``submit_run`` stripped to the essentials, aimed
    at ``/api/v1/survey/`` instead of the results queue.
    """
    out = out or print
    server = server.rstrip("/")
    err = err or _stderr
    if token is None:
        token = auth.load_token(token_path)
    if not token:
        err(
            "no token available - run `alma-certify register` first or pass --token"
        )
        return 3

    with tempfile.TemporaryDirectory(prefix="alma-certify-survey-") as tmp:
        bundle_path = bundle_mod.bundle_run(
            run_dir, os.path.join(tmp, "bundle.tar.zst")
        )
        for attempt in range(1, RETRIES + 1):
            status, body = http.post_multipart(
                server + "/api/v1/survey/",
                fields={},
                file_field="bundle",
                file_path=bundle_path,
                token=token,
            )
            if status in (200, 201):
                if on_success is not None:
                    on_success(body if isinstance(body, dict) else {})
                out("\nsubmitted to the AlmaLinux hardware survey. Nothing further is "
                    "needed - only aggregate statistics are ever published.")
                return 0
            if status == 401:
                err("token rejected - run `alma-certify register` again")
                return 3
            if status in (400, 413):
                err("rejected (%s): %s" % (status, body))
                return 2
            if attempt < RETRIES:
                wait = 5 * attempt
                err(
                    "server error (%s), retrying in %ds..." % (status, wait)
                )
                time.sleep(wait)
        err("giving up after %d attempts (%s): %s" % (RETRIES, status, body))
        return 2
