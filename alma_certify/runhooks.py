"""How a run reports to whatever is driving it.

The engine (``cli._execute`` / ``_execute_tests``) calls these instead of printing or reading stdin
directly, so one run implementation serves two front-ends: the CLI drives it with ``TerminalHooks``
(print, a log file, no interaction), and the Textual TUI will drive it with hooks that write to
widgets and can ask the run to stop. The base class is headless (activity and output go nowhere,
the run is never asked to stop); a driver overrides only what it needs.

Kept deliberately small and stdlib-only so both the base CLI and the vendored-Textual subpackage can
import it without pulling anything heavy.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

from .runner import utcnow_iso


class RunHooks:
    """The seam between a run and its driver. Defaults do nothing; a subclass overrides a subset."""

    def log(self, msg: str) -> None:
        """A line of run activity. The CLI prints it with a ``[timestamp]`` prefix."""

    def out(self, text: str = "") -> None:
        """A block of plain output that is not activity: the end-of-run summary, the run id."""

    def status(self, msg: str) -> None:
        """The step now running, shown as one live line so a long blocking operation - a package
        install, a 15s ``systemctl reload`` - does not read as a hang. Transient: the next status,
        the next test's progress, or the outcome replaces it, so unlike ``log`` it is not a record
        and the base simply drops it."""

    def progress(
        self, *, test_id: str, index: int, total: int, status: Optional[str] = None
    ) -> None:
        """A test is starting (``status`` None) or has finished (``status`` set), for a progress
        display. The CLI does not use this; its ``log`` lines already carry the same information."""

    def should_stop(self) -> bool:
        """Whether to stop the run at the next test boundary (the TUI's Cancel). False here."""
        return False

    def note(self, text: str = "") -> None:
        """Advice that is neither run activity nor the run's result: what the suite is about to
        change on this machine, why a check is going to skip, the commands it would run. The CLI
        writes these to stderr, so redirecting a run's output somewhere does not swallow them; a
        headless driver has nobody to advise and drops them."""

    def confirm(
        self,
        question: str,
        *,
        yes_label: str = "y",
        no_label: str = "n",
        default: bool = False,
    ) -> Optional[bool]:
        """Ask the operator a yes/no question and wait for the answer.

        The engine changes the machine under test in a couple of places - installing a GPU driver,
        inserting a module - and asks first. It must ask through the driver rather than reading
        stdin itself: under the TUI, stdin belongs to Textual, and an ``input()`` on the run's
        worker thread blocks forever behind a status line that still names the previous step. That
        is exactly what it did.

        ``None`` means this driver has no way to ask (headless, or a CLI run with no terminal), and
        is distinct from a "no": the caller says what flag to pass instead. Labels are the words the
        operator sees, so the question can be Reboot/continue where y/n would be a worse question.
        """
        return None

    def authorize(self, *, verification_uri: str, user_code: str, complete_uri: str) -> None:
        """The run needs the operator to approve this machine (device flow): show the URL, the code,
        and a QR of the complete URI. A display, not a prompt - polling continues after it, and
        ``authorized`` marks the end."""

    def authorized(self) -> None:
        """Authorization polling ended (approved, denied, or timed out): take the prompt down."""

    def skip_authorization(self) -> bool:
        """Whether the operator has given up waiting for the device-code approval.

        Distinct from ``should_stop``, which cancels the run. This one abandons only the upload: the
        hardware is still worth testing and ``alma-certify submit`` sends the results afterwards.
        The device code lives for fifteen minutes and an approval that is never given would hold a
        front-end for all of it, so a front-end that can show the prompt must be able to take it
        back. False here: a driver with no way to say so waits the code out, as it always did."""
        return False


class TerminalHooks(RunHooks):
    """Drives a run at a terminal: the behavior the CLI has always had.

    ``log`` both prints (with a timestamp) and appends to the run's log file, exactly as the old
    ``_log_factory`` did; ``out`` prints a block verbatim, as the summary and run id always were.
    ``progress`` stays a no-op and ``should_stop`` stays False, so a terminal run reads and behaves
    as before.

    ``note`` goes to stderr, so redirecting a run's output somewhere still shows what the suite is
    about to change; ``confirm`` is the one place in the package that reads stdin.

    ``run_dir`` is optional so the standalone commands - ``setup-gpu``, which changes the machine
    and therefore asks first - can drive the same prompts without inventing a run directory to log
    into. Without one, ``log`` only prints.
    """

    def __init__(self, run_dir: Optional[str] = None):
        self._log_path = os.path.join(run_dir, "alma-certify.log") if run_dir else None

    def log(self, msg: str) -> None:
        line = "[%s] %s" % (utcnow_iso(), msg)
        print(line, flush=True)
        if self._log_path is None:
            return
        with open(self._log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def out(self, text: str = "") -> None:
        print(text)

    def status(self, msg: str) -> None:
        # No status line to overwrite at a terminal, so print the step as it starts - flushed, so it
        # shows during the blocking call and not after it - and let the log line that follows report
        # how it went. A leading "..." and no timestamp mark it as a "now doing" note rather than a
        # logged event, and it is deliberately not written to the run log.
        print("... %s" % msg, flush=True)

    def note(self, text: str = "") -> None:
        print(text, file=sys.stderr)

    def confirm(
        self,
        question: str,
        *,
        yes_label: str = "y",
        no_label: str = "n",
        default: bool = False,
    ) -> Optional[bool]:
        # Both streams, because a prompt is only answerable when the person can read it and type
        # at it; either one redirected and there is nobody there. Reading EOF as consent would opt
        # somebody in silently, so a closed stdin is None (cannot ask) rather than False.
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None
        shown = "%s [%s/%s] " % (
            question,
            yes_label.capitalize() if default else yes_label,
            no_label if default else no_label.capitalize(),
        )
        try:
            answer = input(shown).strip().lower()
        except EOFError:
            # Ctrl-D at the prompt. Not an answer, and reading it as one would either abort a run
            # somebody wanted or opt them into a driver install they did not ask for.
            print(file=sys.stderr)
            return default
        if not answer:
            return default
        if answer.startswith(yes_label[:1].lower()):
            return True
        if answer.startswith(no_label[:1].lower()):
            return False
        # Anything else is a typo, not an instruction: take the safe answer rather than guessing.
        return default

    def authorize(self, *, verification_uri: str, user_code: str, complete_uri: str) -> None:
        # The same terminal prompt a bare `alma-certify register` shows; shared so there is one
        # of it.
        from .submit import auth

        auth.print_authorization(verification_uri, user_code, complete_uri)

    def authorized(self) -> None:
        # Nothing to take down at a terminal; the flow logs its own "Authorized"/timeout line.
        pass
