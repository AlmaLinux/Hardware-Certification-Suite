"""A whole-machine claim from inside a guest is recorded, and not uploaded.

``virt.py`` documented this gate and nothing called it: the module was never imported, so the
report carried no virtualization facts and a full validation run on a VM uploaded as if it were
bare metal. These pin the wiring, beside the OS gate it cites as precedent.
"""

import argparse
import json

import pytest
from test_runs_listing import make_run

from alma_certify import cli, virt
from alma_certify.config import Config
from alma_certify.state import RunState
from alma_certify.submit import client

KVM = {"type": "kvm", "container": None, "detector": "systemd-detect-virt", "signals": {}}
METAL = {"type": "none", "container": None, "detector": "systemd-detect-virt", "signals": {}}
PODMAN = {"type": "none", "container": "podman", "detector": "systemd-detect-virt", "signals": {}}


def _start(tmp_path, monkeypatch, facts, argv):
    monkeypatch.setattr(virt, "detect", lambda: facts)
    monkeypatch.setattr(cli.hostos, "detect",
                        lambda *a, **k: cli.hostos.HostOS("almalinux", "10.1", "AlmaLinux 10.1"))
    args = cli.build_parser().parse_args(argv + ["--run-dir", str(tmp_path)])
    config = Config.load("/nonexistent")
    config.set("general", "run_dir", str(tmp_path))
    return cli._start_run(args, config, ["collect", "validate"])


def test_the_facts_are_recorded_on_the_run(tmp_path, monkeypatch):
    state = _start(tmp_path, monkeypatch, KVM, ["validate"])
    assert state.meta["virtualization"] == KVM


def test_a_whole_machine_run_in_a_guest_is_kept_local(tmp_path, monkeypatch):
    state = _start(tmp_path, monkeypatch, KVM, ["validate"])
    assert state.meta["submit"] is False
    assert state.meta["no_submit_reason"] == "virtual_machine"


def test_a_container_counts_as_a_guest(tmp_path, monkeypatch):
    state = _start(tmp_path, monkeypatch, PODMAN, ["validate"])
    assert state.meta["no_submit_reason"] == "virtual_machine"


def test_a_scoped_gpu_claim_from_a_guest_still_submits(tmp_path, monkeypatch):
    """The one claim a guest can make: a passed-through card is the real device."""
    state = _start(tmp_path, monkeypatch, KVM, ["validate", "--scope", "gpu"])
    assert state.meta["submit"] is True
    assert "no_submit_reason" not in state.meta


def test_bare_metal_is_untouched(tmp_path, monkeypatch):
    state = _start(tmp_path, monkeypatch, METAL, ["validate"])
    assert state.meta["submit"] is True


def test_no_submit_still_says_so_rather_than_blaming_the_vm(tmp_path, monkeypatch):
    """The reason is stored separately so the end-of-run message names the right cause."""
    state = _start(tmp_path, monkeypatch, KVM, ["validate", "--no-submit"])
    assert state.meta["submit"] is False
    assert "no_submit_reason" not in state.meta


def test_the_report_carries_the_facts(monkeypatch):
    monkeypatch.setattr(virt, "detect", lambda: KVM)
    from alma_certify import report
    env = report.capture_environment(installed_packages=[], enabled_repos=[])
    assert env["virtualization"] == KVM


def test_submit_refuses_a_whole_machine_claim_by_the_reports_own_facts(tmp_path, capsys):
    """Judged from the report, as the OS refusal is: a bundle moved to a laptop is judged by where
    it ran, not by the laptop."""
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(json.dumps({
        "run": {"claim_scope": []},
        "environment": {"virtualization": KVM},
    }))
    state = argparse.Namespace(run_dir=str(run_dir), run_id="deadbeef-0000", meta={})

    code = cli._refuse_virtual_submit(state)

    assert code == cli.EXIT_UNSUPPORTED_OS
    assert "virtual machine" in capsys.readouterr().err


def test_submit_allows_a_scoped_claim_from_a_guest(tmp_path):
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(json.dumps({
        "run": {"claim_scope": ["gpu"]},
        "environment": {"virtualization": KVM},
    }))
    state = argparse.Namespace(run_dir=str(run_dir), run_id="deadbeef-0000", meta={})
    assert cli._refuse_virtual_submit(state) is None


def test_submit_of_an_old_report_without_the_facts_is_not_refused(tmp_path):
    """Reports from before this field exist and must keep uploading."""
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    (run_dir / "report.json").write_text(
        json.dumps({"run": {"claim_scope": []}, "environment": {}}))
    state = argparse.Namespace(run_dir=str(run_dir), run_id="deadbeef-0000", meta={})
    assert cli._refuse_virtual_submit(state) is None


def test_the_module_is_actually_imported_now():
    """The regression that started this: a documented gate with no caller."""
    import inspect
    assert "virt.detect()" in inspect.getsource(cli._start_run)
    from alma_certify import report
    assert "virt.detect()" in inspect.getsource(report.capture_environment)


@pytest.mark.parametrize("facts,expect", [(KVM, True), (PODMAN, True), (METAL, False),
                                          ({"type": "powervm", "container": None}, False)])
def test_the_policy_table_decides(facts, expect):
    assert (virt.full_run_reason(facts) is not None) is expect


# --- through the commands, not just the helpers ------------------------------------------

RUN_ID = "cccccccc-3333-4333-8333-333333333333"


def _stored_run(tmp_path, monkeypatch):
    make_run(tmp_path, RUN_ID, run_types=("collect", "validate"), results={"a": "pass"})
    (tmp_path / RUN_ID / "report.json").write_text(json.dumps({
        "run": {"claim_scope": []},
        "environment": {"os": {"id": "almalinux", "version_id": "10.1"}, "virtualization": KVM},
    }))
    monkeypatch.setattr(client, "submit_run",
                        lambda **kw: pytest.fail("uploaded a whole-machine claim from a guest"))
    monkeypatch.setattr(client, "submit_survey", lambda **kw: pytest.fail("uploaded as a survey"))


def test_the_submit_command_refuses_it(tmp_path, monkeypatch, capsys):
    _stored_run(tmp_path, monkeypatch)

    code = cli.main(["submit", RUN_ID, "--run-dir", str(tmp_path), "--server", "https://x",
                     "--token", "tok"])

    assert code == cli.EXIT_UNSUPPORTED_OS
    err = capsys.readouterr().err
    assert "virtual machine" in err
    assert "only AlmaLinux" not in err, "refused for the wrong reason"


def test_the_end_of_run_message_names_the_machine_not_a_flag(tmp_path, monkeypatch, capsys):
    """Falling through to the ``--no-submit`` wording would say the operator asked for this."""
    _stored_run(tmp_path, monkeypatch)
    state = RunState.find(str(tmp_path), RUN_ID)
    state.meta.update(submit=False, no_submit_reason="virtual_machine", virtualization=KVM)
    config = Config.load(None)
    config.set("general", "server", "https://x")

    cli._autosubmit(state, config, "tok", cli.TerminalHooks(state.run_dir))

    out = capsys.readouterr().out
    assert "not submitted: %s." % virt.full_run_reason(KVM) in out
    assert "--no-submit" not in out
    assert "alma-certify bundle" in out
    assert "alma-certify submit" not in out, "there is nothing to retry"
