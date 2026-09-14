"""alma-certify command-line interface."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shlex
import sys
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import __version__, cpugov, elevate, gpusetup, hostos, nosleep, procutil, virt
from . import bundle as bundle_mod
from . import inventory as inventory_mod
from . import report as report_mod
from .config import DEFAULT_CONFIG_PATH, DEFAULT_SERVER, DEV_SERVER, Config
from .pkg import PackageManager
from .registry import REGISTRY, RunContext, load_all_tests
from .runhooks import RunHooks, TerminalHooks
from .runner import Runner, utcnow_iso
from .state import RunState

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_ERROR = 2
EXIT_USAGE = 3
# Its own code, not folded into EXIT_USAGE: declining the unsupported-OS prompt is
# not a mistyped command, and a script driving the suite needs to tell "blocked
# before starting" apart from "ran, and the hardware failed".
EXIT_UNSUPPORTED_OS = 4

SERVER_ENV = "ALMA_CERTIFY_SERVER"

SELF_SIGNED_HELP = (
    "accept the server's TLS certificate without verifying it, for a dev or staging catalog "
    "with a self-signed one. Also settable as [general] allow_self_signed. This accepts any "
    "certificate from anyone answering on that address, so use it against a server you know"
)

# --disk used to select the target for the storage benchmarks. Those are gone -
# certification compares machines, not drive models - and its remaining consumer
# is validate.storage.io-sanity, which writes a file and reads it back. It is a
# directory, not a block device: the old help text said "block device or
# directory" and nothing ever accepted a device node.
DISK_HELP = (
    "directory the storage I/O check writes its test file in "
    "(default /var/tmp); use this to test a specific filesystem or drive"
)
SERVER_HELP = (
    "catalog base URL, e.g. " + DEFAULT_SERVER + ". Overrides "
    "[general] server in the config file; also settable via $" + SERVER_ENV
)
DEV_HELP = (
    "submit to the staging catalog at " + DEV_SERVER + " instead of the production one. "
    "For developing against a pre-release server; --server overrides it"
)
RUN_DIR_HELP = "base directory for run state (default /var/lib/alma-certify/runs)"

# What a run is a claim *about*, as opposed to which tests it happens to run.
#
# The two are different and the difference is the whole point of the flag. ``--category gpu``
# already runs only the GPU tests, and a run that does that is still submitted as a claim about the
# whole machine: the server sees a machine validation that skipped nearly everything. ``--scope
# gpu`` says the claim itself is narrower, and the server certifies the card and nothing else - not
# the chassis, not the CPU, not the board, and above all not the cloud instance the card was passed
# through to.
#
# It narrows the tests as well, because running the storage and firmware checks to prove something
# about a GPU wastes an hour and produces failures that are about the host.
# GPU and nothing else, at the maintainer's direction, and the reason is virtualization.
#
# A component claim exists so a card can be certified in a cloud instance, where the host belongs
# to somebody else and is unknowable. A GPU survives that: passed through to a guest it is the real
# device, with its own firmware and its own memory, and the driver talks to it directly. Nothing
# else does. A CPU is whatever the hypervisor chose to expose, a NIC may be a paravirtual device,
# and a disk is a virtio queue onto storage the guest cannot see, so a claim about any of those from
# a guest would be a claim about the hypervisor. They can still be certified, as part of a
# whole-machine run on bare metal, where the firmware and the board are being certified too.
#
# A tuple rather than a constant, because the flow around it is deliberately generic and other kinds
# may earn their way in later. Lumina holds the same list for the same reason.
SCOPE_KINDS = ("gpu",)
SCOPE_HELP = (
    "certify only these components rather than the whole machine, as a comma-separated list of "
    + ", ".join(SCOPE_KINDS) + ". Use this to validate a GPU on its own, including one passed "
    "through to a virtual machine: the results say nothing about the host and the catalog will not "
    "certify it. Implies the matching --category unless you pass one. A GPU is the only component "
    "this accepts, because it is the only one a guest sees as the real device; everything else is "
    "certified as part of a whole-machine run on bare metal."
)
# The test categories that pertain to each kind. The server has its own copy of this decision and
# refuses to certify a claim with no gating result in it, so a kind missing here would produce a
# run that is accepted and certifies nothing.
SCOPE_CATEGORIES = {
    "gpu": ("gpu",),
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw)
    try:
        if not _require_root(args, raw):
            return EXIT_USAGE
        return _dispatch(args, parser)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_ERROR


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Run whatever was asked for. Separated so one guard covers every route into it.

    The guided interface used to be dispatched outside the ``KeyboardInterrupt`` guard, so Ctrl-C
    out of the wizard printed a traceback and died by signal, while the same keystroke under
    ``alma-certify tui`` printed "interrupted" and returned 2. Exit codes are a documented contract,
    and two entry points to the same screen cannot honor it differently.
    """
    if not getattr(args, "func", None):
        # A bare ``alma-certify`` at a terminal opens the guided interface, and anywhere else prints
        # the help and exits 3 exactly as it always has. The tty test keeps that safe: a script or a
        # kickstart ``%post`` calling the bare command has no terminal, so it sees no change, and
        # nothing can sit waiting for a keystroke that is never coming.
        if _interactive_terminal():
            return cmd_tui(args)
        parser.print_help()
        return EXIT_USAGE
    return args.func(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alma-certify",
        description="AlmaLinux hardware certification suite",
    )
    parser.add_argument("--version", action="version", version="alma-certify %s" % __version__)
    parser.add_argument("--config", help="path to an alma-certify.conf overriding defaults")
    # Environment-shaped options are accepted both before and after the
    # subcommand, because either placement is a reasonable thing to type.
    # The per-subcommand copies use SUPPRESS so an absent flag there does not
    # clobber a value given globally.
    parser.add_argument("--server", metavar="URL", help=SERVER_HELP)
    parser.add_argument("--dev", action="store_true", help=DEV_HELP)
    parser.add_argument("--allow-self-signed", action="store_true",
                        help=SELF_SIGNED_HELP)
    parser.add_argument("--run-dir", metavar="DIR", help=RUN_DIR_HELP)
    sub = parser.add_subparsers(dest="command")

    def add_server_flag(p: argparse.ArgumentParser) -> None:
        p.add_argument("--server", metavar="URL", default=argparse.SUPPRESS,
                       help=SERVER_HELP)
        p.add_argument("--dev", action="store_true", default=argparse.SUPPRESS,
                       help=DEV_HELP)
        p.add_argument("--allow-self-signed", action="store_true",
                       default=argparse.SUPPRESS, help=SELF_SIGNED_HELP)

    def add_run_dir_flag(p: argparse.ArgumentParser) -> None:
        p.add_argument("--run-dir", metavar="DIR", default=argparse.SUPPRESS,
                       help=RUN_DIR_HELP)

    def add_debug_flag(p: argparse.ArgumentParser) -> None:
        p.add_argument("--debug", action="store_true",
                       help="trace every command the suite runs, with its raw "
                            "stdout/stderr, exit code, and duration, on stderr")

    def add_run_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--pre-release", action="store_true",
                       help="mark the hardware as unreleased (embargoed submission)")
        p.add_argument("--publish-after", metavar="YYYY-MM-DD",
                       help="earliest date results may be published; omit it with "
                            "--pre-release to withhold them until someone lifts the hold")
        p.add_argument("--support-from-minor", metavar="N", type=int,
                       help="on AlmaLinux Kitten: the minor of this major that the "
                            "hardware enablement lands in. The listing publishes with a "
                            "note saying so until that minor ships. Reviewable and "
                            "correctable in the web interface either way")
        p.add_argument("--anonymous", action="store_true",
                       help="publish this run without your username on it - it is listed "
                            "as Anonymous. Reviewers still see who submitted it, and it "
                            "can be changed afterwards in the web interface. Set it "
                            "account-wide there instead if you want it on every run")
        p.add_argument("--redact", action="store_true",
                       help="omit serial numbers / UUIDs from the report")
        add_run_dir_flag(p)
        p.add_argument("--cleanup-packages", action="store_true",
                       help="remove packages this run installed when finished")
        p.add_argument("--no-submit", action="store_true",
                       help="do not upload results when the run finishes "
                            "(they stay in the run directory for later)")
        # Every command with --no-submit can submit, so every one of them can name the server it
        # submits to and how to treat its certificate. Both were global-only, so ``alma-certify run
        # --server X`` was an error while ``alma-certify --server X run`` worked; and the wizard,
        # which builds ``run --flag ...``, had no way to put a global in front of the action
        # without making argv[0] a flag - which the root check reads.
        add_server_flag(p)
        p.add_argument("--no-epel", action="store_true",
                       help="do not install epel-release or enable CodeReady "
                            "Builder; tests needing EPEL-only tools will skip")
        # Needed for any non-interactive use on an unsupported distribution: with
        # no terminal to confirm at, the alternative to a flag is a prompt that
        # blocks a CI job forever.
        p.add_argument("--gpu-setup", action="store_true",
                       help="install a present GPU's missing driver or CUDA toolkit, and load an "
                            "installed driver that is not loaded, without asking; for provisioning "
                            "and CI")
        p.add_argument("--no-gpu-setup", action="store_true",
                       help="never install or load anything for a present GPU, and do not ask. The "
                            "GPU checks then skip")
        p.add_argument("--require-gpu", action="store_true",
                       help="fail the run if it does not end with a GPU the checks can use: no "
                            "card found, an install that failed, or a driver that will not load. "
                            "Without this such a run carries on and the GPU checks skip, which is "
                            "right for a person certifying a machine and wrong for CI")
        p.add_argument("--allow-unsupported-os", action="store_true",
                       help="run on a non-AlmaLinux host without confirming; "
                            "results stay local and cannot be submitted")
        add_debug_flag(p)

    def add_select_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--scope", metavar="KIND[,KIND]", help=SCOPE_HELP)
        p.add_argument("--only", help="comma-separated test ids to run")
        p.add_argument("--exclude", help="comma-separated test ids to skip")
        p.add_argument("--category", help="comma-separated categories to run")
        p.add_argument("--peer", help="peer host for network tests (iperf3 server)")

    def add_all_gpus_flag(p: argparse.ArgumentParser) -> None:
        p.add_argument("--all-gpus", action="store_true",
                       help="benchmark every GPU, including identical cards, and submit each as "
                            "its own result. By default one representative per distinct model is "
                            "benchmarked and identical cards are not repeated")

    p = sub.add_parser(
        "collect",
        help="collect hardware inventory for the AlmaLinux hardware survey",
    )
    add_run_flags(p)
    p.set_defaults(func=cmd_collect)

    # An alias for collect. A collect-only run is a survey submission, and "survey" is
    # the word for what it is for; "collect" is kept because it is also the shared
    # first phase of validate/benchmark and long-standing muscle memory.
    p = sub.add_parser(
        "survey",
        help="contribute hardware inventory to the AlmaLinux hardware survey (alias for collect)",
    )
    add_run_flags(p)
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("validate", help="run certification validation tests")
    add_run_flags(p)
    add_select_flags(p)
    p.add_argument("--disk", metavar="DIR", help=DISK_HELP)
    p.add_argument("--interactive", action="store_true",
                   help="include tests that need a person at the machine")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("benchmark", help="run benchmarks")
    add_run_flags(p)
    add_select_flags(p)
    add_all_gpus_flag(p)
    p.add_argument("--dry-run", action="store_true", help="show the plan and exit")
    p.set_defaults(func=cmd_benchmark)

    p = sub.add_parser("run", help="collect + validate + benchmark in one run")
    add_run_flags(p)
    add_select_flags(p)
    add_all_gpus_flag(p)
    p.add_argument("--interactive", action="store_true")
    p.add_argument("--disk", metavar="DIR", help=DISK_HELP)
    p.set_defaults(func=cmd_run_all)

    p = sub.add_parser("resume", help="resume an interrupted run")
    p.add_argument("run_id")
    add_run_dir_flag(p)
    # Resuming a run that went wrong is one of the likeliest times to want the
    # trace, so resume gets the flag too rather than inheriting a stored choice.
    add_debug_flag(p)
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser(
        "setup-gpu",
        help="install the NVIDIA driver and CUDA toolkit needed to certify a GPU",
    )
    p.add_argument("--yes", action="store_true",
                   help="install without asking; required when there is no terminal")
    p.add_argument("--dry-run", action="store_true",
                   help="print the commands that would run and exit")
    p.set_defaults(func=cmd_setup_gpu)

    p = sub.add_parser(
        "tui", help="guided full-screen interface (the default at a terminal)",
    )
    add_run_dir_flag(p)
    add_server_flag(p)
    p.set_defaults(func=cmd_tui)

    p = sub.add_parser("runs", help="list runs in the run directory, newest first")
    add_run_dir_flag(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("list", help="list available tests")
    p.add_argument("--run-type", choices=["validate", "benchmark"])
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("report", help="print a human-readable run summary")
    p.add_argument("run_id")
    add_run_dir_flag(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("bundle", help="package a run for manual upload")
    p.add_argument("run_id")
    p.add_argument("-o", "--output")
    add_run_dir_flag(p)
    p.set_defaults(func=cmd_bundle)

    p = sub.add_parser("register", help="obtain a submission token from Lumina")
    add_server_flag(p)
    p.add_argument(
        "--no-qr", action="store_true",
        help="do not draw the QR code (the URL and code are always shown); the ALMA_CERTIFY_NO_QR "
             "environment variable does the same and also covers a run that has to register",
    )
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("submit", help="submit a run to Lumina")
    p.add_argument("run_id")
    add_server_flag(p)
    p.add_argument("--token", help="use this token instead of the stored one")
    p.add_argument("--pre-release", action="store_true")
    p.add_argument("--publish-after", metavar="YYYY-MM-DD")
    p.add_argument("--support-from-minor", metavar="N", type=int)
    p.add_argument("--anonymous", action="store_true",
                   help="list this run as Anonymous rather than under your username")
    add_run_dir_flag(p)
    p.set_defaults(func=cmd_submit)

    # --config is equally valid before or after the subcommand, so accept it
    # on every one rather than making people remember where it goes.
    for subparser in sub.choices.values():
        subparser.add_argument(
            "--config", default=argparse.SUPPRESS,
            help="path to an alma-certify.conf overriding defaults",
        )

    return parser


# ---------------------------------------------------------------------------


def _require_root(args: argparse.Namespace, raw: Sequence[str]) -> bool:
    """Be root, or become root through sudo. False means say no more and exit.

    One gate on the one funnel, rather than a call at the top of each command. Every command needs
    it: the run directory is 0750 root:root, the token lives in /etc, and the tests read DMI and
    PCI configuration space. It used to be per command, which meant the read-only ones did not have
    it and failed later as a permission error or, worse, as an empty list of runs on a machine full
    of them.

    Asked, not assumed. Re-running itself with more privilege than it was given is not something a
    tool should do unannounced, so the person at the keyboard answers, and the default is yes
    because they typed a command that needs it. With nobody to ask, it refuses and names the fix:
    prompting into a kickstart ``%post`` would hang it forever.
    """
    if elevate.is_root():
        return True
    # --help and --version never reach here; argparse exits during parsing. A bare command with no
    # terminal prints help, which needs nothing.
    if not getattr(args, "func", None) and not _interactive_terminal():
        return True

    retype = ("  sudo alma-certify %s" % " ".join(shlex.quote(arg) for arg in raw)).rstrip()
    print("alma-certify must run as root.", file=sys.stderr)
    if os.environ.get(elevate.ELEVATED_ENV):
        # Elevated once already and still not root. Asking again would loop.
        print("Elevating did not produce root. Run it as root yourself:\n%s" % retype,
              file=sys.stderr)
        return False
    if not elevate.sudo_available():
        print("sudo is not installed here, so run it as root yourself:\n%s" % retype,
              file=sys.stderr)
        return False

    answer = TerminalHooks().confirm("Re-run it under sudo?", default=True)
    if answer is None:
        print("There is no terminal here to ask for a password. Run it as root:\n%s" % retype,
              file=sys.stderr)
        return False
    if not answer:
        print("Not elevating. Run it as root yourself:\n%s" % retype, file=sys.stderr)
        return False

    elevate.elevate(raw)
    # Only reached if the exec failed, which is not a thing sudo does quietly.
    print("could not re-run under sudo. Run it as root yourself:\n%s" % retype, file=sys.stderr)
    return False


class GpuNotUsable(Exception):
    """``--require-gpu`` was given and the run did not end with a GPU the checks can use.

    Its own exception rather than a False return, because the offer already uses False for "the
    operator chose to stop and reboot", which is a decision rather than a fault, and the two must
    not produce the same exit code.
    """


class HostNotSupported(Exception):
    """A run was asked for on something that is not AlmaLinux and the operator declined it.

    An exception rather than a None return, because ``prepare_run`` hands back a run and there is
    no run to hand back: a caller that forgot to check would otherwise carry on with nothing.
    """


def _guard_supported_os(
    args: argparse.Namespace, hooks: Optional[RunHooks] = None,
) -> bool:
    """Confirm an unsupported distribution before a run starts. False means stop.

    Deliberately before ``_start_run``, so declining leaves no half-created run
    directory to wonder about later.

    ``hooks`` routes it to whatever is driving the run. Without that the guided interface skipped
    this entirely - ``prepare_run`` never called it - so a resident run on a RHEL rebuild simply
    started, with nobody told that nothing it produced could be submitted.
    """
    host = hostos.detect()
    if host.is_almalinux:
        return True
    assume_yes = bool(getattr(args, "allow_unsupported_os", False))
    if hooks is None:
        return hostos.confirm(host, assume_yes=assume_yes)
    # The same warning and the same question, through the driver: a resident run has no stderr to
    # show and no stdin to type at.
    warning = io.StringIO()
    hostos.warn(host, out=warning)
    hooks.note(warning.getvalue().rstrip("\n"))
    if assume_yes:
        hooks.note("continuing anyway (--allow-unsupported-os)")
        return True
    answer = hooks.confirm("Run the tests anyway? Results will not be submittable.")
    if answer is None:
        hooks.note("nothing here can answer that. Pass --allow-unsupported-os to run anyway "
                   "(results stay local).")
        return False
    return answer


def _config(args: argparse.Namespace) -> Config:
    config = Config.load(getattr(args, "config", None))
    if getattr(args, "run_dir", None):
        config.set("general", "run_dir", args.run_dir)
    if getattr(args, "peer", None):
        config.set("network", "peer", args.peer)
    if getattr(args, "disk", None):
        config.set("benchmark", "storage_target_dir", args.disk)
    # Precedence: built-in default < config file < $ALMA_CERTIFY_SERVER < --dev < --server.
    # ``--dev`` outranks the environment because it is typed at the command, and a shell that
    # exports the production URL must not quietly win over the flag that asked for staging. An
    # explicit --server outranks --dev, so naming a third server is never ambiguous.
    server = getattr(args, "server", None) or ""
    if not server and getattr(args, "dev", False):
        server = DEV_SERVER
    if not server:
        server = os.environ.get(SERVER_ENV, "").strip()
    if server:
        config.set("general", "server", server)
    if getattr(args, "allow_self_signed", False):
        config.set("general", "allow_self_signed", "true")
    warning = _apply_tls_policy(config)
    if warning:
        print(warning, file=sys.stderr)
    if getattr(args, "all_gpus", False):
        config.set("benchmark", "all_gpus", "true")
    return config


def _apply_tls_policy(config: Config) -> Optional[str]:
    """Tell the HTTP layer whether to verify certificates; the warning to show when it will not.

    Warned every time rather than once in the documentation: not verifying a certificate is the
    kind of setting that gets turned on for an afternoon against a dev server and then stays in a
    config file for a year. Returned rather than printed so a resident run can show it through its
    hooks, where stderr is never seen.
    """
    from .submit import http

    allow = config.getbool("general", "allow_self_signed")
    http.allow_self_signed(allow)
    if not allow:
        return None
    return (
        "warning: TLS certificate verification is off (--allow-self-signed or [general] "
        "allow_self_signed). Any certificate will be accepted for %s."
        % (config.get("general", "server") or "the configured server")
    )


def _preflight_token(state: RunState, config: Config, hooks: RunHooks) -> Optional[str]:
    """Make sure a usable submission token exists before the run starts.

    Takes the whole ``hooks``, not just its log, because authorization may be needed here: a
    resident run shows the device-code prompt and QR as widgets (``hooks.authorize``), a terminal
    run prints them.
    """
    from .submit import auth

    server = config.get("general", "server").strip()
    if not server:
        hooks.log("no Lumina server configured, so results will not be uploaded "
                  "(set --server, $%s, or [general] server; or pass --no-submit)"
                  % SERVER_ENV)
        state.meta["submit"] = False
        state.save()
        return None
    server = server.rstrip("/")
    # Named, every run. The interface carries the globals it was started with into the run it
    # hosts, ``--server`` among them, and nothing anywhere said so - so the only way to find out
    # which catalog a resident run was talking to was to read the source. A tester watching this
    # step take a while has no way to tell a slow server from an ignored flag.
    hooks.log("submitting to %s" % server)
    return auth.ensure_token(
        server, config.get("general", "token_path"), log=hooks.log, hooks=hooks
    )


def _record_submission(state: RunState, server: str) -> Callable[[dict], None]:
    """A callback that writes a successful upload onto the run.

    Nothing recorded this before, so ``alma-certify runs`` could say what a run found and not
    whether anybody had ever received it, and the operator's only record of an upload was a line
    of terminal output. That is the one thing about a run that cannot be recovered by re-reading it:
    the results are on disk, the verdict is in the report, and whether the server has them is
    knowable only from the server.

    **The first success wins.** Lumina is idempotent on the run id, so a re-upload succeeds and
    reports ``duplicate``; the interesting timestamp is when the evidence first landed, not when
    somebody last re-sent it. What a re-upload does update is the URL, in case the run moved.
    """
    def record(body: dict) -> None:
        submission = state.meta.setdefault("submission", {})
        submission.setdefault("at", utcnow_iso())
        submission["server"] = server
        # Whatever the server chose to identify it by. ``web_url`` is what somebody wants months
        # later; ``uuid`` is the fallback for a server that does not send one.
        url = body.get("web_url") or body.get("uuid")
        if url:
            submission["url"] = url
        if body.get("status"):
            # ``draft`` means uploaded and not yet submitted for review, which is a different thing
            # from certified and worth keeping distinct here too.
            submission["status"] = body["status"]
        state.save()

    return record


def _is_survey(state: RunState) -> bool:
    """A survey run: inventory only, contributed to Lumina's hardware survey rather
    than a reviewable certification run. It is the collect run type on its own -
    ``collect`` and its alias ``survey`` produce it; validate/benchmark do not.
    """
    return state.meta.get("run_types") == ["collect"]


def _autosubmit(state: RunState, config: Config, token: Optional[str], hooks: RunHooks) -> None:
    """Upload the finished run. Never changes the run's exit status: the
    verdict is about the hardware, and an upload can always be retried.

    Output goes through ``hooks.out`` so the upload URL and the action-required notice - the whole
    point of a run - land in a resident interface rather than being printed behind it.
    """
    from .submit import auth, client

    retry_hint = "alma-certify submit %s" % state.run_id[:8]
    bundle_hint = "alma-certify bundle %s" % state.run_id[:8]
    if state.meta.get("no_submit_reason") == "unsupported_os":
        # No retry hint: there is nothing to retry. Offering one would suggest the
        # upload merely failed and could be tried again later.
        hooks.out(
            "\nnot submitted: this run was performed on %s, and only AlmaLinux "
            "runs can be submitted." % state.meta.get("host_os_display", "another OS")
        )
        hooks.out("The results are in %s" % state.run_dir)
        hooks.out("Offline bundle:\n  %s" % bundle_hint)
        return
    if state.meta.get("no_submit_reason") == "virtual_machine":
        reason = virt.full_run_reason(state.meta.get("virtualization") or {})
        hooks.out("\nnot submitted: %s." % reason)
        hooks.out("The results are in %s" % state.run_dir)
        hooks.out("Offline bundle:\n  %s" % bundle_hint)
        return
    if not state.meta.get("submit", True):
        hooks.out("not submitted (--no-submit). To upload later:\n  %s" % retry_hint)
        hooks.out("Offline bundle:\n  %s" % bundle_hint)
        return

    server = config.get("general", "server").strip()
    token = token or auth.load_token(config.get("general", "token_path"))
    if not server or not token:
        hooks.out("results were not uploaded (no %s). To upload later:\n  %s"
                  % ("server configured" if not server else "valid token", retry_hint))
        return

    if _is_survey(state):
        hooks.out("\ncontributing inventory to the AlmaLinux hardware survey at %s ..." % server)
        code = client.submit_survey(
            server=server,
            run_dir=state.run_dir,
            token=token,
            token_path=config.get("general", "token_path"),
            on_success=_record_submission(state, server),
            out=hooks.out,
            err=hooks.note,
        )
    else:
        hooks.out("\nsubmitting results to %s ..." % server)
        code = client.submit_run(
            server=server,
            run_dir=state.run_dir,
            token=token,
            token_path=config.get("general", "token_path"),
            on_success=_record_submission(state, server),
            pre_release=state.meta.get("pre_release") or None,
            # ``or None``, like the flag above: sending a false would override the
            # account-wide default on the server, and not passing the flag is not the
            # same as asking to be named.
            anonymous=state.meta.get("anonymous") or None,
            publish_after=state.meta.get("publish_after"),
            support_from_minor=state.meta.get("support_from_minor"),
            out=hooks.out,
            err=hooks.note,
        )
    if code != 0:
        hooks.log("upload failed; the run is saved locally")
        hooks.out("Retry with:\n  %s" % retry_hint)


def _start_run(args: argparse.Namespace, config: Config, run_types: List[str]) -> RunState:
    run_id = str(uuid.uuid4())
    host = hostos.detect()
    meta = {
        "run_types": run_types,
        "target_type": "hardware",
        # What this run is a claim about. Absent or empty means the whole machine, which is what
        # every run before this flag existed said and what most runs still say.
        "claim_scope": _scope(args) or [],
        "hostname": os.uname().nodename,
        "started_at": utcnow_iso(),
        "finished_at": None,
        "pre_release": bool(getattr(args, "pre_release", False)),
        "anonymous": bool(getattr(args, "anonymous", False)),
        "publish_after": getattr(args, "publish_after", None),
        # Kitten runs only. The server ignores it for a run on a shipped release, where the
        # pass already proves the hardware works on something people can install.
        "support_from_minor": getattr(args, "support_from_minor", None),
        "interactive_included": bool(getattr(args, "interactive", False)),
        "redact": bool(getattr(args, "redact", False)),
        "selection": {
            "only": _csv(getattr(args, "only", None)),
            "exclude": _csv(getattr(args, "exclude", None)),
            "categories": _scoped_categories(
                _scope(args), _csv(getattr(args, "category", None))
            ),
            "interactive": bool(getattr(args, "interactive", False)),
        },
        "cleanup_packages": bool(getattr(args, "cleanup_packages", False)),
        "enable_epel": not bool(getattr(args, "no_epel", False)),
        "submit": not bool(getattr(args, "no_submit", False)),
        # The GPU-setup consent, carried on the run so the offer can be made inside the engine
        # (after the submission token is obtained) rather than in the command wrapper before it.
        "gpu_setup": bool(getattr(args, "gpu_setup", False)),
        "no_gpu_setup": bool(getattr(args, "no_gpu_setup", False)),
        "require_gpu": bool(getattr(args, "require_gpu", False)),
        "peer": config.get("network", "peer") or None,
        # Recorded on the run rather than recomputed later, so `resume` and
        # `submit` inherit the decision instead of re-reading whatever host they
        # happen to be invoked on.
        "host_os_id": host.id,
        "host_os_display": host.display,
    }
    if not host.is_almalinux:
        # Offline is not a preference here, so it overrides --no-submit's absence
        # rather than being merged with it. The reason is stored separately so the
        # end-of-run message does not blame a flag nobody passed.
        meta["submit"] = False
        meta["no_submit_reason"] = "unsupported_os"
    # Same shape for a guest. A whole-machine claim from a VM is about the hypervisor; only a
    # component claim (--scope gpu) survives virtualization. virt.py documented this gate and
    # nothing called it, so every such run was uploaded as if it were metal.
    facts = virt.detect()
    meta["virtualization"] = facts
    if meta["submit"] and not meta["claim_scope"] and virt.full_run_reason(facts):
        meta["submit"] = False
        meta["no_submit_reason"] = "virtual_machine"
    return RunState.create(config.get("general", "run_dir"), run_id, meta)


def _csv(value: Optional[str]) -> Optional[List[str]]:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _scope(args: argparse.Namespace) -> Optional[List[str]]:
    """The claim scope, validated. Raises SystemExit on an unusable one.

    Refused here rather than by the server, so an operator finds out before spending an hour on a
    run the catalog will not accept.
    """
    kinds = _csv(getattr(args, "scope", None))
    if not kinds:
        return None
    unknown = [kind for kind in kinds if kind not in SCOPE_KINDS]
    if unknown:
        # Named with the reason, because ``--scope cpu`` used to work and somebody may have it in a
        # script. "Unknown kind" on its own would read as a typo rather than as a decision.
        raise SystemExit(
            "--scope does not accept %s. It accepts: %s.\n"
            "A component claim exists for hardware a virtual machine sees as the real device, "
            "and a "
            "GPU passed through to a guest is the only one that qualifies: a CPU, a NIC, and a "
            "disk are whatever the hypervisor chose to present. Certify those with a whole-machine "
            "run on bare metal instead." % (", ".join(unknown), ", ".join(SCOPE_KINDS))
        )
    # Ordered and deduplicated, so two operators claiming the same thing produce the same report.
    return sorted(set(kinds))


def _scoped_categories(
    scope: Optional[List[str]], explicit: Optional[List[str]]
) -> Optional[List[str]]:
    """The categories to run: whatever was asked for, or the ones the scope implies.

    An explicit ``--category`` wins and is not narrowed. Somebody who passed both means it, and
    silently intersecting the two would produce a run that skipped tests they asked for without
    saying so.
    """
    if explicit or not scope:
        return explicit
    categories = []
    for kind in scope:
        for category in SCOPE_CATEGORIES.get(kind, ()):
            if category not in categories:
                categories.append(category)
    return categories or None


def _enable_debug(state: RunState, ctx=None) -> None:
    """Send a trace of every command to **stderr**.

    stderr, not stdout, for two reasons. ``alma-certify report --json`` writes JSON
    to stdout, so a trace there would corrupt anything piping it; and keeping them
    separate means ``alma-certify validate --debug 2> trace.log`` saves the trace
    while the run's own summary still scrolls past normally.

    Not written to ``alma-certify.log`` either. That file is per-run and small enough
    to read, and raw tool output would bury the run's own narrative in it - use a
    shell redirect if you want the trace on disk.
    """
    label = (lambda: ctx.current_test_id) if ctx is not None else None

    def write(line: str) -> None:
        print(line, file=sys.stderr, flush=True)

    procutil.set_debug(write, label=label)
    if state.meta.get("redact"):
        # Worth saying plainly: --redact is about the *report*, and the trace is
        # raw tool output, so serials and UUIDs it would have stripped are visible
        # here. Anyone about to paste a trace into a bug report should know.
        print(
            "note: --debug output is not redacted; --redact only affects "
            "report.json, so serial numbers may appear in the trace below",
            file=sys.stderr,
        )


def _select_tests(state: RunState, run_type: str) -> List[Any]:
    sel = state.meta.get("selection", {})
    classes = REGISTRY.select(
        run_type,
        only=sel.get("only"),
        exclude=sel.get("exclude"),
        categories=sel.get("categories"),
        interactive=sel.get("interactive", False),
    )
    return [cls() for cls in classes]


def execute(
    state: RunState, config: Config, run_types: List[str], hooks: RunHooks,
    debug: bool = False,
) -> int:
    """Public engine entry: run ``run_types`` for ``state``, reporting through ``hooks``.

    The CLI reaches the same orchestration via ``_execute`` with terminal hooks by default; a
    front-end that hosts the run rather than shelling out (the Textual TUI) builds its own
    ``RunHooks`` and calls this.
    """
    return _execute(state, config, run_types, debug=debug, hooks=hooks)


def prepare_run(
    argv: List[str], hooks: Optional[RunHooks] = None,
) -> Tuple[RunState, Config, List[str]]:
    """Build the run a guided argv describes, without executing it.

    The resident interface calls this and then ``execute(state, config, run_types, hooks)`` to host
    a run in-process. ``argv`` is a full command line - a subcommand plus flags, with any global
    flags the interface was started with carried in front - the same argv the hand-off path runs.
    Rejects an argv that is not one of the runnable subcommands.

    The unsupported-OS check is here rather than in the command functions, because this is the
    other way into a run and it did not have one: the guided interface went straight past a gate
    every CLI run passes through. Raises ``HostNotSupported`` when the operator declines it.
    """
    args = build_parser().parse_args(argv)
    run_types = {
        cmd_collect: ["collect"],
        cmd_validate: ["collect", "validate"],
        cmd_benchmark: ["collect", "benchmark"],
        cmd_run_all: ["collect", "validate", "benchmark"],
    }.get(getattr(args, "func", None))
    if run_types is None:
        raise ValueError("not a runnable command: %s" % " ".join(argv))
    if not _guard_supported_os(args, hooks):
        raise HostNotSupported(hostos.detect().display)
    config = _config(args)
    # _config printed this to stderr, which a resident run never shows. Say it where it is heard.
    if hooks is not None:
        warning = _apply_tls_policy(config)
        if warning:
            hooks.note(warning)
    state = _start_run(args, config, run_types)
    return state, config, run_types


def _execute(
    state: RunState, config: Config, run_types: List[str], debug: bool = False,
    hooks: Optional[RunHooks] = None,
) -> int:
    """Keep the machine awake, then run.

    A wrapper rather than a line inside the engine below, so there is one place that holds the lock
    and one place that releases it however the run ends. A full run is hours: a workstation suspends
    on idle and a laptop suspends when its lid closes, and either one part way through a benchmark
    pass loses the run and can leave a half-written report that looks like a result.
    """
    hooks = hooks or TerminalHooks(state.run_dir)
    # Announced before the call, not after: taking the wake-lock reloads logind, which can block
    # for up to 15s, and without a "now doing" line that wait reads as a hang. The log lines that
    # follow report how it went.
    hooks.status("keeping the machine awake for the run")
    with nosleep.inhibited(log=hooks.log) as inhibitor:
        # Recorded, because "was this machine allowed to sleep during the run" is a question about
        # the conditions the results were produced under, and a reviewer cannot ask the machine
        # later.
        state.meta["sleep_inhibited"] = nosleep.held(inhibitor)
        # The governor is only worth forcing when a benchmark will actually measure speed;
        # validation asks whether the hardware works, not how fast, so a validate-only run leaves
        # the machine's power policy alone. nullcontext keeps the one code path below.
        if "benchmark" in run_types:
            hooks.status("setting the CPU governor to performance")
        governor = (
            cpugov.performance(log=hooks.log) if "benchmark" in run_types
            else contextlib.nullcontext(frozenset())
        )
        with governor as changed:
            # Same reasoning as sleep_inhibited: the governor a benchmark ran under is part of the
            # conditions behind the numbers.
            state.meta["cpu_governor_forced"] = cpugov.forced(changed)
            state.save()
            return _execute_tests(state, config, run_types, debug, hooks=hooks)


def _execute_tests(
    state: RunState, config: Config, run_types: List[str], debug: bool = False,
    hooks: Optional[RunHooks] = None,
) -> int:
    """Shared engine for collect/validate/benchmark/run/resume."""
    hooks = hooks or TerminalHooks(state.run_dir)
    log = hooks.log
    pkg = PackageManager(allow_extra_repos=state.meta.get("enable_epel", True))

    # Installed here, before inventory, so the trace covers the whole run and not
    # only the test loop: inventory collection and package installs shell out too,
    # and "dmidecode returned nothing" is exactly the sort of thing being
    # debugged. Re-installed with a label once the context exists.
    if debug:
        _enable_debug(state)

    # 0. authorization, before any tests: discovering a missing or nearly
    # expired token after a multi-hour benchmark pass would waste the run.
    submit_token = None
    if state.meta.get("submit", True):
        hooks.status("checking your submission token")
        submit_token = _preflight_token(state, config, hooks)

    # 0b. GPU driver/toolkit setup, after the token on purpose. Entering the device code is a quick
    # human step; a driver install is a long one. Doing the install first meant the operator
    # answered the code prompt only after the download finished, by which time they had often walked
    # away and the run sat waiting. So every step that needs a person is front-loaded, quickest
    # first: get the code, then deal with drivers, then the run proceeds unattended. Only where GPU
    # tests will actually run, so a collect-only pass never offers it.
    if any(run_type in ("validate", "benchmark") for run_type in run_types):
        try:
            proceed = _offer_gpu_setup(
                gpu_setup=state.meta.get("gpu_setup", False),
                no_gpu_setup=state.meta.get("no_gpu_setup", False),
                require=state.meta.get("require_gpu", False),
                hooks=hooks,
            )
        except GpuNotUsable as exc:
            # --require-gpu. Without it this is a note and the run carries on to skip the GPU
            # checks, which is right for somebody certifying a machine and useless for CI, where
            # the whole job existed to prove the stack installs.
            log("no usable GPU and --require-gpu was given: %s" % exc)
            # The same facts the report carries, in the log as well. A CI failure is read from the
            # job output, and "the driver would not load" on its own sends somebody looking for a
            # dnf error that is not there: the usual cause is nvidia-smi answering while
            # nvidia_uvm never loaded, which is only visible in these fields.
            for field, value in sorted(gpusetup.load_record().items()):
                if field in ("attempts", "installed_during_run"):
                    for entry in value or []:
                        log("  %s: %s" % (field, entry))
                elif value not in (None, [], {}):
                    log("  %s: %s" % (field, value))
            return EXIT_ERROR
        if not proceed:
            # The driver installed but would not load, and the operator chose to reboot rather than
            # run the rest without the GPU. Stop before the tests so the hours are not spent on a
            # run that was going to miss GPU coverage anyway; a re-run after the reboot gets it.
            log("stopped before the tests so the driver can load after a reboot; run the same "
                "command again afterward for GPU coverage")
            return EXIT_OK

    # A cancel asked for during any of the above. The test loop has checked this between tests
    # since it existed; nothing before it did, so a run cancelled during authorization, a driver
    # install, or inventory carried on to completion with the interface reporting that it was
    # stopping. Checked here rather than inside each step because these are the boundaries where
    # stopping leaves nothing half-done.
    if hooks.should_stop():
        log("stopped before the tests at your request")
        return EXIT_OK

    # 1. inventory (always; cached on resume). Not cheap on a large machine: one smartctl per
    # drive, and a shelf of them adds up to minutes. It gets its own status line for that reason -
    # without one the interface still named the step before it, and an operator watching "checking
    # your submission token" count past ten minutes has no reason to think anything is happening.
    inventory = state.load_inventory()
    if inventory is None:
        hooks.status("collecting hardware inventory")
        log("collecting hardware inventory")
        inventory = inventory_mod.collect_all(
            state.run_dir, redact=state.meta.get("redact", False),
            log=log, status=hooks.status,
        )

    ctx = RunContext(
        config=config,
        run_dir=state.run_dir,
        inventory=inventory,
        pkg=pkg,
        peer=state.meta.get("peer"),
        log=log,
    )
    if debug:
        # Now that there is a context, every traced command can name the test that
        # ran it. Without this the trace is a flat list of commands and working out
        # which test each belonged to means counting.
        _enable_debug(state, ctx)

    # 2. tests
    load_all_tests()
    tests: List[Any] = []
    for run_type in run_types:
        if run_type == "collect":
            continue
        tests.extend(_select_tests(state, run_type))
    if tests:
        plan = [t.id for t in tests]
        if not state.data.get("plan"):
            state.set_plan(plan)
        cooldown = (
            config.getfloat("benchmark", "cooldown_seconds", 8.0)
            if "benchmark" in run_types
            else 0
        )
        runner = Runner(state, ctx, log, cooldown_seconds=cooldown,
                        progress=hooks.progress, should_stop=hooks.should_stop)
        pending = [t for t in tests if not state.is_completed(t.id)]
        # Most benchmark and several validation tools ship in EPEL, and many
        # EPEL packages need CodeReady Builder, so set both up before the
        # first install rather than discovering the gap per-test.
        if state.meta.get("enable_epel", True):
            hooks.status("setting up EPEL and CRB repositories")
            repo_state = pkg.enable_extra_repos(log=log)
            state.meta["repo_setup"] = repo_state
            state.save()
        else:
            log("skipping EPEL/CRB setup (--no-epel)")
        hooks.status("installing packages for the selected tests")
        unavailable = runner.prepare_packages(pending)
        runner.execute(tests, skip_pkg_failed=unavailable)

    # 3. finalize
    state.meta["finished_at"] = utcnow_iso()
    state.save()
    environment = report_mod.capture_environment(
        pkg.newly_installed, enabled_repos=pkg.enabled_repos
    )
    rep = report_mod.assemble(state, inventory, state.results(), environment)
    path = report_mod.write(state, rep)
    if state.meta.get("cleanup_packages"):
        log("removing packages installed by this run")
        pkg.cleanup()
    log("report written: %s" % path)

    # The report is the authority on where the tests actually ran, and it only
    # exists now. Re-checking here catches a run that started on AlmaLinux and
    # finished somewhere else, which `resume` on a different machine does: the
    # decision taken at run start would otherwise still be the one in force.
    if not hostos.report_is_almalinux(rep):
        state.meta["submit"] = False
        state.meta["no_submit_reason"] = "unsupported_os"
        reported = hostos.report_os_id(rep)
        if reported and reported != state.meta.get("host_os_id"):
            state.meta["host_os_id"] = reported
            state.meta["host_os_display"] = reported
        state.save()

    for line in _summary_lines(rep):
        hooks.out(line)
    hooks.out("\nrun id: %s" % state.run_id)

    # 4. upload, unless opted out. Results are already on disk, so a failed
    # upload never loses them - it prints how to retry.
    _autosubmit(state, config, submit_token, hooks)

    statuses = {r["status"] for r in rep["results"]}
    if "error" in statuses:
        return EXIT_ERROR
    if "fail" in statuses:
        return EXIT_FAILURES
    return EXIT_OK


def _print_summary(rep: Dict[str, Any]) -> None:
    for line in _summary_lines(rep):
        print(line)


def _summary_lines(rep: Dict[str, Any]) -> List[str]:
    """The run summary, as lines.

    Lines rather than prints, because the guided interface shows this same summary in a
    pager, and two formatters would drift. The itemized required skips below exist precisely
    because a summary once left something out, and that must not become possible in one place
    and not the other.
    """
    out: List[str] = []
    counts: Dict[str, int] = {}
    for r in rep["results"]:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    # Two elements, not one with a newline in it. ``print`` swallows the difference, so this was
    # invisible until the guided interface drew the same lines into a pager, where one of them wiped
    # the frame border and overprinted the next row. A function that returns lines returns lines.
    out.append("")
    out.append("== summary ==")
    for status in ("pass", "fail", "error", "skip"):
        if counts.get(status):
            out.append("  %-5s %d" % (status, counts[status]))
    for r in rep["results"]:
        if r["status"] in ("fail", "error"):
            out.append("  %s: %s - %s" % (r["status"].upper(), r["id"], r.get("reason") or ""))
    # Skipped *required* checks, itemized with their reasons.
    #
    # Only fail and error were itemized, so a required check that did not run appeared as one digit
    # next to the word "skip" with no id and no reason, under a verdict of PASS. Reported from a
    # real run: three required GPU checks skipped because the suite could not find a compiler that
    # was installed, and nothing on the screen said which three or why.
    #
    # A skip is not a pass. ``verdict`` ignores skips by design, since a machine with no BMC should
    # not fail for having no BMC, and that is right for a *conditional* check. For a required one it
    # is a gap in the evidence, and the reader is the only one who can decide whether it matters.
    missed = [
        r for r in rep["results"]
        if r["run_type"] == "validate"
        and r["status"] == "skip"
        and r.get("severity") == "required"
    ]
    for r in missed:
        out.append("  SKIP: %s - %s" % (r["id"], r.get("reason") or ""))
    verdict = report_mod.verdict(rep)
    if verdict is not None:
        # "test verdict", not "certification verdict". The old wording was the
        # first of three things at the end of a run that read as "you are done" -
        # it announced a *certification* result when all it reports is whether the
        # required tests passed on this machine. Certification needs a reviewer.
        out.append("test verdict: %s" % ("PASS" if verdict else "FAIL"))
        if verdict and missed:
            # Not "the required tests passed", which would be false: some of them did not run.
            out.append(
                "  (%d required check(s) did not run, listed above; nothing failed, but the "
                "evidence has holes in it and a reviewer will see them)" % len(missed)
            )
        elif verdict:
            out.append("  (the required tests passed; certification still needs review)")
    return out


# -- commands ---------------------------------------------------------------


def _gpu_summary() -> dict:
    """The inventory the GPU state is read from, collected fresh.

    Its own collection rather than a stored run's, because this command is the thing somebody
    reaches for *before* a run works, and asking them to produce a report first would be circular.
    """
    import tempfile

    with tempfile.TemporaryDirectory(prefix="alma-certify-gpuscan-") as scratch:
        return {"gpus": inventory_mod.gpu.collect(scratch).get("gpus", [])}


def cmd_setup_gpu(args: argparse.Namespace) -> int:
    """Install what an NVIDIA GPU needs before it can be certified.

    Its own command as well as an offer during a run, because the two are wanted at different times:
    the offer catches somebody who did not know, and the command is what they reach for afterwards,
    what a provisioning script calls, and what they run again after the reboot went wrong.
    """
    host = hostos.detect()
    # Before the inventory, and before anything asks nvidia-smi anything: what is loaded now is the
    # baseline the report compares against, and the asking is itself liable to load it.
    gpusetup.note_starting_state()
    summary = _gpu_summary()
    state = gpusetup.cuda_state(summary)

    if state == gpusetup.STATE_NO_CARD:
        print("no NVIDIA GPU detected, so there is nothing to install for one")
        return EXIT_OK
    if state == gpusetup.STATE_READY:
        print("the NVIDIA driver and CUDA toolkit are both present; nothing to do")
        return EXIT_OK
    if state == gpusetup.STATE_DRIVER_NOT_LOADED:
        return _load_installed_driver(summary, args)

    print(gpusetup.describe(state, summary))
    blocked = gpusetup.install_impossible_reason()
    if blocked:
        print("\nnot installing: %s" % blocked, file=sys.stderr)
        return EXIT_USAGE
    live = gpusetup.on_live_media()
    if live:
        print(
            "\nnote: this is live media - the install and load are for this session only and do "
            "not survive a reboot, and a reboot cannot recover a driver that will not load here. "
            "Attempting anyway.",
            file=sys.stderr,
        )

    major = (host.version_id or "").split(".")[0]
    if not major:
        print(
            "\ncannot tell which release this is, so the right repository for it is unknown",
            file=sys.stderr,
        )
        return EXIT_USAGE
    steps = gpusetup.install_plan(
        state, major=major, is_almalinux=host.is_almalinux,
    )

    print("\nthis would run, as root:")
    for step in steps:
        print("  # %s" % " ".join(step.argv))
    if state == gpusetup.STATE_DRIVER_MISSING:
        # The modprobes belong in the plan too. They were missing from it, so ``--dry-run`` promised
        # four dnf commands and said nothing about inserting modules into the running kernel, which
        # is the part a change-control reviewer or a provisioning author needs to see.
        for step in gpusetup.load_plan():
            print("  # %s" % " ".join(step.argv))
    if args.dry_run:
        return EXIT_OK

    if not _confirm_gpu_setup(assume_yes=args.yes):
        return EXIT_USAGE

    print()
    failed = gpusetup.run_plan(steps)
    gpusetup.note_install(steps, failed)
    if failed is not None:
        print(
            "\nfailed while trying to %s. Nothing further was attempted." % failed.why,
            file=sys.stderr,
        )
        return EXIT_ERROR

    if state == gpusetup.STATE_DRIVER_MISSING:
        # Not asked again. Consent was given for an install whose entire purpose is a working
        # driver, and stopping here to ask permission for the last step of the thing they just
        # agreed to would be pedantry.
        print("\ninstalled. Trying to load it into the kernel that is already running:")
        return _report_load(gpusetup.try_load(), installed=True, live=live)
    print("\ninstalled. No reboot needed; the GPU checks can run now.")
    return EXIT_OK


def _load_installed_driver(summary: dict, args: argparse.Namespace) -> int:
    """The driver is installed and not loaded, so there is nothing to install.

    Its own path because the plan is three modprobes rather than a repository and a download, and
    printing an install plan for a machine that already has the packages would misdescribe what is
    about to happen to it.
    """
    print(gpusetup.describe(gpusetup.STATE_DRIVER_NOT_LOADED, summary))
    blocked = gpusetup.cannot_load_reason()
    if blocked:
        print("\nnot loading it: %s" % blocked, file=sys.stderr)
        return EXIT_USAGE
    print("\nthis would run, as root:")
    for step in gpusetup.load_plan():
        print("  # %s" % " ".join(step.argv))
    if args.dry_run:
        return EXIT_OK
    if not _confirm_gpu_setup(assume_yes=args.yes, action="load"):
        return EXIT_USAGE
    print()
    return _report_load(gpusetup.try_load(), installed=False, live=gpusetup.on_live_media())


def _reboot_hint(attempt: gpusetup.LoadAttempt, *, live: bool) -> str:
    """The trailing advice after a load that did not come up, or "".

    A reboot is named only where it can actually help: never on live media, where a reboot loses
    the session and the install with it, and never where the failure is one a reboot does not fix
    (a rejected module signature, say). Both would send somebody to reboot a machine to no end.
    """
    if not attempt.reboot_may_help:
        return ""
    if live:
        return " On live media a reboot cannot bring it back, so the GPU checks skip this run."
    return " Reboot and run again for GPU coverage."


def _report_load(attempt: gpusetup.LoadAttempt, *, installed: bool, live: bool = False) -> int:
    """Say what came of a load attempt.

    ``EXIT_OK`` whether or not it worked, on purpose. On the install path what was asked for did
    happen, and a reboot is the ordinary second half of installing a kernel module rather than a
    failure of this command; on the load path the machine is no worse than it was. What differs is
    the advice, and the one thing it must not do is say "reboot" where a reboot changes nothing.
    """
    if attempt.ok:
        loaded = ", ".join(attempt.modules) or "no modules reported"
        print(
            "\n%sthe driver is loaded (%s) and answering. The GPU checks can run now."
            % ("installed, and " if installed else "", loaded)
        )
        return EXIT_OK
    print(
        "\n%sthe driver did not come up: %s."
        % ("installed, but " if installed else "", attempt.reason),
        file=sys.stderr,
    )
    if attempt.reboot_may_help and not live:
        print("  # reboot", file=sys.stderr)
    elif attempt.reboot_may_help and live:
        print("  (on live media a reboot cannot recover this; the card is not certified this "
              "session)", file=sys.stderr)
    return EXIT_OK


def _confirm_gpu_setup(
    *, assume_yes: bool, action: str = "install", flag: str = "--yes",
    hooks: Optional[RunHooks] = None,
) -> bool:
    """Ask before changing the machine. False means stop.

    The same three paths as ``hostos.confirm``, and for the same reasons: a prompt that hangs is
    worse than one that refuses, reading EOF as consent would opt somebody in silently, and the
    surprising outcome is the one that has to be typed.

    ``action`` because this now guards two different changes and they are not equivalent: one
    downloads gigabytes from a third party, the other inserts a module that is already on the disk.
    Somebody reading "install this now?" before a modprobe would reasonably decline.

    ``flag`` because the two callers are reached by different flags and this used to name only its
    own. During a run it is ``--gpu-setup``; ``setup-gpu`` is the command with a ``--yes``. Somebody
    in a CI job told to pass a flag that command does not have is left with nothing to try.

    ``hooks`` is whatever is driving this, and the question goes through it rather than to ``input``
    so the TUI can ask it in-interface. Reading stdin here hung a resident run outright: Textual
    owns the terminal, so nothing reached the prompt and nothing showed it.
    """
    hooks = hooks or TerminalHooks()
    gerund = "installing" if action == "install" else "loading"
    if assume_yes:
        hooks.out("%s (%s)" % (gerund, flag))
        return True
    answer = hooks.confirm("%s this now?" % action)
    if answer is None:
        hooks.note(
            "not attached to a terminal, so this cannot be confirmed here. "
            "Pass %s to %s anyway." % (flag, action)
        )
        return False
    if not answer:
        hooks.out("not %s" % gerund)
    return answer


def cmd_collect(args: argparse.Namespace) -> int:
    """Collect hardware inventory and contribute it to the AlmaLinux hardware survey.

    A collect-only run is a survey submission: it is inventory, not certification
    evidence, so it is uploaded to Lumina's survey endpoint rather than the results
    review queue and needs no follow-up from the submitter. ``alma-certify survey`` is
    an alias. (The collect *phase* still runs inside validate/benchmark, which submit
    as certification runs as before.)
    """
    if not _guard_supported_os(args):
        return EXIT_UNSUPPORTED_OS
    config = _config(args)
    state = _start_run(args, config, ["collect"])
    return _execute(state, config, ["collect"], debug=args.debug)


def _stop_to_reboot(
    attempt: gpusetup.LoadAttempt, *, live: bool, hooks: Optional[RunHooks] = None,
) -> bool:
    """After a driver install whose load failed: whether to stop the run so the operator can reboot.

    This is the point a reboot fixes, and a run that plows straight on without the GPU spends the
    hours the tester would rather spend rebooting and re-running - so it pauses here rather than
    gliding past the message where it is easily missed. Only where a reboot could actually help:
    not on live media (a reboot loses the install), and not for a failure a reboot does not change
    (a rejected module signature). And only where there is somebody to ask, since an unattended run
    has nobody to reboot it and must carry on with the message already printed.

    Returns True to stop the run (the operator will reboot), False to continue without the GPU. The
    tester usually wants the reboot, so it is the default.
    """
    if live or not attempt.reboot_may_help:
        return False
    hooks = hooks or TerminalHooks()
    answer = hooks.confirm(
        "Reboot and run again for GPU coverage, or continue this run without the GPU?",
        yes_label="Reboot", no_label="continue", default=True,
    )
    return bool(answer)  # no way to ask (unattended): carry on, message already printed


def _offer_gpu_setup(
    *, gpu_setup: bool = False, no_gpu_setup: bool = False, require: bool = False,
    hooks: Optional[RunHooks] = None,
) -> bool:
    """Ask, before the run, whether to install what a present GPU needs.

    A prompt rather than advice to go and run something else. Sending somebody to ``setup-gpu`` when
    they have already typed a command, on a machine whose card is sitting there unusable, is making
    them do the work of relaying a message to themselves. If the answer is almost always yes, ask.

    Four paths, the ones ``hostos.confirm`` established for the unsupported-OS question, because a
    prompt that hangs is worse than a prompt that refuses:

    - ``--gpu-setup``: install without asking. For provisioning and CI.
    - ``--no-gpu-setup``: do not install and do not ask.
    - Somebody there to ask: ask, defaulting to **no**. Installing a driver is the consequential
      answer, so it is the one that has to be typed. The question goes to whatever is driving the
      run (``hooks``), so under the TUI it is a dialog rather than a prompt into a terminal Textual
      owns - which is what used to hang a resident run here.
    - Nobody there: skip, and name the flags. Prompting into a closed stdin would hang a kickstart
      ``%post`` forever, and reading EOF as consent would opt somebody in silently.

    The prompt says what it will cost, which differs by case and is why this is not one message. A
    missing toolkit needs no reboot and the run can carry straight on. A missing driver cannot help
    this run at all, because its kernel modules cannot load into the kernel already running, so it
    is worth installing and worth being told to reboot and run again. And on AlmaLinux 8 or another
    rebuild it means adding NVIDIA's own repository, which is a third-party change somebody should
    know they are agreeing to.

    Returns True for the run to proceed, and False only when a driver install's load failed and the
    operator chose (at ``_stop_to_reboot``) to stop and reboot rather than run on without the GPU.
    """
    hooks = hooks or TerminalHooks()

    def give_up(why: str) -> bool:
        """Carry on without a GPU, or fail, depending on ``--require-gpu``.

        Every path out of this function that leaves the machine without a usable GPU goes through
        here, so the flag cannot be honored in some of them and forgotten in others. That is how
        the failure it exists to catch stayed green: an install that failed logged a note, returned
        True, and the run went on to skip every GPU check and exit 0.
        """
        if require:
            raise GpuNotUsable(why)
        return True

    if no_gpu_setup:
        return give_up("--no-gpu-setup was given, so nothing was installed")
    hooks.status("checking whether this machine's GPU needs a driver or toolkit")
    gpusetup.note_starting_state()
    try:
        summary = _gpu_summary()
    except Exception:  # noqa: BLE001 - this must never be why a run does not start
        return give_up("could not read this machine's GPU inventory")
    state = gpusetup.cuda_state(summary)
    if state == gpusetup.STATE_NO_CARD:
        return give_up("no GPU was found on this machine")
    if state == gpusetup.STATE_READY:
        return True
    if state == gpusetup.STATE_DRIVER_NOT_LOADED:
        # Handled first and separately: nothing needs installing, and if the load works the machine
        # may still want a toolkit, so the state is asked again rather than assumed.
        state, proceed = _offer_driver_load(gpu_setup, summary, hooks=hooks)
        if not proceed:
            return False
        if state is None:
            return give_up("the installed driver could not be loaded")
        if state == gpusetup.STATE_NO_CARD:
            # The card was there a moment ago and is not now. Nothing to install for it either way,
            # but it is not a machine the GPU checks can run on.
            return give_up("no GPU was found after loading the driver")
        if state == gpusetup.STATE_READY:
            return True
    blocked = gpusetup.install_impossible_reason()
    if blocked:
        # A container or a read-only root: installing and loading genuinely cannot work here, and
        # no modprobe or reboot changes that, so the only useful thing is to say why checks skip.
        # Live media is deliberately NOT here: its overlay takes the install and its running kernel
        # takes the modprobe, so the driver loads for this session and the offer is worth making.
        hooks.note("\nnote: %s" % gpusetup.describe(state, summary))
        hooks.note("      not offering to install it: %s\n" % blocked)
        return give_up("cannot install a GPU driver here: %s" % blocked)
    live = gpusetup.on_live_media()

    host = hostos.detect()
    major = (host.version_id or "").split(".")[0]
    if not major:
        hooks.note(
            "\nnote: %s\n      cannot tell which release this is, so the right repository for "
            "it is unknown.\n" % gpusetup.describe(state, summary)
        )
        return give_up("cannot tell which release this is, so no repository is known")

    steps = gpusetup.install_plan(state, major=major, is_almalinux=host.is_almalinux)
    third_party = not gpusetup.repo_is_configured(major=major, is_almalinux=host.is_almalinux)

    hooks.note("\n%s" % gpusetup.describe(state, summary))
    if live:
        hooks.note(
            "This is live media: the install lands in the session's overlay and the driver loads "
            "into the running kernel, so this run can use it, but nothing here survives a reboot "
            "and a reboot cannot recover a driver that will not load. It may not work; the suite "
            "will try."
        )
    if third_party:
        hooks.note("This adds NVIDIA's own repository, which is not part of AlmaLinux.")
    for step in steps:
        hooks.note("  # %s" % " ".join(step.argv))
    if state == gpusetup.STATE_DRIVER_MISSING:
        for step in gpusetup.load_plan():
            hooks.note("  # %s" % " ".join(step.argv))

    if not _confirm_gpu_setup(assume_yes=gpu_setup, flag="--gpu-setup", hooks=hooks):
        hooks.note("continuing without it; the GPU checks will skip.\n")
        return give_up("the GPU setup was declined")

    hooks.status("installing what the GPU needs; this downloads packages and takes a few minutes")
    failed = gpusetup.run_plan(steps, log=hooks.note)
    gpusetup.note_install(steps, failed)
    if failed is not None:
        hooks.note("could not %s, so the GPU checks will skip.\n" % failed.why)
        return give_up("could not %s" % failed.why)
    if state == gpusetup.STATE_DRIVER_MISSING:
        hooks.note("\ninstalled. Trying to load it into the kernel that is already running:")
        hooks.status("loading the driver into the running kernel")
        attempt = gpusetup.try_load(log=hooks.note)
        if attempt.ok:
            hooks.note(
                "\nthe driver is loaded (%s) and answering, so this run can certify the card "
                "after all.\n" % ", ".join(attempt.modules)
            )
            return True
        # The load failed. Say so, then pause: a reboot usually brings it up, and running the rest
        # of the suite without the GPU wastes the time the tester would spend rebooting.
        hooks.note(
            "\nthe driver did not come up: %s. The GPU checks will skip on this run.%s\n"
            % (attempt.reason, _reboot_hint(attempt, live=live))
        )
        if _stop_to_reboot(attempt, live=live, hooks=hooks):
            hooks.note("stopping so you can reboot; run the same command again afterward for GPU "
                       "coverage.\n")
            return False
        return give_up("the driver installed but would not load: %s" % attempt.reason)
    else:
        hooks.note("\ninstalled. The GPU checks can run now.\n")
    return True


def _offer_driver_load(
    gpu_setup: bool, summary: dict, *, hooks: Optional[RunHooks] = None,
) -> Tuple[Optional[str], bool]:
    """Ask, before the run, whether to load a driver that is installed and not in the kernel.

    Asked rather than just done, because inserting a module changes the running kernel and this
    suite does not change a machine it was pointed at without being told to. The same two flags
    cover it as the install offer: somebody who passed ``--gpu-setup`` to have a driver installed
    unattended wants it loaded too, and ``--no-gpu-setup`` means leave the machine alone. Nothing is
    downloaded and no repository touched, so there is nothing to warn about beyond the modprobes.

    Returns ``(state_after, proceed)``. ``state_after`` is the machine's cuda_state once the driver
    is up, so the caller can go on to offer a toolkit if that is now what is missing, or None if the
    GPU is still no use this run. ``proceed`` is False only when the load failed and the operator
    chose to stop and reboot - the canonical "installed earlier, never rebooted" case, where a
    reboot is exactly the fix.
    """
    hooks = hooks or TerminalHooks()
    text = gpusetup.describe(gpusetup.STATE_DRIVER_NOT_LOADED, summary)
    blocked = gpusetup.cannot_load_reason()
    if blocked:
        hooks.note("\nnote: %s\n      not trying to load it: %s\n" % (text, blocked))
        return None, True
    hooks.note("\n%s" % text)
    for step in gpusetup.load_plan():
        hooks.note("  # %s" % " ".join(step.argv))
    if not _confirm_gpu_setup(assume_yes=gpu_setup, action="load", flag="--gpu-setup",
                              hooks=hooks):
        hooks.note("continuing without it; the GPU checks will skip.\n")
        return None, True
    hooks.status("loading the driver into the running kernel")
    attempt = gpusetup.try_load(log=hooks.note)
    if not attempt.ok:
        live = gpusetup.on_live_media()
        hooks.note(
            "\nthe driver did not come up: %s. The GPU checks will skip on this run.%s\n"
            % (attempt.reason, _reboot_hint(attempt, live=live))
        )
        if _stop_to_reboot(attempt, live=live, hooks=hooks):
            hooks.note("stopping so you can reboot; run the same command again afterward for GPU "
                       "coverage.\n")
            return None, False
        return None, True
    hooks.note("\nthe driver is loaded (%s) and answering.\n" % ", ".join(attempt.modules))
    return gpusetup.cuda_state(summary), True


def cmd_validate(args: argparse.Namespace) -> int:
    if not _guard_supported_os(args):
        return EXIT_UNSUPPORTED_OS
    config = _config(args)
    state = _start_run(args, config, ["collect", "validate"])
    # The GPU-setup offer is inside _execute, after the submission token, so the quick human step
    # (the device code) comes before the slow one (a driver install). See _execute_tests step 0b.
    return _execute(state, config, ["collect", "validate"], debug=args.debug)


def cmd_benchmark(args: argparse.Namespace) -> int:
    config = _config(args)
    if args.dry_run:
        load_all_tests()
        # The same rule as a real run, so ``--dry-run`` prints the plan that would actually
        # execute rather than the one before the scope narrowed it.
        state_meta_sel = {
            "only": _csv(args.only), "exclude": _csv(args.exclude),
            "categories": _scoped_categories(_scope(args), _csv(args.category)),
        }
        classes = REGISTRY.select("benchmark", only=state_meta_sel["only"],
                                  exclude=state_meta_sel["exclude"],
                                  categories=state_meta_sel["categories"])
        for cls in classes:
            print("%-40s %s" % (cls.id, cls.category))
        return EXIT_OK
    # After --dry-run, which only prints the plan: nothing runs, so there is
    # nothing to warn about and no reason to make it un-runnable off AlmaLinux.
    if not _guard_supported_os(args):
        return EXIT_UNSUPPORTED_OS
    state = _start_run(args, config, ["collect", "benchmark"])
    # The GPU-setup offer (which a benchmark run needs most: every GPU benchmark wants a vendor
    # OpenCL runtime or the CUDA toolkit) is inside _execute, after the submission token, so the
    # code prompt precedes a long driver install. See _execute_tests step 0b.
    return _execute(state, config, ["collect", "benchmark"], debug=args.debug)


def cmd_run_all(args: argparse.Namespace) -> int:
    if not _guard_supported_os(args):
        return EXIT_UNSUPPORTED_OS
    config = _config(args)
    run_types = ["collect", "validate", "benchmark"]
    state = _start_run(args, config, run_types)
    # The GPU-setup offer is inside _execute, after the submission token, so the quick human step
    # (the device code) comes before the slow one (a driver install). See _execute_tests step 0b.
    return _execute(state, config, run_types, debug=args.debug)


def cmd_resume(args: argparse.Namespace) -> int:
    config = _config(args)
    state = RunState.find(config.get("general", "run_dir"), args.run_id)
    state.mark_resumed()
    if state.meta.get("peer"):
        config.set("network", "peer", state.meta["peer"])
    return _execute(state, config, state.meta.get("run_types", []),
                    debug=args.debug)


def cmd_list(args: argparse.Namespace) -> int:
    load_all_tests()
    rows = []
    for cls in REGISTRY.all():
        if args.run_type and cls.run_type != args.run_type:
            continue
        rows.append(
            {
                "id": cls.id,
                "run_type": cls.run_type,
                "category": cls.category,
                "severity": cls.severity if cls.run_type == "validate" else None,
                "interactive": cls.interactive,
            }
        )
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            flags = "I" if row["interactive"] else "-"
            print(
                "%-44s %-9s %-10s %-13s %s"
                % (row["id"], row["run_type"], row["category"], row["severity"] or "", flags)
            )
    return EXIT_OK


def _read_report(state: RunState) -> Optional[Dict[str, Any]]:
    """A finished run's report, or None if it never got that far.

    Missing is an ordinary state, not an error: a run interrupted partway through
    has a state directory and no report yet.
    """
    path = os.path.join(state.run_dir, "report.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _load_run(
    args: argparse.Namespace, config: Config,
) -> Tuple[RunState, Dict[str, Any]]:
    """The run's journal and its report together.

    Both, because they answer different questions and ``report`` needs one of each: the report is
    what the tests found, and only the journal knows whether anybody has received it. The submission
    record deliberately lives in the journal and not in the report - the bundle is built *from* the
    report, so writing the upload into the thing being uploaded would be circular and would change
    the hash of the evidence after the fact.
    """
    state = RunState.find(config.get("general", "run_dir"), args.run_id)
    path = os.path.join(state.run_dir, "report.json")
    with open(path, encoding="utf-8") as fh:
        return state, json.load(fh)


def _interactive_terminal() -> bool:
    """Whether there is a person at a terminal to draw for.

    Both streams, the same rule ``hostos.confirm`` and the GPU offer already follow. Output alone
    being a tty is not enough: ``alma-certify | tee`` has a terminal on stdin and a pipe on
    stdout, and a full-screen interface drawn into a pipe is nobody's idea of a log.
    """
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError, OSError):
        # Not a terminal, by every route there is to not having one: ``None`` under pythonw and some
        # supervisors, which was an AttributeError and exit 1 where the contract says help and exit
        # 3; a closed stream, which is ValueError; and a detached one, which is OSError.
        return False


def _select_tui():
    """Import the guided interface, or say why it is unavailable.

    Returns ``(module, None)`` when the interface imports and can draw here, else
    ``(None, reason)``. The interface and its vendored Textual stack ship in the
    ``alma-certify-tui`` subpackage; when it is not installed, importing ``alma_certify_tui``
    fails and there is no full-screen interface, only the plain commands. Kept a lazy import so the
    base CLI never pulls Textual in on an ordinary, non-interface invocation.
    """
    try:
        import alma_certify_tui as frontend
    except Exception as exc:  # noqa: BLE001 - an unimportable interface is just unavailable
        return None, ("the guided interface is not installed (%s).\n"
                      "Install it with: dnf install alma-certify-tui" % exc)
    blocked = frontend.unavailable_reason()
    if blocked:
        return None, blocked
    return frontend, None


def cmd_tui(args: argparse.Namespace) -> int:
    """The guided interface: an application that configures and, by default, hosts the run.

    It builds the same command the CLI takes and either runs it resident (streaming progress into
    the interface) or, if the reader chooses, hands it off to run in plain output. It ships in the
    ``alma-certify-tui`` subpackage (Textual); without it there is no full-screen interface and this
    prints the plain commands to use instead.
    """
    if not _interactive_terminal():
        print(
            "the guided interface needs a terminal on both stdin and stdout.\n"
            "Try `alma-certify --help`, or `alma-certify run` to certify this machine.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    frontend, blocked = _select_tui()
    if frontend is None:
        # Named, and then something useful anyway. A console this ncurses has no description for is
        # a real thing on s390x, and a traceback would be a poor answer to it.
        print("cannot draw a full-screen interface here: %s\n" % blocked, file=sys.stderr)
        print(
            "The commands the interface would have run for you:\n"
            "  alma-certify validate          certification checks\n"
            "  alma-certify benchmark         performance figures\n"
            "  alma-certify run               both, in one run\n"
            "  alma-certify runs              what has run on this machine already"
        )
        return EXIT_USAGE

    config = _config(args)
    host = hostos.detect()

    def report_lines(run_id: str) -> List[str]:
        try:
            state, rep = _load_run(argparse.Namespace(run_id=run_id), config)
        except (OSError, ValueError, KeyError) as exc:
            return ["could not read that run's report: %s" % exc]
        run = rep.get("run") or {}
        head = [
            "run %s (%s) on %s" % (run.get("run_id", run_id),
                                   ", ".join(run.get("run_types") or []),
                                   run.get("hostname", "?")),
            "suite %s, started %s" % (run.get("suite_version", "?"), run.get("started_at", "?")),
        ]
        return head + _submission_line(state).split("\n") + _summary_lines(rep)

    def runs():
        """The run rows, or None if the directory is there and cannot be read.

        The two are different answers and the wizard said the same thing for both: a reader without
        root was told "there are no runs on this machine yet" on a machine full of them, because
        ``os.listdir`` raised PermissionError and an empty list was all the screen could show.
        """
        try:
            return _run_rows(config.get("general", "run_dir"))
        except OSError:
            return None

    # The globals this was started with, carried into whatever the interface runs or hands off, so a
    # resumed or resident run looks in the same run dir it listed from. Without them "Resume it"
    # on a run it had just shown ended in "run not found".
    carried: List[str] = []
    for flag in ("config", "server", "run_dir"):
        value = getattr(args, flag, None)
        if value:
            carried += ["--" + flag.replace("_", "-"), str(value)]
    # Carried but deliberately not offered as a control. Accepting an unverified certificate
    # is a developer's flag for a dev or staging catalog, and a checkbox for it in a guided
    # interface is an invitation to turn off certificate checking without meaning to. Typing
    # it on the command line is the point: ``alma-certify --allow-self-signed tui`` still works.
    if getattr(args, "allow_self_signed", False):
        carried.append("--allow-self-signed")
    # Same reasoning for --dev: which catalog to submit to is not a run choice, it is who you are.
    if getattr(args, "dev", False):
        carried.append("--dev")

    result = frontend.start(
        runs=runs,
        report_lines=report_lines,
        version=__version__,
        host=host.display,
        carried=carried,
    )
    if result is None:
        return EXIT_OK
    if isinstance(result, int):
        # A resident run: the interface hosted it in-process and this is its exit code.
        return result

    # A hand-off: the interface built this command and the reader chose to run it in plain output.
    # Printed first so the command shown is the command run, and copyable into a script.
    argv = carried + list(result)
    print("$ alma-certify %s" % " ".join(argv))
    print()
    return main(argv)


def cmd_runs(args: argparse.Namespace) -> int:
    """List what is in the run directory.

    Every other command that takes a run takes a UUID - ``report``, ``resume``, ``bundle``,
    ``submit`` - and nothing printed one after the run that made it had scrolled away. The id is
    announced once when a run starts and then only exists in a directory name, so the answer to
    "which run was that" was ``ls /var/lib/alma-certify/runs``, and the answer to "which of those is
    the one that failed" was to open each state.json.

    Read from ``state.json`` rather than ``report.json``, because the runs somebody is looking for
    are disproportionately the ones that did not finish, and an unfinished run has no report.
    """
    config = _config(args)
    base = config.get("general", "run_dir")
    try:
        rows = _run_rows(base)
    except OSError as exc:
        print("cannot read the run directory %s: %s" % (base, exc), file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        print(json.dumps(rows, indent=2))
        return EXIT_OK
    if not rows:
        print("no runs in %s" % base)
        return EXIT_OK
    # Fixed widths, sized for the longest real value in each column rather than for these three
    # headings: ``collect,validate,benchmark`` is what a plain ``alma-certify run`` produces and
    # it is 26 characters, so a narrower column pushed every following field out of line on the
    # commonest row of all.
    row_format = "%-9s %-17s %-26s %-9s %-11s %s"
    print(row_format % ("RUN", "STARTED", "TYPES", "CLAIM", "SUBMITTED", "RESULT"))
    for row in rows:
        print(row_format % (
            row["short_id"], _short_time(row["started_at"]),
            ",".join(row["run_types"]) or "?",
            ",".join(row["claim_scope"]) or "machine",
            # A date rather than a tick: "did this ever reach the server" and "when" are the same
            # question, and a run submitted before a listing changed hands is worth dating.
            _short_time((row["submission"] or {}).get("at"))[:10] if row["submission"] else "no",
            row["summary"],
        ))
    return EXIT_OK


def _short_time(stamp: Optional[str]) -> str:
    """``2026-08-16T18:22:07Z`` as ``2026-08-16 18:22``, for a column.

    Minutes are enough to tell one run from another by eye, which is what this list is for, and
    the exact value is in ``--json``. Sliced rather than parsed: the only producer is
    ``utcnow_iso``, and anything unlike its output is printed as it came, so a reader sees why.
    """
    if not stamp:
        return "?"
    if len(stamp) >= 16 and stamp[10] == "T":
        return stamp[:10] + " " + stamp[11:16]
    return stamp


def _run_rows(base: str) -> List[Dict[str, Any]]:
    """One row per run directory, newest first.

    A directory that cannot be read is listed rather than skipped, and never raises. A run killed
    between ``makedirs`` and the first ``save`` has no state.json at all, and that is the kind of
    run somebody is looking for when they reach for this.
    """
    if not os.path.isdir(base):
        return []
    rows = []
    for name in os.listdir(base):
        state_path = os.path.join(base, name, "state.json")
        if not os.path.isfile(state_path):
            continue
        try:
            with open(state_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            rows.append({
                "run_id": name, "short_id": name[:8], "started_at": None, "run_types": [],
                "claim_scope": [], "hostname": None, "finished": False,
                "finished_at": None,
                # Every key the normal row has. A caller that reads ``reported`` on this one used to
                # get a KeyError, and the run it happened on was by definition the broken one
                # somebody was looking for.
                "reported": os.path.isfile(os.path.join(base, name, "report.json")),
                "counts": {}, "submission": None, "summary": "unreadable: %s" % exc,
            })
            continue
        meta = data.get("meta") or {}
        completed = data.get("completed") or {}
        counts: Dict[str, int] = {}
        for result in completed.values():
            status = (result or {}).get("status") or "?"
            counts[status] = counts.get(status, 0) + 1
        rows.append({
            "run_id": data.get("run_id") or name,
            # What ``RunState.find`` accepts, so a reader can paste this straight into the next
            # command instead of retyping a UUID.
            "short_id": (data.get("run_id") or name)[:8],
            "started_at": meta.get("started_at"),
            "finished_at": meta.get("finished_at"),
            "run_types": meta.get("run_types") or [],
            "claim_scope": meta.get("claim_scope") or [],
            "hostname": meta.get("hostname"),
            "finished": bool(meta.get("finished_at")),
            "reported": os.path.isfile(os.path.join(base, name, "report.json")),
            "counts": counts,
            # Whether the server has this run, which is the one fact about it that cannot be
            # recovered by re-reading the run directory.
            "submission": meta.get("submission") or None,
            "summary": _run_summary_line(meta, data.get("plan") or [], counts),
        })
    # Newest first, and never on the directory name: a UUID sorts randomly, so the run somebody
    # just made would appear anywhere. Unstarted runs sort last rather than crashing the sort.
    rows.sort(key=lambda row: row["started_at"] or "", reverse=True)
    return rows


def _run_summary_line(meta: Dict[str, Any], plan: List[str], counts: Dict[str, int]) -> str:
    """What became of a run, in a few words.

    Whether it finished comes first, because it decides what the counts mean: eight passes out of a
    plan of forty is not a machine that passed, and the old way of finding that out was to notice
    that ``report.json`` was missing.
    """
    tallies = ", ".join(
        "%d %s" % (counts[status], status)
        for status in ("pass", "fail", "error", "skip")
        if counts.get(status)
    )
    done = sum(counts.values())
    if not meta.get("finished_at"):
        return "unfinished, %d of %d tests%s" % (done, len(plan) or done,
                                                 " (%s)" % tallies if tallies else "")
    return tallies or "no tests ran"


def cmd_report(args: argparse.Namespace) -> int:
    config = _config(args)
    state, rep = _load_run(args, config)
    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True))
    else:
        run = rep["run"]
        print("run %s (%s) on %s" % (run["run_id"], ", ".join(run["run_types"]),
                                     run["hostname"]))
        print("suite %s, started %s" % (run["suite_version"], run["started_at"]))
        print(_submission_line(state))
        _print_summary(rep)
    return EXIT_OK


def _submission_line(state: RunState) -> str:
    """Whether the server has this run, in one line.

    Named on the report as well as in the listing, because this is the page somebody opens when they
    are deciding whether to send it, and "not submitted" is the answer they are looking for.
    """
    submission = state.meta.get("submission") or {}
    if not submission.get("at"):
        return "not submitted. To upload:\n  alma-certify submit %s" % state.run_id[:8]
    where = submission.get("url") or submission.get("server") or "the server"
    line = "submitted %s to %s" % (submission["at"], where)
    if submission.get("status") == "draft":
        # The distinction ``print_outcome`` exists to make: uploaded is not submitted for review,
        # and a reader here is owed the same warning rather than a word that sounds finished.
        line += "\n  uploaded, and not yet submitted for review: open the link and finish it"
    return line


def cmd_bundle(args: argparse.Namespace) -> int:
    config = _config(args)
    state = RunState.find(config.get("general", "run_dir"), args.run_id)
    path = bundle_mod.bundle_run(state.run_dir, args.output)
    print(path)
    return EXIT_OK


def _resolve_server(config: Config) -> Optional[str]:
    """Return the configured Lumina URL, or None after explaining how to set it."""
    server = config.get("general", "server").strip()
    if server:
        if not server.startswith(("http://", "https://")):
            print(
                "server %r must start with http:// or https://" % server,
                file=sys.stderr,
            )
            return None
        return server.rstrip("/")
    print(
        "No Lumina server configured. Set one of:\n"
        "  alma-certify --server https://lumina.example.org <command>\n"
        "  export %s=https://lumina.example.org\n"
        "  [general] server = ... in %s" % (SERVER_ENV, DEFAULT_CONFIG_PATH),
        file=sys.stderr,
    )
    return None


def cmd_register(args: argparse.Namespace) -> int:
    from .submit import auth

    config = _config(args)
    server = _resolve_server(config)
    if server is None:
        return EXIT_USAGE
    return auth.register(
        server, config.get("general", "token_path"), no_qr=getattr(args, "no_qr", False)
    )


def _refuse_unsupported_submit(state: RunState) -> Optional[int]:
    """Block uploading a run that was not performed on AlmaLinux.

    Judged from the run's **own report**, not from ``hostos.detect()``. A bundle
    made on one machine is often submitted from another, and checking the local
    host would let an unsupported run through simply by moving it - while also
    blocking a perfectly good AlmaLinux run that someone chose to upload from
    their laptop.

    Falls back to the run's recorded ``host_os_id`` when a report has not been
    written yet, which is the case for a run that was interrupted before it
    finished.
    """
    report = _read_report(state)
    if report:
        os_id = hostos.report_os_id(report)
        display = os_id or "an unidentified operating system"
    else:
        os_id = str(state.meta.get("host_os_id") or "")
        display = state.meta.get("host_os_display") or os_id or "an unidentified OS"
    if os_id == hostos.ALMALINUX_ID:
        return None
    print(
        "This run was performed on %s.\n"
        "Only AlmaLinux runs can be submitted: the catalog records what works on\n"
        "AlmaLinux, and a result from another distribution is not evidence about\n"
        "AlmaLinux.\n"
        "\n"
        "The results stay on disk, and `alma-certify bundle %s` still writes an\n"
        "offline bundle you can keep or share."
        % (display, state.run_id[:8]),
        file=sys.stderr,
    )
    return EXIT_UNSUPPORTED_OS


def _refuse_virtual_submit(state: RunState) -> Optional[int]:
    """Block uploading a whole-machine claim made inside a guest.

    Judged from the report's own recorded facts, as the OS refusal is, so a bundle moved to another
    machine is judged by where it ran. A scoped run (``--scope gpu``) is the one claim a guest can
    make, and passes.
    """
    report = _read_report(state)
    if report:
        scope = (report.get("run") or {}).get("claim_scope") or []
        facts = (report.get("environment") or {}).get("virtualization") or {}
    else:
        scope = state.meta.get("claim_scope") or []
        facts = state.meta.get("virtualization") or {}
    if scope or not facts:
        return None
    reason = virt.full_run_reason(facts)
    if reason is None:
        return None
    print(
        "This run was performed inside a virtual machine: %s.\n"
        "The results stay on disk, and `alma-certify bundle %s` still writes an\n"
        "offline bundle you can keep or share." % (reason, state.run_id[:8]),
        file=sys.stderr,
    )
    return EXIT_UNSUPPORTED_OS


def cmd_submit(args: argparse.Namespace) -> int:
    from .submit import client

    config = _config(args)
    server = _resolve_server(config)
    if server is None:
        return EXIT_USAGE
    state = RunState.find(config.get("general", "run_dir"), args.run_id)
    refusal = _refuse_unsupported_submit(state) or _refuse_virtual_submit(state)
    if refusal is not None:
        return refusal
    if _is_survey(state):
        # A survey run goes to the survey endpoint, not the results queue - the same
        # routing autosubmit uses, so re-submitting a stored survey run lands correctly.
        return client.submit_survey(
            server=server,
            run_dir=state.run_dir,
            token=args.token,
            token_path=config.get("general", "token_path"),
            on_success=_record_submission(state, server),
        )
    return client.submit_run(
        server=server,
        run_dir=state.run_dir,
        token=args.token,
        token_path=config.get("general", "token_path"),
        on_success=_record_submission(state, server),
        pre_release=args.pre_release or None,
        # The flag on this command, or the one the run was started with: a run stored
        # for later upload keeps the answer given when it ran.
        anonymous=(
            getattr(args, "anonymous", False) or state.meta.get("anonymous")
        ) or None,
        publish_after=args.publish_after,
        # From the flag if given here, else whatever the run was started with.
        support_from_minor=(
            args.support_from_minor
            if getattr(args, "support_from_minor", None) is not None
            else state.meta.get("support_from_minor")
        ),
    )
