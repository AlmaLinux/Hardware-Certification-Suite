"""The choices behind one guided run, and the command they add up to.

Framework-neutral on purpose: this is what a person picked, and ``Plan.argv`` turns it into what the
CLI takes. The guided interface (``alma_certify_tui``) renders and edits a ``Plan``; nothing here
draws anything, so the planner is testable without a terminal and there is one definition of "what a
run is" for every front-end. It was extracted from the old curses module when that was retired,
which is why the doc voice matches the rest of the suite.
"""
from __future__ import annotations

from typing import Any, List, NamedTuple, Optional


class Item(NamedTuple):
    """One line in a menu: what it says, and what choosing it means."""

    label: str
    value: Any
    # Shown to the right of the label. For a setting, what it is currently set to.
    detail: str = ""


# The GPU-driver answer is three-valued, not two: doing nothing means "ask me when we get there",
# which is the default and is not the same as either flag.
GPU_ASK, GPU_YES, GPU_NO = "ask", "yes", "no"
_GPU_LABELS = {
    GPU_ASK: "ask me if a driver is missing",
    GPU_YES: "install it without asking",
    GPU_NO: "never install anything",
}

_WHAT_LABELS = {
    "collect": "collect inventory only (hardware survey)",
    "validate": "validate this machine",
    "benchmark": "benchmark this machine",
    "run": "collect, validate, and benchmark",
}

# The run types that actually execute tests. A collect-only run is a hardware-survey
# submission: it runs nothing, certifies nothing, and is never published as a listing -
# so the choices below that only shape a test run do not apply to it, and the ``collect``
# subcommand does not even accept the selection flags.
_RUNS_TESTS = ("validate", "benchmark", "run")


class Plan:
    """The choices behind one run, and the command they add up to.

    Deliberately not an ``argparse.Namespace``: this holds what a person said, and ``argv`` turns it
    into what the CLI takes. Keeping them apart is what makes the interface testable without a
    terminal and what stops a second definition of "what a run is" growing here.
    """

    def __init__(self, what: str = "run"):
        self.what = what
        self.scope: List[str] = []
        self.categories: List[str] = []
        self.pre_release = False
        # An embargo end date (YYYY-MM-DD) for a pre-release submission: the earliest results may be
        # published. None means held until a person lifts it. Only meaningful with pre_release.
        self.publish_after: Optional[str] = None
        self.gpu = GPU_ASK
        # Publish this run without a username on it. Only means anything for a run that
        # produces a certification submission: a survey publishes aggregates and names no
        # submitter at any point, so there is nothing there to be anonymous about.
        self.anonymous = False
        self.submit = True

    def argv(self) -> List[str]:
        argv = [self.what]
        # Only for a run that executes tests: ``collect`` takes no --scope/--category at
        # all, and an embargo or a driver install has nothing to act on in a survey.
        if self.what in _RUNS_TESTS:
            if self.scope:
                argv += ["--scope", ",".join(self.scope)]
            if self.categories:
                argv += ["--category", ",".join(self.categories)]
            if self.pre_release:
                argv.append("--pre-release")
            if self.publish_after:
                argv += ["--publish-after", self.publish_after]
            if self.gpu == GPU_YES:
                argv.append("--gpu-setup")
            elif self.gpu == GPU_NO:
                argv.append("--no-gpu-setup")
            if self.anonymous:
                argv.append("--anonymous")
        if not self.submit:
            argv.append("--no-submit")
        return argv

    def command(self) -> str:
        return "alma-certify " + " ".join(self.argv())

    def certifies_whole_system(self) -> bool:
        """Whether this run could certify the machine on its own if it passes.

        A certification is of a whole system, so it takes a validation run of the entire machine
        with every category. A narrower run - a single ``--scope``, or a hand-picked set of
        categories - is still worth uploading, but as supporting evidence toward a full run rather
        than a certification in itself. A benchmark measures speed and never certifies, so a
        benchmark-only run is not whole-system either. The server is the authority on what it will
        accept; this is the same rule stated where the run is chosen, so the choice is informed.
        """
        return self.what in ("validate", "run") and not self.scope and not self.categories

    def set_what(self, what: str) -> None:
        """Change the run type, and forget categories chosen for the old one.

        One writer for two fields that have to move together. Backing out and picking a different
        run type used to keep categories that may not exist in the new one: ``alma-certify benchmark
        --category ipmi`` selects nothing, runs no tests, and exits 0, which reads as a machine that
        passed.
        """
        if what == self.what:
            return
        self.what = what
        self.categories = []
        # Switching to a survey drops the choices that only mean something for a test
        # run, so backing into it cannot leave a scope or an embargo silently set.
        if what not in _RUNS_TESTS:
            self.scope = []
            self.pre_release = False
            self.publish_after = None

    def rows(self) -> List[Item]:
        """The hub's settings, each with what it is currently set to."""
        return [
            Item("What to run", "what", _WHAT_LABELS[self.what]),
            Item("What it certifies", "scope",
                 ", ".join(self.scope) if self.scope else "the whole machine"),
            Item("Categories", "categories",
                 ", ".join(self.categories) if self.categories else "all of them"),
            Item("Missing GPU driver", "gpu", _GPU_LABELS[self.gpu]),
            Item("Unreleased hardware", "pre_release",
                 "yes, hold the results back" if self.pre_release else "no"),
            Item("Upload when finished", "submit", "yes" if self.submit else "no"),
        ]


def categories_for(what: str) -> List[str]:
    """The categories a given run type actually has tests in.

    From the registry rather than a list kept here, so a category added with a new test appears
    without anybody remembering to update the interface.
    """
    from .registry import REGISTRY, load_all_tests

    load_all_tests()
    wanted = ("validate", "benchmark") if what == "run" else (what,)
    # Through ``select`` rather than over ``all``, with the interactive default the interface's
    # argv implies. The raw registry offered a category whose only tests are
    # interactive, and choosing it produced a run that selected nothing, ran nothing, and exited 0:
    # a machine that reads as having passed.
    return sorted({
        cls.category for run_type in wanted for cls in REGISTRY.select(run_type)
    })
