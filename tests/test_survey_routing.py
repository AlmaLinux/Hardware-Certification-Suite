"""A collect-only run (survey) submits to the survey endpoint; validate/benchmark do not."""
from __future__ import annotations

from test_runs_listing import make_run

from alma_certify import cli
from alma_certify.state import RunState
from alma_certify.submit import client


def _state_and_config(tmp_path, run_id, run_types):
    make_run(
        tmp_path, run_id, run_types=run_types,
        results={} if run_types == ("collect",) else {"a": "pass"},
    )
    state = RunState.find(str(tmp_path), run_id)
    config = cli.Config.load(None)
    config.set("general", "server", "https://lumina.example.org")
    config.set("general", "run_dir", str(tmp_path))
    return state, config


def _patch_clients(monkeypatch):
    calls = []
    monkeypatch.setattr(client, "submit_survey", lambda **kw: calls.append("survey") or 0)
    monkeypatch.setattr(client, "submit_run", lambda **kw: calls.append("run") or 0)
    return calls


def test_collect_only_run_routes_to_the_survey_endpoint(tmp_path, monkeypatch):
    calls = _patch_clients(monkeypatch)
    state, config = _state_and_config(
        tmp_path, "aaaaaaaa-1111-4111-8111-111111111111", ("collect",)
    )
    cli._autosubmit(state, config, "token", cli.TerminalHooks(state.run_dir))
    assert calls == ["survey"]


def test_validate_run_still_routes_to_the_results_endpoint(tmp_path, monkeypatch):
    calls = _patch_clients(monkeypatch)
    state, config = _state_and_config(
        tmp_path, "bbbbbbbb-2222-4222-8222-222222222222", ("collect", "validate")
    )
    cli._autosubmit(state, config, "token", cli.TerminalHooks(state.run_dir))
    assert calls == ["run"]


def test_survey_is_a_runnable_alias_for_collect():
    args = cli.build_parser().parse_args(["survey"])
    assert args.func is cli.cmd_collect
