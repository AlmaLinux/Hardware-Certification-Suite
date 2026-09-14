"""Running anywhere, submitting only from AlmaLinux.

The suite is deliberately runnable on any distribution: a tool that refuses to
execute anywhere but the one supported OS is useless for working out whether the
OS is what broke. But a result from Rocky or RHEL is not evidence about AlmaLinux,
so it must not become a submission.

That splits into three rules, each tested here:

1. **Execution** warns and asks. Continuing is the surprising answer, so it is the
   one that has to be typed; declining is the default.
2. **No terminal** refuses rather than prompting. A blocked prompt in a kickstart
   ``%post`` or a CI job hangs forever, and reading EOF as consent would opt them
   in silently.
3. **Submission** is judged from the *report*, not the live host, because bundling
   on one machine and uploading from another is normal and would otherwise be the
   way around the check.
"""

import json
import os

import pytest

from alma_certify import cli, hostos

ALMA_9 = (
    'NAME="AlmaLinux"\nID="almalinux"\nVERSION_ID="9.6"\n'
    'PRETTY_NAME="AlmaLinux 9.6 (Sage Margay)"\n'
)
ROCKY_9 = (
    'NAME="Rocky Linux"\nID="rocky"\nVERSION_ID="9.6"\n'
    'PRETTY_NAME="Rocky Linux 9.6 (Blue Onyx)"\n'
    'ID_LIKE="rhel centos fedora"\n'
)


def _os_release(tmp_path, content):
    path = tmp_path / "os-release"
    path.write_text(content, encoding="utf-8")
    return str(path)


# --- detection ----------------------------------------------------------------


def test_almalinux_is_recognised(tmp_path):
    host = hostos.detect(_os_release(tmp_path, ALMA_9))

    assert host.is_almalinux
    assert host.version_id == "9.6"


def test_a_rebuild_is_not_almalinux(tmp_path):
    host = hostos.detect(_os_release(tmp_path, ROCKY_9))

    assert not host.is_almalinux
    assert "Rocky" in host.display


def test_id_like_is_not_consulted(tmp_path):
    """``ID`` is compared exactly; ``ID_LIKE`` is deliberately ignored.

    Uses a distribution that claims AlmaLinux kinship in ``ID_LIKE`` but is not
    AlmaLinux. Asserting Rocky is not AlmaLinux would prove nothing here, because
    Rocky's ``ID_LIKE`` says "rhel centos fedora" and never mentions AlmaLinux at
    all: the check would pass whether or not the field were consulted.
    """
    host = hostos.detect(_os_release(tmp_path, (
        'NAME="Downstream Rebuild"\nID="downstream"\nVERSION_ID="9.6"\n'
        'ID_LIKE="almalinux rhel fedora"\n'
    )))

    assert not host.is_almalinux, "ID_LIKE was treated as an identity"


def test_a_missing_os_release_is_not_almalinux(tmp_path):
    """"Cannot tell" must not mean "supported", or a stripped container image is
    the way around the gate."""
    host = hostos.detect(str(tmp_path / "does-not-exist"))

    assert not host.is_almalinux
    assert host.display  # still says *something* rather than an empty string


def test_quoting_and_comments_are_handled(tmp_path):
    host = hostos.detect(_os_release(
        tmp_path, "# a comment\nID=almalinux\nVERSION_ID='10.0'\n"
    ))

    assert host.is_almalinux
    assert host.version_id == "10.0"


# --- the prompt ---------------------------------------------------------------


class _Recorder:
    def __init__(self):
        self.text = ""

    def write(self, chunk):
        self.text += chunk


def _rocky():
    return hostos.HostOS("rocky", "9.6", "Rocky Linux 9.6")


def test_the_warning_names_the_os_and_says_only_almalinux_is_supported():
    out = _Recorder()

    hostos.warn(_rocky(), out=out)

    assert "Rocky Linux 9.6" in out.text
    assert "only supported operating system" in out.text
    assert "cannot be submitted" in out.text


def test_declining_stops_the_run():
    out = _Recorder()

    assert hostos.confirm(
        _rocky(), interactive=True, out=out, reader=lambda _: "n"
    ) is False


def test_just_pressing_enter_declines():
    """Continuing is the surprising outcome, so it has to be typed."""
    out = _Recorder()

    assert hostos.confirm(
        _rocky(), interactive=True, out=out, reader=lambda _: ""
    ) is False


@pytest.mark.parametrize("answer", ["y", "Y", "yes", " YES "])
def test_agreeing_proceeds(answer):
    out = _Recorder()

    assert hostos.confirm(
        _rocky(), interactive=True, out=out, reader=lambda _: answer
    ) is True


def test_the_flag_skips_the_prompt():
    out = _Recorder()
    asked = []

    result = hostos.confirm(
        _rocky(), assume_yes=True, interactive=True, out=out,
        reader=lambda p: asked.append(p) or "n",
    )

    assert result is True
    assert asked == [], "asked despite --allow-unsupported-os"


def test_without_a_terminal_it_refuses_instead_of_prompting():
    """The important one. Prompting into a closed stdin hangs a CI job forever."""
    out = _Recorder()
    asked = []

    result = hostos.confirm(
        _rocky(), interactive=False, out=out,
        reader=lambda p: asked.append(p) or "y",
    )

    assert result is False
    assert asked == [], "prompted with no terminal attached"
    assert "--allow-unsupported-os" in out.text, "did not name the way forward"


def test_eof_is_not_consent():
    out = _Recorder()

    def raise_eof(_):
        raise EOFError

    assert hostos.confirm(
        _rocky(), interactive=True, out=out, reader=raise_eof
    ) is False


# --- the report is what decides submission ------------------------------------


def _report(os_id):
    return {"environment": {"os": {"id": os_id, "version_id": "9.6"}}}


def test_the_report_decides_not_the_host():
    assert hostos.report_is_almalinux(_report("almalinux")) is True
    assert hostos.report_is_almalinux(_report("rocky")) is False


def test_a_report_with_no_environment_is_not_almalinux():
    assert hostos.report_is_almalinux({}) is False
    assert hostos.report_is_almalinux({"environment": {}}) is False


class _FakeState:
    """Just enough RunState for the submit refusal."""

    def __init__(self, run_dir, meta):
        self.run_dir = str(run_dir)
        self.meta = meta
        self.run_id = "0123456789abcdef"


def _state(tmp_path, *, report_os=None, meta=None):
    if report_os is not None:
        with open(os.path.join(str(tmp_path), "report.json"), "w") as fh:
            json.dump(_report(report_os), fh)
    return _FakeState(tmp_path, meta or {})


def test_submit_is_refused_for_a_non_almalinux_run(tmp_path, capsys):
    state = _state(tmp_path, report_os="rocky")

    assert cli._refuse_unsupported_submit(state) == cli.EXIT_UNSUPPORTED_OS
    err = capsys.readouterr().err
    assert "rocky" in err
    assert "alma-certify bundle" in err, "did not point at the offline path"


def test_submit_is_allowed_for_an_almalinux_run(tmp_path):
    state = _state(tmp_path, report_os="almalinux")

    assert cli._refuse_unsupported_submit(state) is None


def test_an_almalinux_run_uploaded_from_elsewhere_is_still_allowed(tmp_path):
    """Bundling on the machine under test and submitting from a workstation is
    ordinary. Checking the live host would block this and let the reverse through.
    """
    state = _state(tmp_path, report_os="almalinux", meta={"host_os_id": "fedora"})

    assert cli._refuse_unsupported_submit(state) is None


def test_a_run_with_no_report_falls_back_to_its_recorded_os(tmp_path, capsys):
    """An interrupted run has a state directory and no report yet."""
    state = _state(tmp_path, meta={"host_os_id": "rocky",
                                   "host_os_display": "Rocky Linux 9.6"})

    assert cli._refuse_unsupported_submit(state) == cli.EXIT_UNSUPPORTED_OS
    assert "Rocky Linux 9.6" in capsys.readouterr().err


def test_a_run_with_neither_is_refused(tmp_path, capsys):
    """Unknown is not AlmaLinux."""
    state = _state(tmp_path, meta={})

    assert cli._refuse_unsupported_submit(state) == cli.EXIT_UNSUPPORTED_OS


# --- the CLI wiring -----------------------------------------------------------


@pytest.mark.parametrize("command", ["collect", "validate", "benchmark", "run"])
def test_every_run_command_accepts_the_flag(command):
    args = cli.build_parser().parse_args([command, "--allow-unsupported-os"])

    assert args.allow_unsupported_os is True


@pytest.mark.parametrize("command", ["collect", "validate", "benchmark", "run"])
def test_every_run_command_is_gated(command, monkeypatch):
    """A command that skipped the guard would run and then quietly try to upload.
    """
    seen = []
    monkeypatch.setattr(cli, "_guard_supported_os",
                        lambda args, hooks=None: seen.append(args) or False)

    code = cli.main([command])

    assert code == cli.EXIT_UNSUPPORTED_OS, f"{command} ran anyway"
    assert len(seen) == 1


def test_the_exit_code_is_distinct():
    """A script needs to tell "blocked before starting" from "the hardware
    failed"."""
    assert cli.EXIT_UNSUPPORTED_OS not in (
        cli.EXIT_OK, cli.EXIT_FAILURES, cli.EXIT_ERROR, cli.EXIT_USAGE,
    )


def test_a_benchmark_dry_run_is_not_gated(monkeypatch):
    """It only prints the plan. Nothing executes, so there is nothing to warn
    about."""
    monkeypatch.setattr(cli, "_guard_supported_os",
                        lambda args, hooks=None: pytest.fail("gated a dry run"))

    assert cli.main(["benchmark", "--dry-run"]) == cli.EXIT_OK


# --- one os-release parser -----------------------------------------------------
#
# There were four, and they disagreed about quotes. ``hostos`` stripped both styles;
# the copies in report.py, inventory/osinfo.py, and pkg.py stripped only double
# quotes. That mattered because report.py's output *is* ``environment.os.id`` - the
# field the submit gate reads (``hostos.report_os_id``) and the one Lumina quarantines
# on. So a spec-legal single-quoted os-release meant:
#
#   run start  -> hostos.detect() strips the quotes, the run is AlmaLinux, proceeds
#   report     -> records "'almalinux'" with the quotes still attached
#   submit     -> report_is_almalinux() is False, so a genuine AlmaLinux run is refused
#
# Quoting is optional in os-release(5) and both forms are legal, so this was reachable
# on any hand-edited or custom-built image.


SINGLE_QUOTED = "NAME='AlmaLinux'\nID='almalinux'\nVERSION_ID='8.10'\n"


@pytest.fixture
def _single_quoted_os_release(monkeypatch):
    """Every parser reads /etc/os-release through procutil.read_file."""
    from alma_certify import procutil

    real = procutil.read_file
    monkeypatch.setattr(
        procutil, "read_file",
        lambda path, default=None: (
            SINGLE_QUOTED if "os-release" in str(path) else real(path, default)
        ),
    )


def test_the_report_parser_strips_single_quotes(_single_quoted_os_release):
    """The copy that matters most: its output is what gates submission."""
    from alma_certify import report

    assert report._parse_os_release().get("ID") == "almalinux"


def test_a_single_quoted_run_is_still_submittable(_single_quoted_os_release):
    """End to end on the field that decides it."""
    from alma_certify import report

    environment = report.capture_environment([])

    assert environment["os"]["id"] == "almalinux"
    assert hostos.report_is_almalinux({"environment": environment}) is True


def test_the_inventory_parser_strips_single_quotes(tmp_path, _single_quoted_os_release):
    from alma_certify.inventory import osinfo

    assert osinfo.collect(str(tmp_path))["id"] == "almalinux"


def test_os_major_survives_single_quotes(_single_quoted_os_release):
    """A None here picks el9's ``crb`` over el8's ``powertools``, so package setup
    fails on exactly the release that needs the older name."""
    from alma_certify.pkg import PackageManager

    assert PackageManager().os_major() == 8


def test_every_os_release_reader_goes_through_hostos():
    """Structural: four parsers is how they drifted. Only hostos may parse.

    Asserted on the source because the point is that no second implementation
    exists, which no behavioral test can show.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "alma_certify"
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "hostos.py":
            continue
        text = path.read_text()
        if 'strip(\'"\')' in text and "os-release" in text:
            offenders.append(str(path.relative_to(root)))

    assert offenders == [], f"a second os-release parser reappeared in {offenders}"


# --- the other way into a run ---------------------------------------------------
#
# Every CLI run passes the unsupported-OS gate on its way in. The guided interface went a different
# way - ``prepare_run`` - and that way had no gate at all, so a resident run on a RHEL rebuild
# simply started: no warning, no question, and nobody told that nothing it produced could be
# submitted. The check lives in prepare_run now, and it is asked through the run's hooks because a
# resident run has no stderr to print to and no stdin to read.


class _Driver(cli.RunHooks):
    def __init__(self, answer):
        self.answer = answer
        self.asked = []
        self.notes = []

    def note(self, text=""):
        self.notes.append(text)

    def confirm(self, question, *, yes_label="y", no_label="n", default=False):
        self.asked.append(question)
        return self.answer


def _rebuild(monkeypatch):
    monkeypatch.setattr(
        cli.hostos, "detect",
        lambda *a, **k: cli.hostos.HostOS("rocky", "10.1", "Rocky Linux 10.1"),
    )


def test_the_guided_path_asks_before_starting_a_run_on_a_rebuild(monkeypatch, tmp_path):
    _rebuild(monkeypatch)
    driver = _Driver(answer=False)

    with pytest.raises(cli.HostNotSupported):
        cli.prepare_run(["collect", "--run-dir", str(tmp_path)], driver)

    assert driver.asked, "it must ask rather than just starting"
    assert any("unsupported operating system" in note.lower() for note in driver.notes)
    assert any("cannot be submitted" in note for note in driver.notes), (
        "what it costs has to be readable next to the question"
    )
    assert list(tmp_path.iterdir()) == [], "declining must leave no half-created run behind"


def test_saying_yes_on_a_rebuild_starts_the_run(monkeypatch, tmp_path):
    _rebuild(monkeypatch)
    driver = _Driver(answer=True)

    state, _, run_types = cli.prepare_run(["collect", "--run-dir", str(tmp_path)], driver)

    assert run_types == ["collect"]
    assert state.run_dir


def test_the_flag_answers_it_there_too(monkeypatch, tmp_path):
    _rebuild(monkeypatch)
    driver = _Driver(answer=False)

    cli.prepare_run(["collect", "--allow-unsupported-os", "--run-dir", str(tmp_path)], driver)

    assert driver.asked == [], "the flag is the answer; asking anyway would be asking twice"


def test_almalinux_is_not_asked_anything(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli.hostos, "detect",
        lambda *a, **k: cli.hostos.HostOS("almalinux", "10.1", "AlmaLinux 10.1"),
    )
    driver = _Driver(answer=False)

    cli.prepare_run(["collect", "--run-dir", str(tmp_path)], driver)

    assert driver.asked == []
    assert driver.notes == []
