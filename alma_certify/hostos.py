"""Is this machine running AlmaLinux?

The suite will run its tests anywhere - a test that only executes on the one
supported distribution is useless for finding out whether the distribution is the
problem. What it will not do is let results from another distribution be
submitted as AlmaLinux certification evidence.

So the gate is on *submission*, not on execution:

- On AlmaLinux, nothing changes.
- Anywhere else, the run is announced as unsupported, confirmed with the person
  at the keyboard, and then forced offline: tests run, a bundle can be written,
  and no upload path is offered.

``ID`` is compared exactly, with no ``ID_LIKE`` fallback. Every RHEL rebuild
carries ``ID_LIKE="rhel centos fedora"``, so accepting that would accept exactly
the distributions this check exists to tell apart.
"""

from __future__ import annotations

import sys
from typing import Dict, Optional, TextIO

from . import procutil

ALMALINUX_ID = "almalinux"

OS_RELEASE_PATH = "/etc/os-release"


class HostOS:
    """What ``/etc/os-release`` says this machine is."""

    def __init__(self, os_id: str, version_id: str, pretty_name: str) -> None:
        self.id = os_id
        self.version_id = version_id
        self.pretty_name = pretty_name

    @property
    def is_almalinux(self) -> bool:
        return self.id == ALMALINUX_ID

    @property
    def display(self) -> str:
        """The most specific name available, for messages.

        ``PRETTY_NAME`` when the file has one; otherwise the id and version, which
        is all a minimal container image tends to carry. "unknown" rather than an
        empty string, so a message never reads "this is not AlmaLinux, it is ."
        """
        if self.pretty_name:
            return self.pretty_name
        if self.id and self.version_id:
            return "%s %s" % (self.id, self.version_id)
        return self.id or "an unidentified operating system"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "HostOS(id=%r, version_id=%r)" % (self.id, self.version_id)


def parse_os_release(content: str) -> Dict[str, str]:
    fields = {}  # type: Dict[str, str]
    for line in content.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields


def detect(path: str = OS_RELEASE_PATH) -> HostOS:
    """Read the running system's identity.

    A missing or unreadable ``/etc/os-release`` yields an empty id, which is not
    AlmaLinux and so takes the unsupported path. Treating "cannot tell" as
    supported would make a stripped-down image the way around the check.
    """
    fields = parse_os_release(procutil.read_file(path, "") or "")
    return HostOS(
        os_id=fields.get("ID", ""),
        version_id=fields.get("VERSION_ID", ""),
        pretty_name=fields.get("PRETTY_NAME", ""),
    )


def report_os_id(report: Dict) -> str:
    """The distribution recorded *in a report*, which is not always this host.

    ``alma-certify submit`` has to judge the machine the tests ran on, not the one
    doing the uploading. Those differ whenever a run is bundled on one box and
    submitted from another, and re-detecting the local host would let an
    unsupported run through by moving it.
    """
    environment = report.get("environment") or {}
    return str((environment.get("os") or {}).get("id") or "").strip()


def report_is_almalinux(report: Dict) -> bool:
    return report_os_id(report) == ALMALINUX_ID


UNSUPPORTED_NOTICE = (
    "AlmaLinux is the only supported operating system for this suite.\n"
    "  This machine reports: %s\n"
    "\n"
    "  The tests will still run, and results are still written to disk, but they\n"
    "  cannot be submitted: the catalog records what works on AlmaLinux, and a\n"
    "  result from another distribution is not evidence about AlmaLinux.\n"
    "  Expect skips and failures from checks that assume AlmaLinux packaging."
)


def warn(host: HostOS, out: Optional[TextIO] = None) -> None:
    """Say what the machine is and what that costs, before anything runs."""
    stream = out if out is not None else sys.stderr
    stream.write("\nWARNING: unsupported operating system\n")
    stream.write("  " + (UNSUPPORTED_NOTICE % host.display) + "\n\n")


def confirm(
    host: HostOS,
    *,
    assume_yes: bool = False,
    interactive: Optional[bool] = None,
    out: Optional[TextIO] = None,
    reader=None,
) -> bool:
    """Ask whether to run anyway. False means the caller should stop.

    Three paths, because a prompt that hangs is worse than a prompt that refuses:

    - ``--allow-unsupported-os`` was given: proceed without asking. The person
      has already answered.
    - Attached to a terminal: ask, defaulting to **no**. Continuing is the
      surprising outcome, so it is the one that has to be typed.
    - Not attached to a terminal: refuse, and name the flag. Prompting into a
      closed stdin would block a CI job or a kickstart %post forever, and
      reading EOF as consent would silently opt them in.
    """
    stream = out if out is not None else sys.stderr
    warn(host, out=stream)

    if assume_yes:
        stream.write("continuing anyway (--allow-unsupported-os)\n")
        return True

    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stderr.isatty()
    if not interactive:
        stream.write(
            "not attached to a terminal, so this cannot be confirmed here.\n"
            "Pass --allow-unsupported-os to run anyway (results stay local).\n"
        )
        return False

    prompt = "Run the tests anyway? Results will not be submittable. [y/N]: "
    ask = reader if reader is not None else input
    try:
        answer = ask(prompt)
    except EOFError:
        answer = ""
    return str(answer).strip().lower() in ("y", "yes")
