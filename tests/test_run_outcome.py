"""What a run says when it finishes, and whether the reader can tell it is not done.

A validation run is uploaded as a **draft**: it is stored, but no reviewer sees it
until the submitter adds the listing details the suite cannot detect. Three things
used to make that read as finished anyway -

- ``certification verdict: PASS`` announced a *certification* result when all it
  reports is that the required tests passed on this machine,
- ``accepted`` described the bundle, and sounded like the submission, and
- a URL, which looks like somewhere the result already lives,

- and the one line saying otherwise came last, after those three. People stopped
reading at "PASS" and "accepted".

None of that output had a single test, which is how the wording drifted into
implying the opposite of the truth. It does now.
"""

import pytest

from alma_certify import cli
from alma_certify.submit import client

URL = "https://catalog.almalinux.org/results/runs/4f6c2a4e/"


def _outcome(capsys, **body):
    body.setdefault("web_url", URL)
    client.print_outcome(body)
    return capsys.readouterr().out


def _lines(text):
    return [line for line in text.splitlines() if line.strip()]


def _summary(capsys, verdict_pass=True):
    cli._print_summary({"results": [
        {"id": "validate.cpu.functional", "run_type": "validate",
         "status": "pass" if verdict_pass else "fail", "severity": "required",
         "reason": None},
    ]})
    return capsys.readouterr().out


# --- a draft is unmistakably unfinished ---------------------------------------


def test_a_draft_says_action_is_required(capsys):
    out = _outcome(capsys, status="draft")

    assert "ACTION REQUIRED" in out


def test_a_draft_says_it_is_not_submitted_for_review(capsys):
    out = _outcome(capsys, status="draft")

    assert "NOT submitted for review" in out


def test_the_action_notice_is_the_last_thing_printed(capsys):
    """The heart of the complaint. The old note existed but came after two lines
    that read as conclusive, so it was skipped - nothing may follow it that could
    look like a sign-off.
    """
    out = _outcome(capsys, status="draft")
    lines = _lines(out)

    assert lines[-1].startswith("="), lines[-1]
    # And the notice comes after the URL line, not before it.
    assert lines.index("uploaded: " + URL) < next(
        i for i, line in enumerate(lines) if "ACTION REQUIRED" in line
    )


def test_a_draft_is_not_called_accepted(capsys):
    """"accepted" was true of the bundle and false of the submission."""
    out = _outcome(capsys, status="draft")

    assert "accepted" not in out.lower()


def test_a_draft_spells_out_that_pass_is_not_certified(capsys):
    """The specific misreading to head off."""
    out = _outcome(capsys, status="draft")

    assert "does not" in out
    assert "certified" in out


def test_a_draft_gives_numbered_steps(capsys):
    """"Open the link and add details" left the order and the finish line vague."""
    out = _outcome(capsys, status="draft")

    assert "1. open" in out
    assert "2. add the listing details" in out
    assert '3. press "Submit for review"' in out


def test_a_draft_says_what_happens_if_ignored(capsys):
    """The consequence, not just the instruction."""
    out = _outcome(capsys, status="draft")

    assert "visible only to you" in out


def test_the_url_is_still_there(capsys):
    """Making it clearer must not remove the thing people actually need."""
    out = _outcome(capsys, status="draft")

    assert out.count(URL) >= 2  # the status line and the numbered step


# --- finding it again without the terminal output -----------------------------


def test_a_draft_also_points_at_the_dashboard(capsys):
    """A URL in scrolled-away terminal output is a URL you have lost. The
    dashboard is the durable route back to the same run."""
    out = _outcome(capsys, status="draft")

    assert "https://catalog.almalinux.org/my/" in out
    assert "dashboard" in out


def test_the_dashboard_hint_names_what_to_look_for(capsys):
    """"It is on your dashboard" is not a route if the page has six cards.

    These four strings are Lumina's, quoted verbatim so the instruction can be
    followed by eye. The server side pins them in
    ``lumina/accounts/tests/test_dashboard.py`` - renaming one there fails a test
    rather than silently making this text wrong.
    """
    out = _outcome(capsys, status="draft")

    assert "My validation runs" in out
    assert "Awaiting submitter details" in out
    assert "Finish submission" in out
    assert "Action column" in out


def test_the_dashboard_is_on_the_same_host_as_the_run_page(capsys):
    """Derived from web_url rather than the configured server, because a server
    behind a proxy reports the public name - the one that works in a browser."""
    client.print_outcome(
        {"status": "draft", "web_url": "https://public.example.org/results/runs/x/"},
        server="http://10.0.0.5:8000",
    )

    out = capsys.readouterr().out
    assert "https://public.example.org/my/" in out
    assert "10.0.0.5" not in out


def test_the_configured_server_is_the_fallback(capsys):
    """When the response carries only a uuid there is no origin to derive from."""
    client.print_outcome(
        {"status": "draft", "uuid": "4f6c2a4e"},
        server="https://catalog.almalinux.org",
    )

    assert "https://catalog.almalinux.org/my/" in capsys.readouterr().out


def test_with_no_host_at_all_the_path_is_still_shown(capsys):
    """Degrades to something followable rather than to "None/my/"."""
    client.print_outcome({"status": "draft", "uuid": "4f6c2a4e"})

    out = capsys.readouterr().out
    assert "/my/" in out
    assert "None" not in out


def test_a_pending_run_gets_no_dashboard_instructions(capsys):
    """Nothing to find and nothing to do, so pointing at the dashboard would imply
    otherwise."""
    out = _outcome(capsys, status="pending")

    assert "My validation runs" not in out
    assert "Finish submission" not in out


# --- states with nothing outstanding ------------------------------------------


def test_a_pending_run_says_nothing_is_needed(capsys):
    """Benchmark and collect runs go straight to the queue. Crying "action
    required" at them would train people to ignore it."""
    out = _outcome(capsys, status="pending")

    assert "ACTION REQUIRED" not in out
    assert "Nothing further is needed" in out


def test_a_resubmitted_bundle_says_so(capsys):
    out = _outcome(capsys, status="pending", duplicate=True)

    assert "already on the server" in out


def test_a_resubmitted_draft_still_asks_for_the_details(capsys):
    """Re-uploading does not discharge the obligation, so the notice has to
    survive the duplicate path."""
    out = _outcome(capsys, status="draft", duplicate=True)

    assert "ACTION REQUIRED" in out


def test_a_quarantined_run_does_not_claim_to_be_evidence(capsys):
    """Unreachable from this version, which refuses to submit one. Reporting
    "uploaded" and stopping would leave the person believing it counted."""
    out = _outcome(capsys, status="quarantined")

    assert "NOT ACCEPTED AS EVIDENCE" in out
    assert "cannot certify" in out


def test_an_unknown_status_is_reported_rather_than_swallowed(capsys):
    """A server newer than the client should not fall silently through."""
    out = _outcome(capsys, status="something-new")

    assert "something-new" in out


def test_a_missing_status_still_prints_the_url(capsys):
    out = _outcome(capsys)

    assert URL in out


# --- the verdict line ---------------------------------------------------------


def test_the_verdict_is_not_called_a_certification_verdict(capsys):
    """It reports whether the required tests passed here. Calling that a
    certification verdict announced a decision nobody had made."""
    out = _summary(capsys)

    assert "certification verdict" not in out
    assert "test verdict: PASS" in out


def test_a_passing_verdict_says_review_is_still_needed(capsys):
    out = _summary(capsys)

    assert "still needs review" in out


def test_a_failing_verdict_does_not_add_the_qualifier(capsys):
    """Nothing to clarify: a FAIL is not going to be mistaken for certified."""
    out = _summary(capsys, verdict_pass=False)

    assert "test verdict: FAIL" in out
    assert "still needs review" not in out


def test_a_run_with_no_validation_results_has_no_verdict(capsys):
    """A benchmark-only run is not pass or fail, and inventing a verdict for it
    would be the same false-finality problem in another place."""
    cli._print_summary({"results": [
        {"id": "bench.cpu.sysbench", "run_type": "benchmark", "status": "pass",
         "severity": None, "reason": None},
    ]})

    assert "verdict" not in capsys.readouterr().out


# --- the wording is exercised end to end --------------------------------------


@pytest.mark.parametrize("status_value", ["draft", "pending", "quarantined"])
def test_every_state_names_the_run_page(capsys, status_value):
    """Whatever the state, the reader needs to know where to look."""
    out = _outcome(capsys, status=status_value)

    assert URL in out


@pytest.mark.parametrize("http_status", [201, 200])
def test_submit_run_actually_prints_the_outcome(capsys, monkeypatch, http_status):
    """The wiring, not just the formatter.

    Every test above calls ``print_outcome`` directly, so all of them stayed green
    when the old "accepted" wording was restored at the *call site* - the formatter
    was untouched and simply went unused. This drives ``submit_run`` far enough to
    prove the upload path is what produces that output, which is the only version
    a person ever sees.
    """
    monkeypatch.setattr(client.bundle_mod, "bundle_run",
                        lambda run_dir, out: out)
    monkeypatch.setattr(
        client.http, "post_multipart",
        lambda *a, **kw: (http_status, {"status": "draft", "web_url": URL,
                                        "uuid": "4f6c2a4e"}),
    )

    code = client.submit_run(
        server="https://catalog.almalinux.org", run_dir="/nonexistent",
        token="tok", token_path="/nonexistent/token.json",
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "ACTION REQUIRED" in out, "the upload path did not print the notice"
    assert "accepted" not in out.lower()


# --- devices excluded by server rules -------------------------------------------


def test_excluded_components_are_named_with_their_reason(capsys):
    out = _outcome(capsys, status="pending", excluded_components=[
        {"kind": "GPU", "component": "ASPEED Graphics Family",
         "reason": "management display adapter (BMC console), not an accelerator under test"},
        {"kind": "GPU", "component": "Raphael", "reason": "onboard iGPU"},
    ])
    assert "excluded by rule" in out
    assert "ASPEED Graphics Family" in out and "management display adapter" in out
    assert "Raphael" in out and "onboard iGPU" in out
    assert "a reviewer can include them" in out


def test_nothing_is_printed_about_exclusions_when_there_are_none(capsys):
    out = _outcome(capsys, status="pending", excluded_components=[])
    assert "excluded by rule" not in out
    out = _outcome(capsys, status="pending")  # key absent entirely
    assert "excluded by rule" not in out


# --- failures reach the driver, not a swallowed stderr --------------------------


def test_upload_failures_go_through_the_err_seam(monkeypatch):
    """Success already went through ``out``; every failure went to stderr. Under the interface
    stderr is not shown, so a rejected token failed an upload with nothing on screen."""
    monkeypatch.setattr(client.bundle_mod, "bundle_run", lambda run_dir, out: out)
    monkeypatch.setattr(client.http, "post_multipart", lambda *a, **kw: (401, {"detail": "bad"}))
    seen = []

    code = client.submit_run(server="https://x", run_dir="/nonexistent", token="tok",
                             token_path="/nonexistent", out=lambda m: None, err=seen.append)

    assert code == 3
    assert seen == ["token rejected - run `alma-certify register` again"]


def test_survey_failures_go_through_the_err_seam_too(monkeypatch):
    monkeypatch.setattr(client.bundle_mod, "bundle_run", lambda run_dir, out: out)
    monkeypatch.setattr(client.http, "post_multipart", lambda *a, **kw: (413, "too big"))
    seen = []

    code = client.submit_survey(server="https://x", run_dir="/nonexistent", token="tok",
                                token_path="/nonexistent", out=lambda m: None, err=seen.append)

    assert code == 2
    assert seen == ["rejected (413): too big"]


def test_failures_still_reach_stderr_on_the_command_line(monkeypatch, capsys):
    """The default: nothing passed, and the terminal user sees it where they always did."""
    monkeypatch.setattr(client.bundle_mod, "bundle_run", lambda run_dir, out: out)
    monkeypatch.setattr(client.http, "post_multipart", lambda *a, **kw: (401, {}))

    client.submit_run(server="https://x", run_dir="/nonexistent", token="tok",
                      token_path="/nonexistent")

    captured = capsys.readouterr()
    assert "token rejected" in captured.err
    assert captured.out == ""


def test_the_engine_hands_its_note_channel_to_the_client():
    import inspect
    source = inspect.getsource(cli._autosubmit)
    assert source.count("err=hooks.note") == 2, "both submit paths must route failures"
