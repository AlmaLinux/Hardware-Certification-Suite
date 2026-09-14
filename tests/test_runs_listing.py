"""Listing the runs on a machine.

Four commands take a run - ``report``, ``resume``, ``bundle``, ``submit`` - and every one of them
wants a UUID that nothing ever printed twice. The id is announced when a run starts and then lives
only in a directory name, so "which run was that" meant ``ls /var/lib/alma-certify/runs`` and
"which of those failed" meant opening each state.json by hand.

Read from ``state.json`` rather than ``report.json`` throughout, because the runs somebody goes
looking for are disproportionately the ones that did not finish, and those have no report at all.
"""

import json

import pytest

from alma_certify import cli


def make_run(base, run_id, *, started="2026-08-16T18:22:07Z", finished=None,
             run_types=("validate",), scope=(), results=None, plan_len=None,
             hostname="poweredge-r760", report=False):
    """One run directory, as ``RunState`` would leave it."""
    run_dir = base / run_id
    run_dir.mkdir()
    completed = {test: {"id": test, "status": status} for test, status in (results or {}).items()}
    filler = (plan_len or len(completed)) - len(completed)
    plan = list(completed) + ["filler-%d" % i for i in range(filler)]
    (run_dir / "state.json").write_text(json.dumps({
        "run_id": run_id,
        "meta": {
            "run_types": list(run_types), "claim_scope": list(scope),
            "started_at": started, "finished_at": finished, "hostname": hostname,
        },
        "completed": completed,
        "plan": plan,
    }))
    if report:
        (run_dir / "report.json").write_text("{}")
    return run_dir


def listing(capsys, base, *extra):
    assert cli.main(["runs", "--run-dir", str(base)] + list(extra)) == cli.EXIT_OK
    return capsys.readouterr().out


# --- what it lists ---------------------------------------------------------------


def test_a_run_is_listed_with_what_became_of_it(tmp_path, capsys):
    make_run(tmp_path, "9f3c1a2e-1111-4111-8111-111111111111",
             finished="2026-08-16T19:04:51Z", run_types=("collect", "validate"),
             results={"validate.cpu.flags": "pass", "validate.net.link": "fail"}, report=True)

    out = listing(capsys, tmp_path)

    assert "9f3c1a2e" in out
    assert "collect,validate" in out
    assert "1 pass, 1 fail" in out


def test_the_id_shown_is_the_one_the_other_commands_accept(tmp_path, capsys):
    """``RunState.find`` takes a unique prefix, so the eight characters in the table can be pasted
    straight into ``report`` or ``resume`` instead of retyping a UUID."""
    from alma_certify.state import RunState

    make_run(tmp_path, "9f3c1a2e-1111-4111-8111-111111111111", results={"a": "pass"})

    out = listing(capsys, tmp_path)
    short = out.splitlines()[1].split()[0]

    assert RunState.find(str(tmp_path), short).run_id == "9f3c1a2e-1111-4111-8111-111111111111"


def test_newest_first(tmp_path, capsys):
    """Never by directory name: a UUID sorts randomly, so the run somebody just made would turn up
    anywhere in the list."""
    make_run(tmp_path, "00000000-1111-4111-8111-111111111111", started="2026-08-10T11:02:00Z")
    make_run(tmp_path, "ffffffff-2222-4222-8222-222222222222", started="2026-08-17T08:14:55Z")

    lines = listing(capsys, tmp_path).splitlines()[1:]

    assert [line.split()[0] for line in lines] == ["ffffffff", "00000000"]


def test_a_whole_machine_claim_says_so(tmp_path, capsys):
    """Rather than an empty column. Most runs are whole-machine claims and a blank there reads as
    missing data."""
    make_run(tmp_path, "aaaaaaaa-1111-4111-8111-111111111111", scope=())

    assert "machine" in listing(capsys, tmp_path)


def test_a_scoped_claim_names_the_components(tmp_path, capsys):
    make_run(tmp_path, "bbbbbbbb-1111-4111-8111-111111111111", scope=("gpu", "cpu"))

    assert "gpu,cpu" in listing(capsys, tmp_path)


# --- the runs somebody is actually looking for -----------------------------------


def test_an_unfinished_run_says_how_far_it_got(tmp_path, capsys):
    """The reason this reads state.json and not report.json. Eight passes out of a plan of forty is
    not a machine that passed, and the old way to find that out was noticing report.json missing."""
    make_run(tmp_path, "0b71d4c9-2222-4222-8222-222222222222", finished=None,
             results={"bench.gpu.clpeak": "pass", "bench.gpu.cuda-bandwidth": "error"},
             plan_len=14)

    out = listing(capsys, tmp_path)

    assert "unfinished, 2 of 14 tests" in out
    assert "1 pass, 1 error" in out


def test_a_run_killed_before_its_first_save_is_not_a_crash(tmp_path, capsys):
    """``RunState.create`` makes the directory before writing anything into it, so a run interrupted
    in that window has no state.json at all."""
    (tmp_path / "dead0000-4444-4444-8444-444444444444").mkdir()
    make_run(tmp_path, "9f3c1a2e-1111-4111-8111-111111111111", results={"a": "pass"})

    out = listing(capsys, tmp_path)

    assert "9f3c1a2e" in out
    assert "dead0000" not in out, "there is nothing to say about it, so it is not a row"


def test_a_truncated_journal_is_listed_rather_than_skipped(tmp_path, capsys):
    """Silently omitting it would be the worst answer: somebody looking for a run that went wrong
    would be told it does not exist."""
    broken = tmp_path / "bad00000-5555-4555-8555-555555555555"
    broken.mkdir()
    (broken / "state.json").write_text('{"run_id": "bad0')

    out = listing(capsys, tmp_path)

    assert "bad00000" in out
    assert "unreadable" in out


def test_one_unreadable_run_does_not_hide_the_others(tmp_path, capsys):
    broken = tmp_path / "bad00000-5555-4555-8555-555555555555"
    broken.mkdir()
    (broken / "state.json").write_text("not json")
    make_run(tmp_path, "9f3c1a2e-1111-4111-8111-111111111111", results={"a": "pass"})

    out = listing(capsys, tmp_path)

    assert "9f3c1a2e" in out
    assert "bad00000" in out


def test_a_run_with_no_results_yet_says_that(tmp_path, capsys):
    make_run(tmp_path, "c4d5e6f7-3333-4333-8333-333333333333",
             finished="2026-08-10T11:03:10Z", run_types=("collect",), results={})

    assert "no tests ran" in listing(capsys, tmp_path)


def test_an_empty_run_directory_says_where_it_looked(tmp_path, capsys):
    """A bare prompt would leave somebody wondering whether the command worked or the path was
    wrong."""
    out = listing(capsys, tmp_path)

    assert str(tmp_path) in out
    assert "no runs" in out


def test_a_missing_run_directory_is_not_an_error(tmp_path, capsys):
    """Nothing has run on this machine yet, which is a fine state to be in and not a failure."""
    out = listing(capsys, tmp_path / "never-created")

    assert "no runs" in out


# --- shape and formatting --------------------------------------------------------


def test_the_columns_line_up_for_the_longest_real_value(tmp_path, capsys):
    """``collect,validate,benchmark`` is what a plain ``alma-certify run`` produces, and it is the
    commonest row there is. A column narrower than that pushed every field after it out of line."""
    make_run(tmp_path, "9f3c1a2e-1111-4111-8111-111111111111",
             run_types=("collect", "validate", "benchmark"),
             finished="2026-08-16T19:04:51Z", results={"a": "pass"}, report=True)
    make_run(tmp_path, "0b71d4c9-2222-4222-8222-222222222222",
             started="2026-08-10T11:02:00Z", run_types=("collect",), results={"a": "pass"})

    lines = listing(capsys, tmp_path).splitlines()
    header = lines[0]

    for line in lines[1:]:
        assert line.index("machine") == header.index("CLAIM"), line


def test_the_timestamp_is_readable_and_the_exact_one_is_in_json(tmp_path, capsys):
    make_run(tmp_path, "9f3c1a2e-1111-4111-8111-111111111111", started="2026-08-16T18:22:07Z")

    out = listing(capsys, tmp_path)
    rows = json.loads(listing(capsys, tmp_path, "--json"))

    assert "2026-08-16 18:22" in out
    assert rows[0]["started_at"] == "2026-08-16T18:22:07Z"


@pytest.mark.parametrize("stamp,expected", [
    ("2026-08-16T18:22:07Z", "2026-08-16 18:22"),
    (None, "?"),
    ("", "?"),
    # Anything unlike utcnow_iso's output is printed as it came, so a reader can see why.
    ("last tuesday", "last tuesday"),
])
def test_only_this_suites_own_timestamps_are_reformatted(stamp, expected):
    assert cli._short_time(stamp) == expected


def test_the_json_carries_what_the_table_leaves_out(tmp_path, capsys):
    """The table is for reading; the JSON is for a script. Hostname and the finished flag are in
    one and not the other on purpose: a run directory belongs to one machine, so its hostname is the
    same on every row."""
    make_run(tmp_path, "9f3c1a2e-1111-4111-8111-111111111111", hostname="gpu-node-04",
             finished="2026-08-16T19:04:51Z", results={"a": "pass", "b": "fail"}, report=True)

    rows = json.loads(listing(capsys, tmp_path, "--json"))

    assert rows[0]["hostname"] == "gpu-node-04"
    assert rows[0]["run_id"] == "9f3c1a2e-1111-4111-8111-111111111111"
    assert rows[0]["finished"] is True
    assert rows[0]["reported"] is True
    assert rows[0]["counts"] == {"pass": 1, "fail": 1}


def test_the_json_is_a_list_even_with_nothing_in_it(tmp_path, capsys):
    """So a script can iterate it without a special case."""
    assert json.loads(listing(capsys, tmp_path, "--json")) == []


def test_listing_runs_needs_root_like_everything_else(tmp_path, capsys, monkeypatch):
    """Reading changes nothing, but the thing being read is root's: the run directory is 0750
    root:root because it holds serial numbers and DMI UUIDs.

    This used to be exempt, on the reasoning that reading is harmless. What that produced was worse
    than a refusal: ``os.listdir`` raised PermissionError, the listing swallowed it, and a reader
    without root was told there were no runs on this machine on a machine full of them. The gate is
    at the entry point now and covers every command, and it offers to elevate rather than just
    refusing, so the answer is a sudo prompt instead of a wrong answer.
    """
    monkeypatch.setattr(cli.elevate, "is_root", lambda: False)
    monkeypatch.setattr(cli.elevate, "sudo_available", lambda: False)
    make_run(tmp_path, "9f3c1a2e-1111-4111-8111-111111111111", results={"a": "pass"})

    assert cli.main(["runs", "--run-dir", str(tmp_path)]) == cli.EXIT_USAGE
    assert "must run as root" in capsys.readouterr().err


# --- whether the server has it ---------------------------------------------------
#
# The one fact about a run that cannot be recovered by re-reading it. The results are on disk and
# the verdict is in the report; whether anybody received them is knowable only from the server, and
# before this the operator's only record of a successful upload was a line of terminal output.


def submitted(**overrides):
    record = {"at": "2026-08-16T19:06:12Z", "server": "https://catalog.almalinux.org",
              "url": "https://catalog.almalinux.org/runs/9f3c1a2e/", "status": "draft"}
    record.update(overrides)
    return record


def test_an_unsubmitted_run_says_no(tmp_path, capsys):
    make_run(tmp_path, "9f3c1a2e-1111-4111-8111-111111111111", results={"a": "pass"})

    lines = listing(capsys, tmp_path).splitlines()

    # By column position rather than by counting words from the end: the RESULT column holds a
    # variable number of words, so "the fourth from last" was reading a digit out of the tallies.
    column = lines[0].index("SUBMITTED")
    assert lines[1][column:].startswith("no")


def test_a_submitted_run_is_dated(tmp_path, capsys):
    """A date rather than a tick: "did this reach the server" and "when" are the same question,
    and a run submitted before a listing changed hands is worth dating."""
    run = make_run(tmp_path, "9f3c1a2e-1111-4111-8111-111111111111", results={"a": "pass"})
    state = json.loads((run / "state.json").read_text())
    state["meta"]["submission"] = submitted()
    (run / "state.json").write_text(json.dumps(state))

    out = listing(capsys, tmp_path)

    assert "2026-08-16" in out
    assert json.loads(listing(capsys, tmp_path, "--json"))[0]["submission"]["url"].endswith(
        "/runs/9f3c1a2e/"
    )


def test_the_columns_still_line_up_with_a_submission(tmp_path, capsys):
    run = make_run(tmp_path, "9f3c1a2e-1111-4111-8111-111111111111",
                   run_types=("collect", "validate", "benchmark"), results={"a": "pass"})
    state = json.loads((run / "state.json").read_text())
    state["meta"]["submission"] = submitted()
    (run / "state.json").write_text(json.dumps(state))
    make_run(tmp_path, "0b71d4c9-2222-4222-8222-222222222222",
             started="2026-08-10T11:02:00Z", results={"a": "pass"})

    lines = listing(capsys, tmp_path).splitlines()
    header = lines[0]

    for line in lines[1:]:
        assert line.index("machine") == header.index("CLAIM"), line


# --- what gets written, and when -------------------------------------------------


def _state(tmp_path, run_id="9f3c1a2e-1111-4111-8111-111111111111"):
    from alma_certify.state import RunState

    make_run(tmp_path, run_id, results={"a": "pass"})
    return RunState.find(str(tmp_path), run_id)


def test_a_successful_upload_is_written_onto_the_run(tmp_path):
    from alma_certify.state import RunState

    state = _state(tmp_path)

    cli._record_submission(state, "https://catalog.almalinux.org")({
        "web_url": "https://catalog.almalinux.org/runs/9f3c1a2e/",
        "status": "draft",
    })

    # Re-read from disk: an in-memory update nobody saved would be lost with the process.
    reloaded = RunState.find(str(tmp_path), "9f3c1a2e").meta["submission"]
    assert reloaded["at"]
    assert reloaded["server"] == "https://catalog.almalinux.org"
    assert reloaded["url"] == "https://catalog.almalinux.org/runs/9f3c1a2e/"
    assert reloaded["status"] == "draft"


def test_the_first_success_is_the_one_that_is_dated(tmp_path, monkeypatch):
    """Lumina is idempotent on the run id, so re-uploading succeeds and reports ``duplicate``. The
    interesting timestamp is when the evidence first landed, not when somebody last re-sent it."""
    state = _state(tmp_path)
    record = cli._record_submission(state, "https://catalog.almalinux.org")
    # Two different clocks, because ``utcnow_iso`` has second granularity: both calls in one process
    # produced the same string, so the assertion below held whether or not the code kept the first.
    stamps = iter(["2026-08-16T19:06:12Z", "2026-08-17T09:30:00Z"])
    monkeypatch.setattr(cli, "utcnow_iso", lambda: next(stamps))

    record({"web_url": "https://catalog.almalinux.org/runs/9f3c1a2e/"})
    first = state.meta["submission"]["at"]
    record({"web_url": "https://catalog.almalinux.org/runs/moved/", "duplicate": True})

    assert first == "2026-08-16T19:06:12Z"
    assert state.meta["submission"]["at"] == first
    assert state.meta["submission"]["url"].endswith("/runs/moved/"), "a moved run should re-point"


def test_a_server_that_sends_no_url_still_records_the_upload(tmp_path):
    """The upload is the fact; the link is a convenience."""
    state = _state(tmp_path)

    cli._record_submission(state, "https://lumina.example.org")({"uuid": "abc-123"})

    assert state.meta["submission"]["url"] == "abc-123"
    assert state.meta["submission"]["at"]


def test_a_server_that_sends_nothing_useful_is_not_a_crash(tmp_path):
    state = _state(tmp_path)

    cli._record_submission(state, "https://lumina.example.org")({})

    assert state.meta["submission"]["at"]
    assert "url" not in state.meta["submission"]


def test_only_a_success_records_anything(tmp_path, monkeypatch):
    """Through the real ``submit_run``, because the guarantee is about where the callback is called
    from: a rejected bundle must leave no trace of a submission that did not happen."""
    from alma_certify.submit import client

    state = _state(tmp_path)
    monkeypatch.setattr(client.bundle_mod, "bundle_run", lambda run_dir, out: out)
    monkeypatch.setattr(client, "RETRIES", 1)
    monkeypatch.setattr(
        client.http, "post_multipart",
        lambda *a, **kw: (400, "not a bundle"),
    )

    code = client.submit_run(
        server="https://lumina.example.org", run_dir=state.run_dir, token="t",
        token_path="/nonexistent",
        on_success=cli._record_submission(state, "https://lumina.example.org"),
    )

    assert code != 0
    assert "submission" not in state.meta


def test_the_callback_fires_on_a_successful_upload(tmp_path, monkeypatch):
    from alma_certify.submit import client

    state = _state(tmp_path)
    monkeypatch.setattr(client.bundle_mod, "bundle_run", lambda run_dir, out: out)
    monkeypatch.setattr(client, "print_outcome", lambda body, server=None, out=None: None)
    monkeypatch.setattr(
        client.http, "post_multipart",
        lambda *a, **kw: (201, {"web_url": "https://lumina.example.org/runs/9f3c1a2e/"}),
    )

    code = client.submit_run(
        server="https://lumina.example.org", run_dir=state.run_dir, token="t",
        token_path="/nonexistent",
        on_success=cli._record_submission(state, "https://lumina.example.org"),
    )

    assert code == 0
    assert state.meta["submission"]["url"].endswith("/runs/9f3c1a2e/")


def test_the_upload_is_recorded_even_if_reporting_it_goes_wrong(tmp_path, monkeypatch):
    """Recorded before the outcome is printed. The evidence is on the server either way, and that is
    the fact worth keeping."""
    from alma_certify.submit import client

    state = _state(tmp_path)
    monkeypatch.setattr(client.bundle_mod, "bundle_run", lambda run_dir, out: out)
    monkeypatch.setattr(client.http, "post_multipart", lambda *a, **kw: (200, {"uuid": "abc"}))

    def explode(body, server=None, out=None):
        raise RuntimeError("terminal on fire")

    monkeypatch.setattr(client, "print_outcome", explode)

    with pytest.raises(RuntimeError):
        client.submit_run(
            server="https://lumina.example.org", run_dir=state.run_dir, token="t",
            token_path="/nonexistent",
            on_success=cli._record_submission(state, "https://lumina.example.org"),
        )

    assert state.meta["submission"]["at"]


def test_the_report_says_whether_the_server_has_it(tmp_path):
    state = _state(tmp_path)

    assert "not submitted" in cli._submission_line(state)
    assert "alma-certify submit 9f3c1a2e" in cli._submission_line(state)

    state.meta["submission"] = submitted()

    line = cli._submission_line(state)
    assert "submitted 2026-08-16T19:06:12Z" in line
    assert "catalog.almalinux.org/runs/9f3c1a2e/" in line
    # ``draft`` means uploaded and not yet submitted for review, which is the distinction
    # ``print_outcome`` exists to make. A reader here is owed the same warning.
    assert "not yet submitted for review" in line


def test_a_submission_past_the_draft_stage_is_not_nagged_about(tmp_path):
    state = _state(tmp_path)
    state.meta["submission"] = submitted(status="submitted")

    assert "not yet submitted for review" not in cli._submission_line(state)


def test_both_submission_paths_record_it(tmp_path, monkeypatch, capsys):
    """The wiring, at both call sites, and this is why: ``submit_run`` is called from two places,
    the upload at the end of a run and ``alma-certify submit`` afterwards. A hook passed at only
    one of them records half the submissions there are, and deleting either argument changed
    nothing that failed until this test existed.
    """
    import argparse

    from alma_certify.submit import client

    recorded = {}

    def fake_submit_run(**kwargs):
        recorded[kwargs["run_dir"]] = kwargs.get("on_success")
        assert kwargs.get("on_success") is not None, "no way to record this upload"
        kwargs["on_success"]({"web_url": "https://lumina.example.org/runs/x/"})
        return 0

    monkeypatch.setattr(client, "submit_run", fake_submit_run)
    monkeypatch.setattr(cli, "_refuse_unsupported_submit", lambda state: None)
    monkeypatch.setattr(cli, "_resolve_server", lambda config: "https://lumina.example.org")

    # 1. the upload at the end of a run
    during = _state(tmp_path, "aaaaaaaa-1111-4111-8111-111111111111")
    config = cli.Config.load(None)
    config.set("general", "server", "https://lumina.example.org")
    config.set("general", "run_dir", str(tmp_path))
    cli._autosubmit(during, config, "token", cli.TerminalHooks(during.run_dir))

    # 2. alma-certify submit, afterwards
    by_hand = _state(tmp_path, "bbbbbbbb-2222-4222-8222-222222222222")
    monkeypatch.setattr(cli, "_config", lambda args: config)
    assert cli.cmd_submit(argparse.Namespace(
        run_id="bbbbbbbb", token=None, pre_release=False, publish_after=None,
        support_from_minor=None, run_dir=str(tmp_path),
    )) == 0

    assert set(recorded) == {during.run_dir, by_hand.run_dir}
    for run_id in ("aaaaaaaa", "bbbbbbbb"):
        submission = json.loads(
            (tmp_path / [d.name for d in tmp_path.iterdir() if d.name.startswith(run_id)][0]
             / "state.json").read_text()
        )["meta"]["submission"]
        assert submission["at"], run_id
        assert submission["url"].endswith("/runs/x/"), run_id
