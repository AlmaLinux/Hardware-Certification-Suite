"""The Textual interface, driven headlessly through its own pilot.

These need the vendored Textual stack, so the module import-skips when it is absent (a base-only
checkout). They cover what the planner tests cannot: that the buttons and form controls reach the
command the planner would build, that "Start in plain terminal" hands that command off, and that
"Start run" hosts it resident (streaming a run's hooks into the screen). The command-building itself
is pinned in test_planning.py; the resident tests use a fake run rather than the real engine.

The viewport is deliberately tall so every control is on screen and the pilot's coordinate clicks
land cleanly; a real terminal scrolls. Async tests without a plugin dependency: each drives
``App.run_test`` under ``asyncio.run``.
"""

import asyncio
import threading
import time

import pytest

alma_certify_tui = pytest.importorskip("alma_certify_tui")
from textual.widgets import (  # noqa: E402 - needs the guard
    Button,
    Input,
    ProgressBar,
    RichLog,
    SelectionList,
    Static,
)

from alma_certify_tui.app import (  # noqa: E402 - after the guard
    _App,
    _AuthScreen,
    _ConfirmScreen,
    _RunScreen,
)

SIZE = (120, 80)


def _app(**over):
    kwargs = dict(
        runs=lambda: [],
        report_lines=lambda run_id: ["report for " + run_id],
        version="0.1.0",
        host="AlmaLinux 10.1",
        carried=[],
    )
    kwargs.update(over)
    return _App(**kwargs)


def _run(coro_factory):
    async def driver():
        app = coro_factory.app = _app(**getattr(coro_factory, "kwargs", {}))
        async with app.run_test(size=SIZE) as pilot:
            await coro_factory(pilot, app)
        return app.return_value
    return asyncio.run(driver())


def drive(**kwargs):
    """Turn a ``(pilot, app)`` coroutine into a callable that returns the app's exit value."""
    def wrap(fn):
        fn.kwargs = kwargs
        return fn
    return wrap


def _log_text(screen) -> str:
    log = screen.query_one("#run-log", RichLog)
    return "\n".join("".join(seg.text for seg in line) for line in log.lines)


async def _wait_for_code(pilot, app, tries=60):
    for _ in range(tries):
        await pilot.pause()
        if getattr(app.screen, "_code", None) is not None:
            return
    raise AssertionError("the resident run never finished")


# --- reaching the interface ------------------------------------------------------


def test_unavailable_reason_names_a_terminal_it_cannot_draw_on(monkeypatch):
    monkeypatch.setenv("TERM", "")
    assert "TERM is not set" in (alma_certify_tui.unavailable_reason() or "")
    monkeypatch.setenv("TERM", "dumb")
    assert "dumb" in (alma_certify_tui.unavailable_reason() or "")
    monkeypatch.setenv("TERM", "xterm-256color")
    assert alma_certify_tui.unavailable_reason() is None


def test_the_main_menu_is_buttons():
    """The first screen uses buttons, not a list. Pinned because it regressed to a list once."""
    @drive()
    async def go(pilot, app):
        go.kinds = [type(app.screen.query_one("#menu-%s" % v)).__name__
                    for v in ("validate", "benchmark", "run", "runs", "quit")]

    _run(go)
    assert go.kinds == ["Button"] * 5


def test_quitting_the_main_menu_asks_for_nothing():
    @drive()
    async def go(pilot, app):
        await pilot.click("#menu-quit")
        await pilot.pause()

    assert _run(go) is None


# --- the form reaches the command, handed off to plain output --------------------


def test_handing_off_returns_the_built_command():
    @drive()
    async def go(pilot, app):
        await pilot.click("#menu-validate")
        await pilot.pause()
        await pilot.click("#start-plain")   # run in plain terminal: hand the argv back
        await pilot.pause()

    assert _run(go) == ["validate"]


def test_a_checkbox_reaches_the_command():
    @drive()
    async def go(pilot, app):
        await pilot.click("#menu-validate")
        await pilot.pause()
        await pilot.click("#submit")        # untick "Upload when finished"
        await pilot.pause()
        await pilot.click("#start-plain")
        await pilot.pause()

    assert _run(go) == ["validate", "--no-submit"]


def test_a_radio_reaches_the_command():
    @drive()
    async def go(pilot, app):
        await pilot.click("#menu-validate")
        await pilot.pause()
        await pilot.click("#scope-gpu")     # "only the GPU"
        await pilot.pause()
        await pilot.click("#start-plain")
        await pilot.pause()

    assert _run(go) == ["validate", "--scope", "gpu"]


def test_the_category_checklist_reaches_the_command():
    @drive()
    async def go(pilot, app):
        await pilot.click("#menu-validate")
        await pilot.pause()
        await pilot.click("#coverage-some")   # reveal the checklist
        await pilot.pause()
        cats = app.screen.query_one("#categories", SelectionList)
        cats.select(cats.get_option_at_index(0))
        go.first = cats.get_option_at_index(0).value
        await pilot.pause()
        await pilot.click("#start-plain")
        await pilot.pause()

    assert _run(go) == ["validate", "--category", go.first]


def test_the_category_checklist_is_hidden_until_you_choose_to_pick():
    """Shrinks the form: the common case (run everything) shows two words, not a long checklist."""
    @drive()
    async def go(pilot, app):
        await pilot.click("#menu-validate")
        await pilot.pause()
        go.before = app.screen.query_one("#categories").display
        await pilot.click("#coverage-some")
        await pilot.pause()
        go.after = app.screen.query_one("#categories").display
        await pilot.click("#back")
        await pilot.pause()

    _run(go)
    assert go.before is False, "the checklist should be collapsed by default (run all)"
    assert go.after is True, "choosing to pick categories should reveal it"


def test_a_subset_run_is_flagged_as_supporting_evidence():
    @drive()
    async def go(pilot, app):
        await pilot.click("#menu-validate")
        await pilot.pause()
        go.full = app.screen.query_one("#cert-note").display   # whole machine, every category
        await pilot.click("#coverage-some")
        await pilot.pause()
        cats = app.screen.query_one("#categories", SelectionList)
        cats.select(cats.get_option_at_index(0))
        await pilot.pause()
        go.partial = app.screen.query_one("#cert-note").display
        go.text = str(app.screen.query_one("#cert-note").render())
        await pilot.click("#back")
        await pilot.pause()

    _run(go)
    assert go.full is False, "a whole-machine run needs no supporting-evidence caveat"
    assert go.partial is True, "picking a subset should show the caveat"
    assert "supporting evidence" in go.text


def test_scoping_to_one_device_hides_the_picker_and_flags_supporting_evidence():
    @drive()
    async def go(pilot, app):
        await pilot.click("#menu-validate")
        await pilot.pause()
        await pilot.click("#scope-gpu")
        await pilot.pause()
        go.note = app.screen.query_one("#cert-note").display
        go.checklist = app.screen.query_one("#categories").display
        go.coverage = app.screen.query_one("#coverage").display
        await pilot.click("#back")
        await pilot.pause()

    _run(go)
    assert go.note is True, "a scoped run is supporting evidence and should say so"
    assert go.checklist is False, "the checklist is hidden when the scope decides categories"
    assert go.coverage is False, "the run-all / choose-specific selector is hidden when scoped"


def test_the_embargo_date_is_hidden_until_the_hardware_is_marked_unreleased():
    @drive()
    async def go(pilot, app):
        await pilot.click("#menu-validate")
        await pilot.pause()
        go.before = app.screen.query_one("#embargo").display
        await pilot.click("#pre_release")
        await pilot.pause()
        go.after = app.screen.query_one("#embargo").display
        await pilot.click("#back")
        await pilot.pause()

    _run(go)
    assert go.before is False
    assert go.after is True


def test_an_embargo_date_becomes_publish_after():
    @drive()
    async def go(pilot, app):
        await pilot.click("#menu-validate")
        await pilot.pause()
        await pilot.click("#pre_release")
        await pilot.pause()
        app.screen.query_one("#publish_after", Input).value = "2027-03-01"
        await pilot.pause()
        await pilot.click("#start-plain")
        await pilot.pause()

    assert _run(go) == ["validate", "--pre-release", "--publish-after", "2027-03-01"]


def test_a_malformed_embargo_date_is_not_put_in_the_command():
    @drive()
    async def go(pilot, app):
        await pilot.click("#menu-validate")
        await pilot.pause()
        await pilot.click("#pre_release")
        await pilot.pause()
        app.screen.query_one("#publish_after", Input).value = "2027-3"
        await pilot.pause()
        await pilot.click("#start-plain")
        await pilot.pause()

    assert _run(go) == ["validate", "--pre-release"]


def test_backing_out_of_the_hub_returns_to_the_menu_not_the_program():
    @drive()
    async def go(pilot, app):
        await pilot.click("#menu-validate")
        await pilot.pause()
        await pilot.click("#back")
        await pilot.pause()
        await pilot.click("#menu-quit")
        await pilot.pause()

    assert _run(go) is None


# --- the resident run --------------------------------------------------------------


def _fake_run(hooks):
    """A stand-in for cli.execute: exercises every hook, then returns EXIT_OK."""
    hooks.log("collecting hardware inventory")
    hooks.progress(test_id="validate.cpu.flags", index=1, total=1, status=None)
    hooks.progress(test_id="validate.cpu.flags", index=1, total=1, status="pass")
    hooks.out("== summary ==")
    return 0


def test_a_resident_run_streams_output_and_reports_its_outcome():
    @drive()
    async def go(pilot, app):
        app.build_run = lambda argv: _fake_run
        await pilot.click("#menu-validate")
        await pilot.pause()
        await pilot.click("#start")          # host the run in the interface
        await _wait_for_code(pilot, app)
        go.code = app.screen._code
        go.text = _log_text(app.screen)
        await pilot.click("#quit")           # Quit exits with the run's code
        await pilot.pause()

    result = _run(go)
    assert go.code == 0
    assert "collecting hardware inventory" in go.text
    assert "== summary ==" in go.text
    assert result == 0


def test_authorization_shows_a_qr_then_takes_it_down():
    """When the run needs authorization, the QR/URL/code appear as a screen and are dismissed when
    polling ends. The fake run mimics auth.register calling authorize then authorized."""
    shown = threading.Event()
    proceed = threading.Event()

    def auth_run(hooks):
        hooks.authorize(
            verification_uri="https://lumina.example/my/activate/",
            user_code="WXYZ-7788",
            complete_uri="https://lumina.example/my/activate/?code=WXYZ-7788",
        )
        shown.set()
        proceed.wait(10)
        hooks.authorized()
        hooks.log("token acquired; proceeding")
        return 0

    @drive()
    async def go(pilot, app):
        app.build_run = lambda argv: auth_run
        await pilot.click("#menu-validate")
        await pilot.pause()
        await pilot.click("#start")
        for _ in range(60):
            await pilot.pause()
            if shown.is_set():
                break
        await pilot.pause()
        go.during = isinstance(app.screen, _AuthScreen)
        # Every path must be offered: the QR, and - so it works even where the QR does not - the
        # URL and the code as readable text.
        go.has_qr = bool(app.screen.query("#qr"))
        go.url_text = str(app.screen.query_one("#auth-url", Static).render())
        go.code_text = str(app.screen.query_one("#auth-code", Static).render())
        proceed.set()
        await _wait_for_code(pilot, app)
        go.after = isinstance(app.screen, _RunScreen)
        await pilot.click("#quit")
        await pilot.pause()

    _run(go)
    assert go.during is True, "the authorization screen should be showing during the flow"
    assert go.has_qr is True
    assert "https://lumina.example/my/activate/" in go.url_text, "the URL must be shown as text"
    assert "WXYZ-7788" in go.code_text, "the code must be shown as text"
    assert go.after is True, "it should return to the run screen once authorization ends"


def test_the_auth_screen_is_built_on_the_ui_thread(monkeypatch):
    """Regression: the auth screen must be constructed on the app's own thread, never the run's
    worker thread. A Textual widget builds an asyncio.Lock in its constructor, and on Python 3.9
    that binds the loop - off the loop thread it raises "no current event loop" (hit on EL9). This
    pins the construction thread on any interpreter, since 3.10+ would not raise to reveal it.
    """
    built = {}
    real_init = _AuthScreen.__init__

    def recording_init(self, *a, **k):
        built["thread"] = threading.get_ident()
        real_init(self, *a, **k)

    monkeypatch.setattr(_AuthScreen, "__init__", recording_init)

    shown = threading.Event()
    proceed = threading.Event()

    def auth_run(hooks):
        hooks.authorize(
            verification_uri="https://lumina.example/my/activate/",
            user_code="WXYZ-7788",
            complete_uri="https://lumina.example/my/activate/?code=WXYZ-7788",
        )
        shown.set()
        proceed.wait(10)
        hooks.authorized()
        return 0

    @drive()
    async def go(pilot, app):
        app.build_run = lambda argv: auth_run
        go.ui_thread = app._thread_id
        await pilot.click("#menu-validate")
        await pilot.pause()
        await pilot.click("#start")
        for _ in range(60):
            await pilot.pause()
            if shown.is_set():
                break
        proceed.set()
        await _wait_for_code(pilot, app)
        await pilot.click("#quit")
        await pilot.pause()

    _run(go)
    assert built.get("thread") == go.ui_thread, (
        "the auth screen was built off the app thread; this crashes on Python 3.9 (EL9)"
    )


def test_a_status_line_shows_the_running_step_and_counts_up():
    """A long step reports what it is doing and ticks an elapsed counter, so a blocking call (the
    15s logind reload that prompted this) is visibly alive on the status line, not frozen."""
    proceed = threading.Event()

    def slow_run(hooks):
        hooks.status("keeping the machine awake for the run")
        proceed.wait(10)
        return 0

    @drive()
    async def go(pilot, app):
        app.build_run = lambda argv: slow_run
        await pilot.click("#menu-validate")
        await pilot.pause()
        await pilot.click("#start")
        for _ in range(60):
            await pilot.pause()
            if "keeping the machine awake" in str(app.screen.query_one("#run-status").render()):
                break
        go.msg = str(app.screen.query_one("#run-status").render())
        # Pretend the step has been running a while, then tick: the elapsed seconds must show.
        app.screen._activity_since -= 5
        app.screen._tick_activity()
        go.ticked = str(app.screen.query_one("#run-status").render())
        proceed.set()
        await _wait_for_code(pilot, app)
        await pilot.click("#quit")
        await pilot.pause()

    _run(go)
    assert "keeping the machine awake for the run" in go.msg
    assert "5s" in go.ticked, "a step that has run a while should show elapsed seconds"


def test_cancel_asks_the_run_to_stop():
    started = threading.Event()
    observed = {}

    def blocking_run(hooks):
        started.set()
        for _ in range(300):
            if hooks.should_stop():
                observed["stopped"] = True
                return 0
            time.sleep(0.01)
        observed["stopped"] = False
        return 0

    @drive()
    async def go(pilot, app):
        app.build_run = lambda argv: blocking_run
        await pilot.click("#menu-validate")
        await pilot.pause()
        await pilot.click("#start")
        for _ in range(50):
            await pilot.pause()
            if started.is_set():
                break
        await pilot.click("#cancel")         # ask the run to stop
        await _wait_for_code(pilot, app)
        await pilot.click("#quit")
        await pilot.pause()

    _run(go)
    assert observed.get("stopped") is True


# --- the previous-runs browser (hands off) -----------------------------------------


_RUN_ROW = {
    "short_id": "9f3c1a2e", "summary": "2 pass, 1 fail",
    "finished": False, "reported": True, "submission": None,
}


def test_a_finished_run_can_be_resumed():
    @drive(runs=lambda: [dict(_RUN_ROW)])
    async def go(pilot, app):
        await pilot.click("#menu-runs")
        await pilot.pause()
        await pilot.press("enter")           # the one run
        await pilot.pause()
        await pilot.press("down", "enter")   # Resume it
        await pilot.pause()

    assert _run(go) == ["resume", "9f3c1a2e"]


def test_an_already_submitted_run_is_not_offered_upload_again():
    row = dict(_RUN_ROW, finished=True, submission={"at": "2026-08-16T19:06:12Z"})

    @drive(runs=lambda: [row])
    async def go(pilot, app):
        await pilot.click("#menu-runs")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("down", "down", "enter")   # Upload it -> refused, stays open
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()

    assert _run(go) is None


def test_pressing_c_copies_the_activation_code():
    """A running Textual app captures the mouse, so the terminal's own copy cannot reach the code;
    ``c`` lifts it out via the clipboard (OSC 52) instead."""
    @drive()
    async def go(pilot, app):
        await app.push_screen(
            _AuthScreen(
                "https://lumina.example/my/activate/",
                "WXYZ-7788",
                "https://lumina.example/my/activate/?code=WXYZ-7788",
            )
        )
        await pilot.pause()
        go.is_auth = isinstance(app.screen, _AuthScreen)
        await pilot.press("c")
        await pilot.pause()
        go.clip = app.clipboard
        go.hint = str(app.screen.query_one("#auth-copy-hint", Static).render())

    _run(go)
    assert go.is_auth is True
    assert go.clip == "WXYZ-7788", "pressing c must copy the code to the clipboard"
    assert "copied" in go.hint


def test_the_menu_offers_a_survey_and_hides_the_test_run_settings():
    """A survey collects inventory and runs nothing, so the form must not offer a scope,
    a category picker, or the GPU-driver step - and the command is a bare `collect`."""
    @drive()
    async def go(pilot, app):
        await pilot.click("#menu-collect")
        await pilot.pause()
        screen = app.screen
        go.scope = screen.query_one("#scope").display
        go.categories = screen.query_one("#categories").display
        go.gpu = screen.query_one("#gpu").display
        go.note = screen.query_one("#cert-note").display
        await pilot.click("#start-plain")
        await pilot.pause()

    assert _run(go) == ["collect"]
    assert go.scope is False, "a survey certifies nothing, so there is no scope to set"
    assert go.categories is False, "a survey runs no tests, so there are no categories"
    assert go.gpu is False, "a survey never reaches the GPU checks"
    assert go.note is True, "it should say what a survey is instead"


def test_the_anonymous_option_reaches_the_command():
    """Ticking it adds --anonymous; the run is otherwise unchanged."""
    @drive()
    async def go(pilot, app):
        await pilot.click("#menu-validate")
        await pilot.pause()
        await pilot.click("#anonymous")
        await pilot.pause()
        await pilot.click("#start-plain")
        await pilot.pause()

    assert _run(go) == ["validate", "--anonymous"]


def test_a_survey_is_not_offered_the_anonymous_option():
    """A survey publishes aggregates and names no submitter, so the question would be
    an offer that changes nothing - and would imply the census attributes submissions."""
    @drive()
    async def go(pilot, app):
        await pilot.click("#menu-collect")
        await pilot.pause()
        go.shown = app.screen.query_one("#anonymous").display

    _run(go)
    assert go.shown is False


# --- the header stops claiming the run is going ----------------------------------


def _survey_run(hooks):
    """A collect run: it reports steps but runs no tests, so it never reports progress."""
    hooks.log("collecting hardware inventory")
    hooks.out("contributing inventory to the AlmaLinux hardware survey ...")
    return 0


def test_a_finished_survey_run_drops_the_indeterminate_progress_bar():
    """The reported bug. A collect run has no test plan, so ``set_progress`` is never
    called and the bar keeps its initial ``total=None``, which renders as "--%". It sat
    there under "Running:" beside "Run complete.", which is the interface saying two
    contradictory things at once.
    """
    @drive()
    async def go(pilot, app):
        app.build_run = lambda argv: _survey_run
        await pilot.click("#menu-collect")
        await pilot.pause()
        await pilot.click("#start")
        await _wait_for_code(pilot, app)
        bar = app.screen.query_one("#run-progress", ProgressBar)
        go.total = bar.total
        go.shown = bar.display
        go.command = str(app.screen.query_one("#run-command", Static).render())
        go.status = str(app.screen.query_one("#run-status", Static).render())
        await pilot.click("#quit")
        await pilot.pause()

    _run(go)
    assert go.total is None, "a survey run measures no steps, so the bar stays unmeasured"
    assert go.shown is False, "and an unmeasured bar has nothing to say about a finished run"
    assert go.command.startswith("Ran:"), go.command
    assert "Running:" not in go.command
    assert "complete" in go.status.lower()


def test_a_finished_test_run_keeps_the_bar_it_actually_measured():
    """The other half: where progress *was* measured, the full bar is true and stays."""
    @drive()
    async def go(pilot, app):
        app.build_run = lambda argv: _fake_run
        await pilot.click("#menu-validate")
        await pilot.pause()
        await pilot.click("#start")
        await _wait_for_code(pilot, app)
        bar = app.screen.query_one("#run-progress", ProgressBar)
        go.total = bar.total
        go.progress = bar.progress
        go.shown = bar.display
        go.command = str(app.screen.query_one("#run-command", Static).render())
        await pilot.click("#quit")
        await pilot.pause()

    _run(go)
    assert go.shown is True
    assert go.total == 1 and go.progress == 1, "one test, finished"
    assert go.command.startswith("Ran:")


def test_a_question_becomes_a_dialog_and_the_run_waits_for_it():
    """Reported: the interface hung on a progress bar, with a status line naming the previous step,
    on a machine with the NVIDIA drivers but no CUDA packages. The run had reached the GPU setup
    offer, which asked with ``input()`` - unreadable and unanswerable while Textual owns the
    terminal. A question is a hook now, and here it is a dialog the run blocks on.
    """
    asked = threading.Event()
    answer = {}

    def asking_run(hooks):
        hooks.status("checking whether this machine's GPU needs a driver or toolkit")
        hooks.note("  # dnf -y install cuda-toolkit")
        asked.set()
        answer["said"] = hooks.confirm("install this now?")
        hooks.log("answered: %s" % answer["said"])
        return 0

    @drive()
    async def go(pilot, app):
        app.build_run = lambda argv: asking_run
        await pilot.click("#menu-validate")
        await pilot.pause()
        await pilot.click("#start")
        for _ in range(60):
            await pilot.pause()
            if asked.is_set():
                break
        for _ in range(60):
            await pilot.pause()
            if isinstance(app.screen, _ConfirmScreen):
                break
        go.dialog = isinstance(app.screen, _ConfirmScreen)
        go.question = str(app.screen.query_one("#dialog-title", Static).render())
        go.detail = str(app.screen.query_one("#confirm-detail", Static).render())
        go.waiting = "said" not in answer, "the run must not proceed before the answer"
        # The run screen is still under the dialog, and its status line is what the tester reads as
        # "what is this waiting on".
        run_screen = next(s for s in app.screen_stack if isinstance(s, _RunScreen))
        go.status = str(run_screen.query_one("#run-status", Static).render())
        await pilot.click("#yes")
        await _wait_for_code(pilot, app)
        go.text = _log_text(app.screen)
        await pilot.click("#quit")
        await pilot.pause()

    _run(go)
    assert go.dialog is True, "the question must be put in front of the operator"
    assert "install this now?" in go.question
    assert "dnf -y install cuda-toolkit" in go.detail, (
        "what it is about to change has to be readable next to the question"
    )
    assert go.waiting[0] is True, go.waiting[1]
    assert answer["said"] is True, "the run resumes with the operator's answer"
    assert "answered: True" in go.text
    assert "GPU" in go.status, (
        "the step being waited on must be the step reported. It said \"checking your submission "
        "token\" - the previous step, because this one set no status of its own."
    )


def test_the_confirm_dialog_is_built_on_the_ui_thread(monkeypatch):
    """Same constraint as the auth screen: a Textual widget binds the running loop in __init__, so
    constructing one on the run's worker thread raises on Python 3.9 (EL9)."""
    built = {}
    real_init = _ConfirmScreen.__init__

    def recording_init(self, *a, **k):
        built["thread"] = threading.get_ident()
        real_init(self, *a, **k)

    monkeypatch.setattr(_ConfirmScreen, "__init__", recording_init)
    asked = threading.Event()

    def asking_run(hooks):
        asked.set()
        hooks.confirm("go ahead?")
        return 0

    @drive()
    async def go(pilot, app):
        app.build_run = lambda argv: asking_run
        go.ui_thread = app._thread_id
        await pilot.click("#menu-validate")
        await pilot.pause()
        await pilot.click("#start")
        for _ in range(60):
            await pilot.pause()
            if isinstance(app.screen, _ConfirmScreen):
                break
        await pilot.click("#no")
        await _wait_for_code(pilot, app)
        await pilot.click("#quit")
        await pilot.pause()

    _run(go)
    assert built["thread"] == go.ui_thread


def test_escape_takes_the_safe_answer():
    """A dialog that can be dismissed without answering must dismiss to the default, never leave
    the run blocked and never read the dismissal as consent."""
    answer = {}

    def asking_run(hooks):
        answer["said"] = hooks.confirm("install this now?", default=False)
        return 0

    @drive()
    async def go(pilot, app):
        app.build_run = lambda argv: asking_run
        await pilot.click("#menu-validate")
        await pilot.pause()
        await pilot.click("#start")
        for _ in range(60):
            await pilot.pause()
            if isinstance(app.screen, _ConfirmScreen):
                break
        await pilot.press("escape")
        await _wait_for_code(pilot, app)
        await pilot.click("#quit")
        await pilot.pause()

    _run(go)
    assert answer["said"] is False


def test_a_long_install_does_not_arrive_whole_in_the_next_dialog():
    """dnf reports several gigabytes of CUDA line by line through the same hook, and the reason for
    a question is the last few lines before it, not the whole install. The log keeps all of it."""
    answer = {}

    def noisy_run(hooks):
        hooks.status("installing what the GPU needs")
        for n in range(200):
            hooks.note("    downloading package %d" % n)
        hooks.status("loading the driver into the running kernel")
        hooks.note("the driver did not come up: no such device.")
        answer["said"] = hooks.confirm(
            "Reboot and run again for GPU coverage, or continue this run without the GPU?",
            yes_label="Reboot", no_label="continue", default=True,
        )
        return 0

    @drive()
    async def go(pilot, app):
        app.build_run = lambda argv: noisy_run
        await pilot.click("#menu-validate")
        await pilot.pause()
        await pilot.click("#start")
        for _ in range(80):
            await pilot.pause()
            if isinstance(app.screen, _ConfirmScreen):
                break
        go.detail = str(app.screen.query_one("#confirm-detail", Static).render())
        go.labels = [str(b.label) for b in app.screen.query(Button)]
        await pilot.press("escape")
        await _wait_for_code(pilot, app)
        go.text = _log_text(app.screen)
        await pilot.click("#quit")
        await pilot.pause()

    _run(go)
    assert "downloading package" not in go.detail, "the install is not the reason for this question"
    assert "the driver did not come up" in go.detail
    assert go.labels == ["Reboot", "Continue"], "y/n is the wrong question here"
    assert answer["said"] is True, "escape takes the default, and the default is the reboot"
    assert "downloading package 199" in go.text, "the log still has all of it"


def test_a_resident_run_on_a_rebuild_asks_before_it_starts(monkeypatch, tmp_path):
    """The guided path went in through ``prepare_run``, which had no unsupported-OS gate, so a
    resident run on a RHEL rebuild simply started: no warning, no question, and nobody told that
    nothing it produced could be submitted. It goes through the real build_run here, not a fake,
    because the gap was in that wiring rather than in anything a fake would stand in for.
    """
    from alma_certify import cli

    monkeypatch.setattr(
        cli.hostos, "detect",
        lambda *a, **k: cli.hostos.HostOS("rocky", "10.1", "Rocky Linux 10.1"),
    )
    started = []
    monkeypatch.setattr(
        cli, "execute", lambda *a, **k: started.append(1) or cli.EXIT_OK,
    )

    @drive(carried=["--run-dir", str(tmp_path)])
    async def go(pilot, app):
        await pilot.click("#menu-collect")
        await pilot.pause()
        await pilot.click("#start")
        for _ in range(80):
            await pilot.pause()
            if isinstance(app.screen, _ConfirmScreen):
                break
        go.asked = isinstance(app.screen, _ConfirmScreen)
        go.detail = str(app.screen.query_one("#confirm-detail", Static).render())
        await pilot.click("#no")
        await _wait_for_code(pilot, app)
        go.code = app.screen._code
        go.outcome = _log_text(app.screen)
        await pilot.click("#quit")
        await pilot.pause()

    _run(go)
    assert go.asked is True, "a rebuild must be confirmed in the interface, not waved through"
    assert "Rocky Linux 10.1" in go.detail
    assert "cannot be submitted" in go.detail
    assert not started, "declining must not start a run"
    assert go.code == cli.EXIT_UNSUPPORTED_OS
    assert list(tmp_path.iterdir()) == [], "and must leave no half-created run behind"


# --- leaving a run that will not stop -------------------------------------------------
#
# Reported by a tester: "the exit button doesn't work via mouse/keyboard, nor does the escape key
# exit as it claims it will". Two things made that true at once.
#
# Cancel is cooperative - it sets a flag the engine reads between steps - so during a step that does
# not return it changed the status line and nothing else, and then disabled itself, leaving a screen
# with no working control anywhere on it. Escape did the same thing a second time. And the run used
# to execute on a Textual thread worker, which runs in asyncio's default executor: both ways out of
# an app join that executor, so even ctrl+q wedged the process behind the step being escaped.


def _never_returns(release):
    """A run that blocks until the test lets it go, the way a step that does not return behaves."""
    def run(hooks):
        hooks.status("waiting for something that never happens")
        release.wait(30)
        return 0
    return run


def _blocked(pilot, app, started):
    """Start a resident validate run and wait until its thread is inside the blocking step."""
    async def go():
        app.build_run = lambda argv: _never_returns(started["release"])
        await pilot.click("#menu-validate")
        await pilot.pause()
        await pilot.click("#start")
        for _ in range(60):
            await pilot.pause()
            if "never happens" in str(app.screen.query_one("#run-status", Static).render()):
                return
        raise AssertionError("the run never reached its blocking step")
    return go()


def test_a_running_run_always_has_a_quit_button():
    """The screen used to carry only Cancel, which disabled itself the first time it was pressed."""
    release = threading.Event()

    @drive()
    async def go(pilot, app):
        await _blocked(pilot, app, {"release": release})
        go.buttons = sorted(b.id for b in app.screen.query(Button))
        go.cancel_enabled = not app.screen.query_one("#cancel", Button).disabled
        release.set()
        await _wait_for_code(pilot, app)
        await pilot.click("#quit")
        await pilot.pause()

    _run(go)
    assert go.buttons == ["cancel", "quit"], go.buttons
    assert go.cancel_enabled is True


def test_quit_leaves_a_run_that_is_not_coming_back():
    """The report, pinned: quit while a step is blocked and the interface actually exits. The run's
    thread is still sitting in that step when it does, which is the whole point - nothing may wait
    on it."""
    release = threading.Event()

    @drive()
    async def go(pilot, app):
        await _blocked(pilot, app, {"release": release})
        await pilot.click("#quit")
        await pilot.pause()
        go.asked = isinstance(app.screen, _ConfirmScreen)
        # The question has to name the step being abandoned; that is what the answer turns on.
        go.detail = str(app.screen.query_one("#confirm-detail", Static).render())
        await pilot.click("#yes")
        await pilot.pause()
        go.still_blocked = not release.is_set()

    result = _run(go)
    assert go.asked is True, "quitting mid-run asks before throwing the run away"
    assert "never happens" in go.detail
    assert go.still_blocked is True, "the interface must not wait for the step it is escaping"
    assert result != 0, "an abandoned run is not a run that passed"
    release.set()


def test_escape_asks_to_stop_and_then_offers_the_way_out():
    """"nor does the escape key exit as it claims it will". The first press is still the polite
    request; the second is the operator saying it did not work."""
    release = threading.Event()

    @drive()
    async def go(pilot, app):
        await _blocked(pilot, app, {"release": release})
        await pilot.press("escape")
        await pilot.pause()
        go.after_one = str(app.screen.query_one("#run-status", Static).render())
        go.still_running = isinstance(app.screen, _RunScreen)
        await pilot.press("escape")
        await pilot.pause()
        go.after_two = isinstance(app.screen, _ConfirmScreen)
        await pilot.click("#no")
        await pilot.pause()
        release.set()
        await _wait_for_code(pilot, app)
        await pilot.click("#quit")
        await pilot.pause()

    _run(go)
    assert "Quit leaves without waiting" in go.after_one, (
        "the status has to say what Cancel can and cannot do, since it cannot stop this step"
    )
    assert go.still_running is True
    assert go.after_two is True, "a second escape has to offer something the first did not"


def test_the_run_can_be_asked_what_it_is_stuck_in():
    """A hang reported as "it stopped doing anything" is a guess; this turns it into a stack."""
    release = threading.Event()

    @drive()
    async def go(pilot, app):
        await _blocked(pilot, app, {"release": release})
        await pilot.press("d")
        await pilot.pause()
        go.text = _log_text(app.screen)
        release.set()
        await _wait_for_code(pilot, app)
        await pilot.click("#quit")
        await pilot.pause()

    _run(go)
    line = next(
        row for row in go.text.splitlines() if "stack dump written to" in row
    )
    path = line.split("stack dump written to ", 1)[1].strip()
    dump = open(path, encoding="utf-8").read()
    assert "waiting for something that never happens" in dump, "it must name the step"
    # The run thread has to be findable in the dump. Only 3.14 puts a name beside faulthandler's
    # thread ids, and this package ships against 3.9 and 3.12, so the header pairs each id with a
    # name itself and marks the one that matters.
    marked = [row for row in dump.splitlines() if row.endswith("<- the run")]
    assert len(marked) == 1, dump
    assert "alma-certify-run" in marked[0]
    ident = marked[0].strip().split()[0]
    stack = dump.split("Thread %s" % ident, 1)
    assert len(stack) == 2, "the header's ids must match the stacks below it"
    blocked = stack[1].split("\n\n", 1)[0]
    assert "in _worker" in blocked, "the marked stack has to be the one hosting the run"


def test_the_authorization_prompt_can_be_left():
    """The device code lives fifteen minutes. The dialog used to have no control on it at all, so
    an approval nobody gave held the interface for all of it. Skipping gives up on the upload, not
    on the run."""
    shown = threading.Event()
    skipped = threading.Event()

    def auth_run(hooks):
        hooks.authorize(
            verification_uri="https://lumina.example/my/activate/",
            user_code="WXYZ-7788",
            complete_uri="https://lumina.example/my/activate/?code=WXYZ-7788",
        )
        shown.set()
        for _ in range(300):
            if hooks.skip_authorization():
                skipped.set()
                break
            time.sleep(0.01)
        hooks.authorized()
        hooks.log("continuing without an upload")
        return 0

    @drive()
    async def go(pilot, app):
        app.build_run = lambda argv: auth_run
        await pilot.click("#menu-validate")
        await pilot.pause()
        await pilot.click("#start")
        for _ in range(60):
            await pilot.pause()
            if shown.is_set():
                break
        await pilot.pause()
        go.during = isinstance(app.screen, _AuthScreen)
        await pilot.press("escape")
        await pilot.pause()
        go.back_on_run = isinstance(app.screen, _RunScreen)
        await _wait_for_code(pilot, app)
        go.text = _log_text(app.screen)
        await pilot.click("#quit")
        await pilot.pause()

    result = _run(go)
    assert go.during is True
    assert skipped.is_set(), "the polling loop has to be told, or it waits the code out anyway"
    assert go.back_on_run is True, "skipping returns to the run, it does not end it"
    assert "continuing without an upload" in go.text
    assert result == 0, "the hardware is still worth testing"


def test_the_command_shown_includes_the_globals_the_interface_was_started_with():
    """Asked by a tester: "is there an issue with it respecting the --server flag I'm giving it?"
    The flag was carried into the run all along; the screen just never said so, showing the plan
    without the globals in front of it."""
    release = threading.Event()

    @drive(carried=["--server", "https://lumina.almalinux.dev"])
    async def go(pilot, app):
        app.build_run = lambda argv: _never_returns(release)
        await pilot.click("#menu-validate")
        await pilot.pause()
        await pilot.click("#start")
        await pilot.pause()
        go.shown = str(app.screen.query_one("#run-command", Static).render())
        release.set()
        await _wait_for_code(pilot, app)
        await pilot.click("#quit")
        await pilot.pause()

    _run(go)
    assert "--server https://lumina.almalinux.dev" in go.shown, go.shown
    assert go.shown.endswith("validate"), go.shown
