"""The Textual guided interface: the suite's one full-screen front-end.

It configures a run through a form and then, by default, HOSTS it: the run executes in-process on a
worker thread and its output streams into the interface (the resident path). The reader can instead
hand the command off and run it in plain output. Either way the command is built from the
framework-neutral ``Plan`` in ``alma_certify.planning`` and run through ``cli.execute`` /
``main``, so there is one definition of what a run is and the command shown is the command run.

The shape: a main menu of buttons, a settings hub that is a form (a checkbox or radio set per
setting) showing the command it is building the whole time, a run screen that streams a resident
run, and a previous-runs browser.

Textual is imported at module top because this module is only ever imported when the interface is
actually being launched (``cli._select_tui`` imports ``alma_certify_tui`` lazily, and only then),
with the vendored stack present. The base package never imports it.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.validation import Regex
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    ProgressBar,
    RadioButton,
    RadioSet,
    RichLog,
    SelectionList,
    Static,
)
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

from alma_certify import cli
from alma_certify.planning import (
    _GPU_LABELS,
    _WHAT_LABELS,
    GPU_ASK,
    GPU_NO,
    GPU_YES,
    Plan,
    categories_for,
)
from alma_certify.runhooks import RunHooks

_KIND_LABELS = {"gpu": "GPU", "cpu": "CPU", "nic": "NIC"}

# How much of the advice before a question is shown with it. Enough for what a GPU offer says it
# will change - a description, a caveat, and the commands - and not so much that a long install
# scrolls a dialog.
_NOTES_KEPT = 16

# What ``_WidgetHooks.post`` returns when there is no longer an interface to post to. A sentinel
# rather than None, because several of the things posted return None perfectly normally and the
# caller that cares - a question waiting for an answer - has to tell the two apart.
_GONE = object()


def unavailable_reason() -> Optional[str]:
    """Why the interface cannot start here, or None if it can.

    Reaching this means the subpackage imported, so Textual is present; what is left to rule out is
    a terminal it cannot draw on.
    """
    term = os.environ.get("TERM")
    if not term:
        return "TERM is not set, so there is no terminal to draw on"
    if term == "dumb":
        return "TERM is 'dumb', which cannot address the cursor"
    return None


def start(
    *,
    runs: Callable[[], Optional[Sequence[Dict[str, Any]]]],
    report_lines: Callable[[str], List[str]],
    version: str = "",
    host: str = "",
    carried: Optional[List[str]] = None,
):
    """Run the interface. Returns the value ``cmd_tui`` interprets: None to quit, an int for a
    resident run's exit code, or a list of argv to hand off and run in plain output. ``carried`` is
    the global flags the interface was started with, folded into whatever it runs or hands off."""
    return _App(
        runs=runs, report_lines=report_lines, version=version, host=host,
        carried=list(carried or []),
    ).run()


def _line(label: str, detail: str = "") -> str:
    return "%s   %s" % (label, detail) if detail else label


def _scope_kinds():
    from alma_certify.cli import SCOPE_KINDS

    return SCOPE_KINDS


class _HubScreen(Screen):
    """The settings as a form, the command they make, and the way out.

    A form rather than a list that drills into a sub-screen per setting: a checkbox for each on/off
    answer, a radio set for each exclusive one, ticked in place. The command it is building stays on
    screen the whole time, which is the part that teaches the CLI. Dismisses with ``None`` (back),
    or ``(mode, argv)`` where mode is ``"resident"`` (host the run here) or ``"handoff"`` (run it in
    plain output).
    """

    BINDINGS = [("escape", "back", "Back"), ("ctrl+s", "start", "Start")]

    def __init__(self, plan: Plan):
        super().__init__()
        self.plan = plan
        # Whether the category checklist is revealed. Off by default (run all), which is the run a
        # certification needs; on when a plan already carries a hand-picked set. A disclosure toggle
        # only - the source of truth for "partial" is plan.categories/scope, not this.
        self._pick_categories = bool(plan.categories)
        # Set once the controls exist, so the Changed events that fire while building the form (from
        # setting each control's initial state) are ignored rather than writing half a plan back.
        self._ready = False

    def compose(self) -> ComposeResult:
        plan = self.plan
        yield Header(show_clock=False)
        # The form scrolls; the command preview and the Start/Back buttons are pinned below it so
        # they stay on screen however tall the form gets or however short the terminal is.
        with Vertical(id="hub"):
            with VerticalScroll(id="hub-form"):
                yield Label("What to run")
                with RadioSet(id="what"):
                    yield RadioButton(_WHAT_LABELS["collect"], value=plan.what == "collect",
                                      id="what-collect")
                    yield RadioButton(_WHAT_LABELS["validate"], value=plan.what == "validate",
                                      id="what-validate")
                    yield RadioButton(_WHAT_LABELS["benchmark"], value=plan.what == "benchmark",
                                      id="what-benchmark")
                    yield RadioButton(_WHAT_LABELS["run"], value=plan.what == "run", id="what-run")

                yield Label("What it certifies", id="scope-label")
                with RadioSet(id="scope"):
                    yield RadioButton("the whole machine", value=not plan.scope, id="scope-all")
                    for kind in _scope_kinds():
                        yield RadioButton(
                            "only the %s (a card passed through to a VM guest)"
                            % _KIND_LABELS.get(kind, kind),
                            value=plan.scope == [kind], id="scope-%s" % kind,
                        )

                yield Label("Categories", id="cats-label")
                with RadioSet(id="coverage"):
                    yield RadioButton("Run all of them", value=not plan.categories,
                                      id="coverage-all")
                    yield RadioButton("Choose specific categories",
                                      value=bool(plan.categories), id="coverage-some")
                yield SelectionList(id="categories")
                # Shown when the scope narrows the run for us, in place of the checklist.
                yield Static("", id="cats-note")
                # Shown when the plan is narrower than a whole-machine validation: it explains that
                # such a run is supporting evidence, not a certification on its own.
                yield Static("", id="cert-note")

                yield Label("If the NVIDIA driver is missing", id="gpu-label")
                with RadioSet(id="gpu"):
                    yield RadioButton(_GPU_LABELS[GPU_ASK], value=plan.gpu == GPU_ASK, id="gpu-ask")
                    yield RadioButton(_GPU_LABELS[GPU_YES], value=plan.gpu == GPU_YES, id="gpu-yes")
                    yield RadioButton(_GPU_LABELS[GPU_NO], value=plan.gpu == GPU_NO, id="gpu-no")

                yield Checkbox("Hardware is unreleased (embargo the submission)",
                               value=plan.pre_release, id="pre_release")
                # Shown only while the box above is ticked. Blank holds the results until a person
                # lifts the embargo; a date lets the server publish them on or after that day.
                with Vertical(id="embargo"):
                    yield Label("Publish on or after this date, or leave blank to hold until "
                                "lifted:")
                    yield Input(
                        placeholder="YYYY-MM-DD", id="publish_after",
                        value=plan.publish_after or "",
                        validators=[Regex(r"^\d{4}-\d{2}-\d{2}$",
                                          failure_description="use YYYY-MM-DD")],
                    )

                # Attribution rather than secrecy, so it sits with the embargo above but is
                # never folded away by it: a run publishes either way, the question is whose
                # name is on it. Hidden only for a survey, which names no submitter at all.
                yield Checkbox("List this run anonymously when it is published",
                               value=plan.anonymous, id="anonymous")
                yield Checkbox("Upload the results when the run finishes", value=plan.submit,
                               id="submit")

            yield Static("", id="cmd")
            with Horizontal(id="hub-buttons"):
                yield Button("Start run", id="start", variant="primary")
                yield Button("Start in plain terminal", id="start-plain")
                yield Button("Back", id="back")
        yield Footer()

    def on_mount(self) -> None:
        self._reload_categories()
        self._sync_survey()
        self._sync_embargo()
        self._update_cmd()
        self._ready = True
        self.query_one("#what", RadioSet).focus()

    # --- keeping the plan and the screen in step -------------------------------------

    def _update_cmd(self) -> None:
        self.query_one("#cmd", Static).update("$ " + self.plan.command())

    def _reload_categories(self) -> None:
        """Rebuild the category checklist for the current run type, keeping still-valid ticks.

        The categories a run has depend on the run type, so this runs on mount and whenever the type
        changes. ``set_what`` has already dropped categories that do not carry over.
        """
        cats = self.query_one("#categories", SelectionList)
        cats.clear_options()
        for name in categories_for(self.plan.what):
            cats.add_option(Selection(name, name, name in self.plan.categories))
        self._sync_categories()

    def _sync_categories(self) -> None:
        """Show only what applies: the whole checklist collapses to two words until it is needed.

        A scope decides the categories for us, so with one set we hide the picker and say so. With
        the whole machine, the checklist appears only once "Choose specific categories" is picked,
        which keeps the form short in the common case (run everything).
        """
        scoped = bool(self.plan.scope)
        self.query_one("#coverage", RadioSet).display = not scoped
        self.query_one("#categories", SelectionList).display = not scoped and self._pick_categories
        note = self.query_one("#cats-note", Static)
        note.display = scoped
        if scoped:
            note.update("Set by the scope above.")
        self._sync_cert_note()

    def _sync_survey(self) -> None:
        """A survey uploads inventory and runs nothing, so hide what cannot apply to it.

        Leaving "What it certifies" and the category picker on screen for a run that executes
        no tests invites a reader to set them and then wonder why the command never changes -
        ``Plan.argv`` correctly drops them, so the form should not offer them either.
        """
        survey = self.plan.what == "collect"
        # The rows no other sync owns.
        for widget_id in ("scope-label", "scope", "cats-label", "gpu-label", "gpu",
                          "pre_release", "anonymous"):
            self.query_one("#" + widget_id).display = not survey
        if survey:
            # Owned by _sync_categories/_sync_embargo the rest of the time: a survey has no
            # categories to pick and nothing to embargo.
            for widget_id in ("coverage", "categories", "cats-note", "embargo"):
                self.query_one("#" + widget_id).display = False
        else:
            # Hand these back to their owners rather than second-guessing their state here -
            # forcing them visible is what hid the "pick categories" rule on the way past.
            self._sync_categories()
            self._sync_embargo()

    def _sync_cert_note(self) -> None:
        """Say, when the plan is narrower than a full run, that it is supporting evidence."""
        note = self.query_one("#cert-note", Static)
        if self.plan.what == "collect":
            # Not a narrower certification - a different thing entirely, and worth saying so
            # plainly where somebody picked it expecting a test run.
            note.display = True
            note.update(
                "A survey runs no tests and certifies nothing. It collects this machine's "
                "inventory and contributes it to the AlmaLinux hardware survey, which "
                "publishes aggregate statistics only."
            )
            return
        show = self.plan.what in ("validate", "run") and not self.plan.certifies_whole_system()
        note.display = show
        if show:
            note.update(
                "A system is certified only by a full run: the whole machine, every category. "
                "This narrower run is still uploaded and kept as supporting evidence for a full "
                "run - it will not certify the system on its own."
            )

    def _sync_embargo(self) -> None:
        box = self.query_one("#embargo", Vertical)
        box.display = self.plan.pre_release
        if not self.plan.pre_release and self.plan.publish_after:
            self.plan.publish_after = None
            self.query_one("#publish_after", Input).value = ""

    # --- events ----------------------------------------------------------------------

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if not self._ready:
            return
        which = event.radio_set.id
        value = (event.pressed.id or "").split("-", 1)[1]
        if which == "what":
            self.plan.set_what(value)
            self._reload_categories()
            self._sync_survey()
        elif which == "scope":
            self.plan.scope = [] if value == "all" else [value]
            if self.plan.scope:
                self.plan.categories = []
            self._reload_categories()
        elif which == "coverage":
            self._pick_categories = value == "some"
            if not self._pick_categories:
                # Back to "run all": drop any ticked categories so the plan really is all of them.
                self.plan.categories = []
                self.query_one("#categories", SelectionList).deselect_all()
            self._sync_categories()
        elif which == "gpu":
            self.plan.gpu = value
        self._update_cmd()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if not self._ready:
            return
        field = event.checkbox.id
        setattr(self.plan, field, bool(event.value))
        if field == "pre_release":
            self._sync_embargo()
        self._update_cmd()

    def on_input_changed(self, event: Input.Changed) -> None:
        if not self._ready or event.input.id != "publish_after":
            return
        text = event.value.strip()
        valid = event.validation_result is None or event.validation_result.is_valid
        # Only a well-formed date reaches the plan, so a half-typed one never builds an argv the CLI
        # would reject; the field still shows its invalid state while typing.
        self.plan.publish_after = text if (text and valid) else None
        self._update_cmd()

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        if not self._ready:
            return
        chosen = set(self.query_one("#categories", SelectionList).selected)
        self.plan.categories = [name for name in categories_for(self.plan.what) if name in chosen]
        self._sync_cert_note()
        self._update_cmd()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start":
            self.dismiss(("resident", self.plan.argv()))
        elif event.button.id == "start-plain":
            self.dismiss(("handoff", self.plan.argv()))
        else:
            self.dismiss(None)

    def action_back(self) -> None:
        self.dismiss(None)

    def action_start(self) -> None:
        self.dismiss(("resident", self.plan.argv()))


class _PagerScreen(ModalScreen):
    """Read-only scrollable text: a run's report, or a short explanation. Dismisses with None."""

    BINDINGS = [("escape", "cancel", "Back"), ("q", "cancel", "Back")]

    def __init__(self, title: str, lines: Sequence[str]):
        super().__init__()
        self._title = title
        self._lines = list(lines)

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self._title, id="dialog-title")
            with VerticalScroll(id="pager"):
                yield Static("\n".join(self._lines) or "(nothing to show)")
            yield Button("Back", id="back")

    def on_mount(self) -> None:
        self.query_one("#pager", VerticalScroll).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class _RunActionsScreen(ModalScreen):
    """What can be done with one previous run. Dismisses with argv for an action, or None."""

    BINDINGS = [("escape", "cancel", "Back"), ("q", "cancel", "Back")]

    def __init__(self, row: Dict[str, Any], report_lines: Callable[[str], List[str]]):
        super().__init__()
        self.row = row
        self._report_lines = report_lines

    def compose(self) -> ComposeResult:
        row = self.row
        with Vertical(id="dialog"):
            yield Static("Run %s\n%s" % (row["short_id"], row.get("summary", "")),
                         id="dialog-title")
            yield OptionList(
                Option("View the report", id="report"),
                Option("Resume it", id="resume"),
                Option("Upload it", id="submit"),
                Option("Write an offline bundle", id="bundle"),
                Option("Back", id="cancel"),
                id="actions",
            )

    def on_mount(self) -> None:
        self.query_one("#actions", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        oid = event.option_id or ""
        row = self.row
        # The gating the old interface stated in each row, kept here: an unfinished run has no
        # report.json, so both upload and bundle would build from nothing, and a finished run has
        # nothing to resume. Ineligible choices say why rather than producing a traceback later.
        if oid == "cancel":
            self.dismiss(None)
        elif oid == "report":
            if not row.get("reported"):
                self.app.notify("This run has no report yet.", severity="warning")
                return
            self.app.push_screen(_PagerScreen("Run %s" % row["short_id"],
                                              self._report_lines(row["short_id"])))
        elif oid == "resume":
            if row.get("finished"):
                self.app.notify("This run already finished.", severity="warning")
                return
            self.dismiss(["resume", row["short_id"]])
        elif oid == "submit":
            if row.get("submission"):
                self.app.notify("This run is already uploaded.", severity="warning")
                return
            if not row.get("reported"):
                self.app.notify("Unfinished; nothing to send yet.", severity="warning")
                return
            self.dismiss(["submit", row["short_id"]])
        elif oid == "bundle":
            if not row.get("reported"):
                self.app.notify("Unfinished; nothing to send yet.", severity="warning")
                return
            self.dismiss(["bundle", row["short_id"]])

    def action_cancel(self) -> None:
        self.dismiss(None)


class _RunsScreen(Screen):
    """Previous runs, and what can be done with one. Dismisses with argv for an action, or None.

    ``rows`` of None means the run directory is there and could not be read, a different answer from
    "there are none": a reader without root would otherwise be told this machine had never run
    anything, on a machine full of runs.
    """

    BINDINGS = [("escape", "back", "Back"), ("q", "back", "Back")]

    def __init__(self, rows: Optional[Sequence[Dict[str, Any]]],
                 report_lines: Callable[[str], List[str]]):
        super().__init__()
        self._rows = rows
        self._report_lines = report_lines

    def _submitted(self, row: Dict[str, Any]) -> str:
        if row.get("submission"):
            return "submitted " + (row["submission"] or {}).get("at", "")[:10]
        return "not submitted"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="runs"):
            if self._rows is None:
                yield Static("The run directory exists and cannot be read from here.\n"
                             "Runs live under /var/lib/alma-certify/runs, which is root's.")
                yield Button("Back", id="back")
            elif not self._rows:
                yield Static("There are no runs on this machine yet.\n"
                             "Start one from the first screen, or with: alma-certify run")
                yield Button("Back", id="back")
            else:
                yield Static("Previous runs (newest first).", id="runs-lead")
                yield OptionList(
                    *(Option(_line("%s  %s" % (row["short_id"], row.get("summary", "")[:44]),
                                   self._submitted(row)), id=str(i))
                      for i, row in enumerate(self._rows)),
                    id="runlist",
                )
        yield Footer()

    def on_mount(self) -> None:
        for widget_id in ("#runlist", "#runs"):
            try:
                self.query_one(widget_id).focus()
                break
            except Exception:  # noqa: BLE001 - whichever exists takes focus; nothing to do if not
                continue

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        row = self._rows[int(event.option_id)]
        self.app.push_screen(_RunActionsScreen(row, self._report_lines), self._action_done)

    def _action_done(self, argv) -> None:
        if argv is not None:
            self.dismiss(argv)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_back(self) -> None:
        self.dismiss(None)


class _AuthScreen(ModalScreen):
    """The device-authorization prompt during a resident run.

    Offers every way to approve, and puts the ones that always work first: open the URL on another
    device and type the code, or - if already signed in there - just approve, or scan the QR (which
    opens the page with the code already filled in). The URL, code, and waiting status sit above the
    QR and the whole dialog scrolls, so the code is readable even where the QR is large, does not
    fit, or does not render. Taken down by ``_WidgetHooks.authorized`` when polling ends (an
    approval or a timeout); the operator approves on a phone or any other device.
    """

    BINDINGS = [("c", "copy_code", "Copy code"), ("escape", "skip", "Skip")]

    def __init__(self, verification_uri: str, user_code: str, complete_uri: str,
                 on_skip: Optional[Callable[[], None]] = None):
        super().__init__()
        self._verification_uri = verification_uri
        self._user_code = user_code
        self._complete_uri = complete_uri
        self._on_skip = on_skip

    def compose(self) -> ComposeResult:
        from rich.text import Text

        from alma_certify.submit import qr

        # A scrolling container, not a fixed box: a large QR must never push the URL and code off
        # screen the way it did when the QR came first in a clipped dialog.
        with VerticalScroll(id="auth"):
            yield Static("Authorize this machine", id="dialog-title")
            yield Static("Approve this run in any one of these ways.")
            yield Static("")
            yield Static("1. Open this page on your phone or another computer:")
            yield Static("     %s" % self._verification_uri, id="auth-url")
            yield Static("   and enter this code:")
            yield Static("     %s" % self._user_code, id="auth-code")
            yield Static("   (press c to copy the code)", id="auth-copy-hint")
            yield Static("")
            yield Static("2. Already signed in on that device? Just approve there, no code needed.")
            yield Static("")
            yield Static("3. Or scan this (it opens the page with the code already filled in):")
            # ``qr.render`` emits ANSI-colored half-blocks (each module black or white via SGR
            # codes), so it must be parsed with ``from_ansi`` - wrapping it in plain ``Text`` would
            # render the escape codes literally, which is a garbled, oversized grid that clips. The
            # colors are in the content; #qr only adds a white quiet-zone margin around it.
            yield Static(Text.from_ansi(qr.render(self._complete_uri)), id="qr")
            yield Static("Waiting for approval; this closes when you approve.", id="auth-wait")
            # A way out. Without one this was a modal dialog with no control on it, covering the
            # run screen for as long as the code lived (fifteen minutes by default) while the
            # approval it was waiting for might never come - which is a hang, whatever the reason.
            with Horizontal(id="auth-buttons"):
                yield Button("Skip and run without uploading", id="auth-skip")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "auth-skip":
            self.action_skip()

    def action_skip(self) -> None:
        """Give up on authorizing and let the run go on without an upload.

        Not a cancel of the run: the results are still worth producing, and ``alma-certify submit``
        uploads them afterwards once this machine is registered. The polling loop is told through
        the callback, which is what actually ends it; this only takes the dialog down.
        """
        if self._on_skip is not None:
            self._on_skip()
        self.dismiss()

    def action_copy_code(self) -> None:
        """Copy the activation code to the clipboard (OSC 52).

        A running Textual app captures the mouse, which disables the terminal's own click-drag
        selection, so ``c`` is the way to lift the code out. Fire-and-forget and terminal-dependent;
        where the terminal ignores OSC 52 the code is still selectable with a shift-drag.
        """
        self.app.copy_to_clipboard(self._user_code)
        try:
            self.query_one("#auth-copy-hint", Static).update("   code copied")
        except Exception:
            pass


class _ConfirmScreen(ModalScreen):
    """A yes/no question the run has to stop and ask, put in front of the operator as a dialog.

    The run asks through ``RunHooks.confirm``, which at a terminal reads stdin. Under the interface
    stdin belongs to Textual, so a run that reached one of those questions - the GPU driver offer,
    on a machine with the drivers but no CUDA packages - blocked on an ``input()`` nobody could see
    or answer, behind a status line still naming the previous step. It read as a hang, and it was
    one.

    The reason is shown above the question, because these questions are about changing the machine
    under test and the answer depends on what is about to change. The default answer is the focused
    button, so Enter takes it; escape takes it too.
    """

    BINDINGS = [("escape", "take_default", "Default")]

    def __init__(self, question: str, detail: str, yes_label: str, no_label: str, default: bool):
        super().__init__()
        self._question = question
        self._detail = detail
        self._yes_label = yes_label
        self._no_label = no_label
        self._default = default

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self._question, id="dialog-title")
            if self._detail:
                yield Static(self._detail, id="confirm-detail")
            with Horizontal(id="confirm-buttons"):
                yield Button(self._yes_label, id="yes",
                             variant="primary" if self._default else "default")
                yield Button(self._no_label, id="no",
                             variant="primary" if not self._default else "default")

    def on_mount(self) -> None:
        self.query_one("#yes" if self._default else "#no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_take_default(self) -> None:
        self.dismiss(self._default)


def _outcome_text(code: int) -> str:
    if code == cli.EXIT_OK:
        return "Run complete."
    if code == cli.EXIT_FAILURES:
        return "Run finished with test failures. See the log above and the run page."
    if code == cli.EXIT_ERROR:
        return "Run finished with errors. See the log above."
    if code == cli.EXIT_UNSUPPORTED_OS:
        return "Nothing ran: this is not AlmaLinux, and the run was not confirmed."
    return "Run ended (exit %s)." % code


class _WidgetHooks(RunHooks):
    """RunHooks that drive a _RunScreen from the run's worker thread.

    Every update is marshalled onto the UI thread with ``call_from_thread``, because Textual widgets
    may only be touched there and the run executes on a worker thread.
    """

    def __init__(self, screen: _RunScreen):
        self._screen = screen
        self._app = screen.app
        self._auth = None
        # Set from the UI thread when the operator gives up on the authorization dialog, and read
        # from the run's thread by the polling loop. A plain Event because that is all it needs to
        # be and threading.Event is the one thing here that is safe in both directions.
        self._skip_auth = threading.Event()
        # Advice logged since the current step started, which is the reason for the question that
        # step is about to ask. Held here rather than read back off the screen so it is only ever
        # touched from the run's thread.
        self._notes = []

    def post(self, callback, *args):
        """Marshal ``callback`` onto the UI thread, dropping it if there is no longer one.

        The run's thread outlives the interface by design: Quit leaves without waiting for a step
        that will not return, so every update after that has nowhere to go. ``call_from_thread``
        raises once the app has stopped, and an unhandled exception here would print a traceback
        over the terminal the app has just handed back. Returns ``_GONE`` in that case, which is
        distinct from a callback that legitimately returned None.
        """
        # Checked before the call as well as caught after it: once the app has stopped,
        # ``call_from_thread`` builds a coroutine and then raises, leaving it never awaited.
        if not self._app.is_running:
            return _GONE
        try:
            return self._app.call_from_thread(callback, *args)
        except RuntimeError:
            return _GONE

    def log(self, msg: str) -> None:
        self.post(self._screen.write_log, msg)

    def out(self, text: str = "") -> None:
        self.post(self._screen.write_log, text)

    def status(self, msg: str) -> None:
        # A new step, so the advice collected for the last one is no longer the reason for anything.
        self._notes = []
        self.post(self._screen.set_activity, msg)

    def progress(self, *, test_id, index, total, status=None) -> None:
        if status is None:
            self.post(self._screen.set_activity, "[%d/%d] %s" % (index, total, test_id))
            self.post(self._screen.set_progress, index - 1, total)
        else:
            self.post(self._screen.set_progress, index, total)

    def should_stop(self) -> bool:
        return self._screen.stop_requested()

    def note(self, text: str = "") -> None:
        # Advice the CLI writes to stderr. There is no stderr to read during a resident run, so it
        # goes in the log with everything else - and it has to, because the next thing after it is
        # usually the question it is the reason for.
        # Capped, because one of the things logged through here is dnf installing several
        # gigabytes of CUDA line by line, and all of it would otherwise arrive in the next dialog.
        # The reason for a question is the last few lines before it; the log keeps the whole thing.
        self._notes.append(text)
        del self._notes[:-_NOTES_KEPT]
        self.post(self._screen.write_log, text)

    def confirm(self, question, *, yes_label="y", no_label="n", default=False):
        # Blocks the run's worker thread until the operator answers, which is the point: the engine
        # is mid-step and must not go on without the answer. Safe because this is a real thread and
        # not the event loop - the UI stays live and draws the dialog while it waits.
        detail = "\n".join(self._notes).strip("\n")
        self._notes = []
        answered = threading.Event()
        chosen = [default]

        def _done(result) -> None:
            chosen[0] = default if result is None else bool(result)
            answered.set()

        # Default labels are terminal shorthand; buttons get words.
        yes = "Yes" if yes_label == "y" else yes_label.capitalize()
        no = "No" if no_label == "n" else no_label.capitalize()
        # Built on the UI thread for the same reason as _AuthScreen: a Textual widget's __init__
        # binds the running loop on Python 3.9, so constructing one here would raise.
        if self.post(self._push_confirm, question, detail, yes, no, default, _done) is _GONE:
            # No interface to ask, because it has quit. Take the default rather than blocking this
            # thread on an answer that can never come.
            return default
        # Polled rather than a bare wait: the interface can be quit while this dialog is up, and
        # this thread would then hold an answer that is never coming.
        while not answered.wait(0.5):
            if not self._app.is_running:
                return default
        return chosen[0]

    async def _push_confirm(self, question, detail, yes, no, default, callback) -> None:
        await self._app.push_screen(
            _ConfirmScreen(question, detail, yes, no, default), callback
        )

    def authorize(self, *, verification_uri, user_code, complete_uri) -> None:
        # Build the screen on the UI thread, not here on the run's worker thread. A Textual widget
        # constructs an asyncio.Lock in __init__, and on Python 3.9 (EL9) that binds the running
        # loop - doing it off the loop thread raises "no current event loop". So carry the plain
        # strings across and let the UI thread build and push it.
        screen = self.post(self._push_auth, verification_uri, user_code, complete_uri)
        self._auth = None if screen is _GONE else screen

    async def _push_auth(self, verification_uri, user_code, complete_uri) -> _AuthScreen:
        screen = _AuthScreen(
            verification_uri, user_code, complete_uri, on_skip=self._skip_auth.set
        )
        await self._app.push_screen(screen)
        return screen

    def skip_authorization(self) -> bool:
        return self._skip_auth.is_set()

    def authorized(self) -> None:
        if self._auth is not None:
            # Already gone if the operator skipped it; dismissing a popped screen pops whatever is
            # under it, which would take the run screen away.
            if not self._skip_auth.is_set():
                self.post(self._auth.dismiss)
            self._auth = None


class _RunScreen(Screen):
    """Hosts a run in-process and streams its output. Dismisses with the run's exit code.

    The run is long and blocking, so it executes on a thread of its own; its RunHooks post back here
    via ``call_from_thread``. Cancel asks it to stop at the next boundary it checks. When it ends,
    Cancel is replaced with Back-to-menu.

    **Leaving always works.** Two things conspired to make that untrue. Cancel is cooperative - it
    sets a flag the engine reads between steps - so during a step that does not return, pressing it
    changed the status line and nothing else, and it then disabled itself, leaving a screen with no
    working control on it. And the run used to execute on a Textual thread worker, which runs in
    asyncio's default executor: shutting the app down joins that executor, so even ctrl+q wedged the
    process behind the very step the operator was trying to escape. So the run gets a plain daemon
    thread that nothing waits on, Quit is on screen for the whole run, and it is a real quit - the
    run's children are killed and the interface exits whatever the run's thread is doing.
    """

    BINDINGS = [
        ("escape", "leave", "Cancel"),
        ("ctrl+q", "force_quit", "Quit"),
        ("d", "dump_stacks", "Diagnose"),
    ]

    def __init__(self, argv: List[str], run: Callable[[RunHooks], int]):
        super().__init__()
        self._argv = argv
        self._run = run
        self._stop = threading.Event()
        self._code: Optional[int] = None
        self._activity: Optional[str] = None
        self._activity_since = 0.0
        self._thread: Optional[threading.Thread] = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="run"):
            yield Static("Running: alma-certify %s" % " ".join(self._argv), id="run-command")
            yield ProgressBar(id="run-progress", show_eta=False)
            yield Static("starting...", id="run-status")
            yield RichLog(id="run-log", highlight=False, markup=False, wrap=True)
            with Horizontal(id="run-buttons"):
                yield Button("Cancel", id="cancel", variant="warning")
                # On screen for the whole run, not only after it. A screen whose one control
                # disables itself the first time it is pressed is a screen with no way off it.
                yield Button("Quit", id="quit")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#run-log", RichLog).focus()
        # Ticks the elapsed counter on the current step (set_activity) once a second. This runs on
        # the UI thread, which stays free while the run's thread is blocked in a long call, so a
        # step that hangs for 15s is visibly counting up rather than frozen.
        self.set_interval(1.0, self._tick_activity)
        # A plain daemon thread rather than ``run_worker(thread=True)``. A Textual thread worker
        # runs in asyncio's default executor, and both ways out of an app join that executor - the
        # loop's ``shutdown_default_executor`` on 3.10+, and ``concurrent.futures``'s own atexit
        # hook below that - so a run stuck in an unreturning call made quitting impossible, not
        # merely slow. Daemon, so nothing at interpreter exit waits on it either.
        self._thread = threading.Thread(target=self._worker, name="alma-certify-run", daemon=True)
        self._thread.start()

    def _worker(self) -> None:
        hooks = _WidgetHooks(self)
        try:
            code = self._run(hooks)
        except Exception as exc:  # noqa: BLE001 - a crash in the run is reported, not fatal to the UI
            hooks.log("run failed: %s" % exc)
            code = 2
        except BaseException:  # noqa: BLE001 - the interface quit out from under this thread
            return
        hooks.post(self._finished, code)

    # Called on the UI thread via call_from_thread.
    def write_log(self, text: str) -> None:
        self.query_one("#run-log", RichLog).write(text)

    def set_status(self, text: str) -> None:
        # A settled line - the outcome, or "stopping..." - so stop any elapsed counter ticking.
        self._activity = None
        self.query_one("#run-status", Static).update(text)

    def set_activity(self, text: str) -> None:
        # The step now running. Its elapsed time ticks (see _tick_activity) so a step that blocks
        # for a while - a package install, a slow systemctl reload - is visibly alive, not hung.
        self._activity = text
        self._activity_since = time.monotonic()
        self._render_activity()

    def _render_activity(self) -> None:
        if self._activity is None:
            return
        elapsed = int(time.monotonic() - self._activity_since)
        suffix = "   %ds" % elapsed if elapsed >= 2 else ""
        self.query_one("#run-status", Static).update(self._activity + suffix)

    def _tick_activity(self) -> None:
        self._render_activity()

    def set_progress(self, completed: int, total: int) -> None:
        self.query_one("#run-progress", ProgressBar).update(total=total, progress=completed)

    def _finished(self, code: int) -> None:
        self._code = code
        self.set_status(_outcome_text(code))
        # Nothing in the header may still claim the run is going. Both of these were left
        # as they were at the start, so a finished collect run sat under "Running:" beside
        # a progress bar reading "--%".
        self.query_one("#run-command", Static).update(
            "Ran: alma-certify %s" % " ".join(self._argv)
        )
        bar = self.query_one("#run-progress", ProgressBar)
        if bar.total is None:
            # No test plan, so ``set_progress`` was never called and the bar is still
            # indeterminate. A survey collects inventory and runs no tests, which is the
            # common way to get here. An indeterminate bar has nothing to say about a run
            # that is over, so it goes rather than being filled to a 100% nobody measured.
            bar.display = False
        buttons = self.query_one("#run-buttons", Horizontal)
        try:
            self.query_one("#cancel", Button).remove()
        except Exception:  # noqa: BLE001 - already gone if Cancel was pressed
            pass
        # Back-to-menu goes in front of the Quit that has been there all along, which now means
        # "quit with this run's exit code" rather than "abandon the run".
        buttons.mount(Button("Back to menu", id="back", variant="primary"), before=0)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "cancel":
            self._request_stop()
        elif bid == "back":
            self.dismiss(self._code)
        elif bid == "quit":
            if self._code is None:
                self.action_force_quit()
            else:
                self.app.exit(self._code)

    def _request_stop(self) -> None:
        """Ask the run to stop at its next boundary, and say what that means.

        Cancel is not a kill and never was: the engine reads the flag between steps, so a step
        already under way - a package download, a benchmark pass, an authorization nobody has
        approved - finishes or times out first. The screen used to say "stopping after the current
        test..." and then disable the button, which read as a promise it could not keep and took the
        only control away at the same time. It now says what is actually happening and leaves Quit
        sitting next to it.
        """
        self._stop.set()
        self.set_status("stopping when the current step finishes; Quit leaves without waiting")
        try:
            self.query_one("#cancel", Button).disabled = True
        except Exception:  # noqa: BLE001 - already gone if the run finished meanwhile
            pass

    def action_leave(self) -> None:
        # Escape asks a running run to stop, and escalates: once a stop has been asked for and the
        # run has not acted on it, the second press is the operator saying the polite request did
        # not work, so offer the real way out rather than setting the same flag again.
        if self._code is not None:
            self.dismiss(self._code)
        elif self._stop.is_set():
            self.action_force_quit()
        else:
            self._request_stop()

    def action_force_quit(self) -> None:
        """Leave the interface whatever the run is doing, after saying what that costs."""
        if self._code is not None:
            self.app.exit(self._code)
            return
        self.app.push_screen(
            _ConfirmScreen(
                "Quit while the run is still going?",
                "The step now running is '%s'.\n\n"
                "Quitting kills the commands this run started and leaves the interface. The run is "
                "recorded on disk up to this point and `alma-certify runs` will list it, but it "
                "will not be finished or uploaded."
                % (self._activity or "starting"),
                "Quit", "Keep waiting", False,
            ),
            self._force_quit_answered,
        )

    def _force_quit_answered(self, quit_now) -> None:
        if not quit_now:
            return
        # The run's thread is a daemon and nothing waits on it, but the commands it started are
        # separate processes that would outlive this one. Kill them before going.
        from alma_certify import procutil

        self._stop.set()
        try:
            killed = procutil.kill_live()
        except Exception:  # noqa: BLE001 - quitting must not fail
            killed = 0
        self.app.exit(
            cli.EXIT_ERROR,
            message="Left the run part way through%s."
            % (", killing %d running command(s)" % killed if killed else ""),
        )

    def action_dump_stacks(self) -> None:
        """Write every thread's stack to a file and name it in the log.

        For the next time a step does not return. The interface can say which step it is on and how
        long it has been there; it cannot say what that step is blocked in, and without that a
        report of a hang is a guess. This turns one into a stack.

        The header is written by hand rather than left to ``faulthandler``, which labels each stack
        with a bare thread id: only 3.14 prints the name beside it, so on every interpreter this
        package ships against ("el9 = 3.9; el8, el10 = 3.12") a dump is a row of anonymous ids, and
        working out which one is the run - the entire point of taking it - means guessing. The ids
        below are the same ``Thread 0x...`` values faulthandler prints, so the two line up.
        """
        import faulthandler
        import tempfile

        path = os.path.join(
            tempfile.gettempdir(),
            "alma-certify-stacks-%d-%d.txt" % (os.getpid(), int(time.time())),
        )
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("step: %s\n" % (self._activity or "(none)"))
                if self._activity is not None:
                    fh.write("on this step for: %ds\n"
                             % int(time.monotonic() - self._activity_since))
                fh.write("stop requested: %s\n\nthreads:\n" % self._stop.is_set())
                for thread in threading.enumerate():
                    fh.write("  0x%016x  %s%s\n" % (
                        thread.ident or 0, thread.name,
                        "   <- the run" if thread is self._thread else "",
                    ))
                fh.write("\n")
                faulthandler.dump_traceback(file=fh, all_threads=True)
        except OSError as exc:
            self.write_log("could not write a stack dump: %s" % exc)
            return
        self.write_log("stack dump written to %s" % path)

    def stop_requested(self) -> bool:
        return self._stop.is_set()


class _MainScreen(Screen):
    """The first screen: pick a run type, or the previous-runs browser. Big buttons, not a list."""

    BINDINGS = [("q", "quit_app", "Quit")]

    _ACTIONS = (
        ("validate", "Validate this machine"),
        ("benchmark", "Benchmark this machine"),
        ("run", "Both, in one run"),
        ("collect", "Collect inventory only (hardware survey)"),
        ("runs", "Previous runs"),
    )

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="menu"):
            yield Static("What would you like to do?", id="menu-lead")
            for value, label in self._ACTIONS:
                yield Button(label, id="menu-%s" % value, variant="primary")
            yield Button("Quit", id="menu-quit")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#menu-validate", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action = (event.button.id or "").split("-", 1)[1]
        if action == "quit":
            self.app.exit(self.app.exit_code)
        elif action == "runs":
            rows = self.app.tui_runs()
            self.app.push_screen(_RunsScreen(rows, self.app.tui_report_lines), self._runs_done)
        else:
            self.app.push_screen(_HubScreen(Plan(action)), self._hub_done)

    def _hub_done(self, result) -> None:
        # None: backed out, stay on the menu. Otherwise (mode, argv): host it here (resident)
        # or hand it off to run in plain output.
        if result is None:
            return
        mode, argv = result
        if mode == "handoff":
            self.app.exit(argv)
        else:
            self.app.push_screen(
                _RunScreen(self.app.full_command(argv), self.app.build_run(argv)),
                self._resident_done,
            )

    def _resident_done(self, code) -> None:
        # A resident run finished and the reader chose "Back to menu": remember its code so quitting
        # from here returns it, and stay so they can start another run.
        self.app.exit_code = code

    def _runs_done(self, argv) -> None:
        # A previous-runs action (resume/upload/bundle) hands off to run in plain output.
        if argv is not None:
            self.app.exit(argv)

    def action_quit_app(self) -> None:
        self.app.exit(self.app.exit_code)


class _App(App):
    CSS = """
    Screen { align: center middle; }
    #menu { width: 60; height: auto; padding: 1 2; }
    #menu-lead { padding-bottom: 1; }
    #menu Button { width: 100%; margin-bottom: 1; }
    #hub { width: 84; height: 90%; padding: 1 2; }
    #hub-form { height: 1fr; }
    #hub Label { text-style: bold; margin-top: 1; }
    #hub RadioSet { width: 100%; }
    #hub Checkbox { margin-top: 1; }
    #embargo { height: auto; padding: 0 0 0 3; }
    #embargo Input { width: 24; }
    #cats-note { color: $text-muted; padding-left: 3; }
    #cert-note { color: $text-warning; padding-top: 1; }
    #cmd { padding-top: 1; color: $text-muted; }
    #hub-buttons { height: auto; padding-top: 1; }
    #runs { width: 78; height: auto; max-height: 90%; padding: 1 2; }
    #runs-lead { padding-bottom: 1; }
    #run { width: 100%; height: 100%; padding: 1 2; }
    #run-command { text-style: bold; }
    #run-status { color: $text-muted; padding: 1 0; }
    #run-log { height: 1fr; border: round $panel; padding: 0 1; }
    #run-buttons { height: auto; padding-top: 1; }
    #dialog { width: 72; height: auto; max-height: 90%; padding: 1 2; border: round $accent;
              background: $surface; }
    #dialog-title { padding-bottom: 1; }
    #confirm-detail { color: $text-muted; padding-bottom: 1; }
    #confirm-buttons { height: auto; }
    #auth { width: 82%; height: 90%; padding: 1 2; border: round $accent; background: $surface; }
    #auth-url { color: $text-accent; }
    #auth-code { color: $text-accent; text-style: bold; }
    #qr { background: white; padding: 1 2; margin: 1 0; width: auto; height: auto; }
    #auth-wait { padding-top: 1; color: $text-muted; }
    #auth-buttons { height: auto; padding-top: 1; }
    #pager { height: 20; border: round $panel; padding: 0 1; margin-bottom: 1; }
    Button { margin-right: 2; }
    OptionList { height: auto; max-height: 20; }
    """

    def __init__(self, *, runs, report_lines, version, host, carried):
        super().__init__()
        self.tui_runs = runs
        self.tui_report_lines = report_lines
        self._version = version
        self._host = host
        self._carried = carried
        # The exit code of the last resident run, so quitting from the menu afterwards returns it.
        self.exit_code = None

    def full_command(self, argv):
        """The argv a resident run actually executes: the plan with the carried globals in front.

        What the run screen shows, too. It used to show the plan alone, so a run started as
        ``alma-certify --server https://lumina.almalinux.dev tui`` displayed "Running: alma-certify
        validate" - which is not the command it was running, and left the reader with no way to
        tell whether the flag they gave had survived into the run.
        """
        return self._carried + list(argv)

    def build_run(self, argv):
        """The callable a _RunScreen executes: host the plan (with carried globals) in-process."""
        full = self.full_command(argv)

        def run(hooks):
            # ``hooks`` reaches prepare_run because the unsupported-OS question lives there: this
            # path used to skip it, so a resident run on a RHEL rebuild started with nobody told
            # that nothing it produced could be submitted.
            try:
                state, config, run_types = cli.prepare_run(full, hooks)
            except cli.HostNotSupported:
                return cli.EXIT_UNSUPPORTED_OS
            return cli.execute(state, config, run_types, hooks, debug=False)

        return run

    def on_mount(self) -> None:
        self.title = "alma-certify"
        self.sub_title = "  ".join(part for part in (self._version, self._host) if part)
        self.push_screen(_MainScreen())
