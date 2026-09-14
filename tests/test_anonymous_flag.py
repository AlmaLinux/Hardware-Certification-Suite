"""``--anonymous``: contribute a run without your username published against it.

Testing hardware and being publicly credited for it are separate decisions, and somebody
validating a customer's machine or their own home lab previously had to choose between
contributing and being named. The flag carries that answer from the command line to the
server; Lumina decides what it means and can still be told account-wide instead.

The one rule worth pinning is that the flag is *tri-state on the wire*. Not passing it is
not the same as passing false: only an absent field lets the submitter's account-wide
setting decide, so a client that helpfully sent ``anonymous=false`` on every upload would
quietly override that preference for every run from that machine.
"""
import argparse

from alma_certify.planning import Plan


def _plan(what="validate", **kw):
    plan = Plan(what)
    for key, value in kw.items():
        setattr(plan, key, value)
    return plan


def test_the_flag_reaches_the_command():
    assert "--anonymous" in _plan(anonymous=True).argv()


def test_it_is_absent_unless_asked_for():
    assert "--anonymous" not in _plan().argv()


def test_a_survey_never_offers_it():
    # A survey publishes aggregates and names no submitter at any point, so there is
    # nothing there to be anonymous about - and an argv saying otherwise would imply
    # the census attributes submissions.
    assert "--anonymous" not in _plan("collect", anonymous=True).argv()


def test_it_survives_switching_run_type():
    # Unlike scope and the embargo, which ``set_what`` clears: this answer is about the
    # person, not about what the run does, so backing out and picking benchmark keeps it.
    plan = _plan(anonymous=True)
    plan.set_what("benchmark")

    assert plan.anonymous
    assert "--anonymous" in plan.argv()


def test_every_run_subcommand_accepts_it():
    from alma_certify.cli import build_parser

    for command in ("validate", "benchmark", "run", "collect", "survey"):
        args = build_parser().parse_args([command, "--anonymous"])
        assert args.anonymous is True, command


def test_it_is_recorded_on_the_run_so_a_later_submit_still_knows(tmp_path):
    # A run stored now and uploaded next week has to carry the answer with it; the flag is
    # not on the command line any more by then, so ``alma-certify submit`` reads it back off
    # the run rather than asking again.
    import configparser

    from alma_certify import config as config_mod
    from alma_certify.cli import _start_run
    from alma_certify.config import Config

    parser = configparser.ConfigParser()
    for section, values in config_mod.DEFAULTS.items():
        parser[section] = dict(values)
    parser["general"]["run_dir"] = str(tmp_path)
    config = Config(parser)
    args = argparse.Namespace(anonymous=True, run_dir=str(tmp_path))
    state = _start_run(args, config, ["collect", "validate"])

    assert state.meta["anonymous"] is True


# --- what actually goes over the wire --------------------------------------------

def _submitted_fields(monkeypatch, tmp_path, *, meta_anonymous):
    """The multipart fields ``submit_run`` would post for a run with this meta."""
    import json

    from test_runs_listing import make_run

    from alma_certify import cli
    from alma_certify.state import RunState
    from alma_certify.submit import client

    run_id = "bbbbbbbb-2222-4222-8222-222222222222"
    run_dir = make_run(tmp_path, run_id, run_types=("collect", "validate"),
                       results={"a": "pass"})
    state_path = run_dir / "state.json"
    state_data = json.loads(state_path.read_text())
    state_data["meta"]["anonymous"] = meta_anonymous
    state_path.write_text(json.dumps(state_data))

    captured = {}
    monkeypatch.setattr(client, "submit_run",
                        lambda **kw: captured.update(kw) or 0)
    config = cli.Config.load(None)
    config.set("general", "server", "https://lumina.example.org")
    config.set("general", "run_dir", str(tmp_path))
    state = RunState.find(str(tmp_path), run_id)
    cli._autosubmit(state, config, "token", cli.TerminalHooks(state.run_dir))
    return captured


def test_an_anonymous_run_tells_the_server_so(monkeypatch, tmp_path):
    assert _submitted_fields(monkeypatch, tmp_path, meta_anonymous=True)["anonymous"] is True


def test_an_ordinary_run_sends_no_instruction_at_all(monkeypatch, tmp_path):
    # Not False: an absent field is what lets the account-wide setting decide. Sending
    # a false here would override that preference on every upload from this machine.
    assert _submitted_fields(monkeypatch, tmp_path, meta_anonymous=False)["anonymous"] is None
