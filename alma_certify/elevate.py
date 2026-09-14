"""Getting to root, or saying plainly why this machine cannot.

Every command here reads or writes something only root can: the run directory is 0750 root:root
because it holds serial numbers and DMI UUIDs, the submission token lives in /etc, and the tests
themselves read DMI, SMART, and PCI configuration space. So the suite is gated on root at its one
entry point rather than per command, and "root" includes root reachable through ``sudo``: somebody
who typed the command without it has not made a mistake worth retyping, they just need elevating.

Asked before elevating, never assumed. A tool that silently re-runs itself with more privilege than
it was given is doing something the person at the keyboard did not type, and the answer is theirs.
With nothing to ask - a kickstart ``%post``, a CI runner, a cron job - it refuses and names the fix
rather than prompting into a closed stdin or escalating unasked.
"""
from __future__ import annotations

import os
import shutil
import sys
from typing import List, Optional, Sequence

# Set across the exec so a sudo that somehow lands back here as the same user refuses rather than
# asking again forever. sudo's own env_reset usually drops it, which is why the euid check above it
# is the real guard; this only matters where a site has turned env_reset off.
ELEVATED_ENV = "ALMA_CERTIFY_ELEVATED"

SUDO = "sudo"


def is_root() -> bool:
    return os.geteuid() == 0


def sudo_available() -> bool:
    return shutil.which(SUDO) is not None


def relaunch_argv(args: Sequence[str]) -> Optional[List[str]]:
    """The argv that re-runs this invocation as root, or None if it cannot be built.

    Rebuilt from the interpreter and the package's own location rather than from ``sys.argv[0]``,
    which is not the command: the installed launcher runs ``python -I -c '...' "$@"``, so argv[0] is
    the string "-c". The same ``-I`` here, for the same reason it is in the launcher - a tool about
    to run as root must not take sys.path from the environment or the working directory.
    """
    interpreter = sys.executable
    if not interpreter or not sudo_available():
        return None
    package = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    boot = (
        "import sys; sys.path.insert(0, %r); "
        "from alma_certify.cli import main; sys.exit(main())" % package
    )
    # "--" so an argument of the suite's that starts with a dash is never read as one of sudo's.
    return [SUDO, "--", interpreter, "-I", "-c", boot] + list(args)


def elevate(args: Sequence[str], *, execvp=os.execvp) -> None:
    """Replace this process with the same command under sudo. Returns only if it could not.

    ``execvp`` rather than a subprocess: there is nothing for this process to do afterwards, and
    handing the terminal straight over means sudo's password prompt, the run's output, and Ctrl-C
    all behave as though the person had typed the sudo themselves.
    """
    argv = relaunch_argv(args)
    if argv is None:
        return
    os.environ[ELEVATED_ENV] = "1"
    execvp(argv[0], argv)
