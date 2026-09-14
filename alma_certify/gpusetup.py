"""Offer to install the NVIDIA driver and CUDA toolkit when a card is present and they are not.

Three of the four NVIDIA validation checks need a toolchain the suite does not ship, and until now
the only thing it did about that was skip and name the packages. That is a poor answer on a machine
somebody has just brought up specifically to certify a card: they have the hardware, they want the
result, and the missing piece is four commands away.

**Consent is the whole design.** The suite has always refused to add NVIDIA's repository on its own
initiative, and that has not changed: adding a third-party repository to a machine somebody pointed
this at, without asking, would be doing something they did not ask for. Offering and asking is a
different act. So nothing here runs without either a terminal answering yes or an explicit flag, and
in neither case does it happen as a side effect of a validation run that was asked for.

**What gets offered depends on what is missing**, because the cases are not alike:

- The **toolkit** alone is missing, and the driver already answers. Nothing needs to reboot: `nvcc`
  appears immediately and the run that prompted can carry straight on to certify the card.
- The **driver** is missing. Installing it is worth doing, and then it is worth *trying* to bring it
  up in the kernel that is already running rather than assuming a reboot: see `try_load`. Sometimes
  that works and the run carries on; sometimes it cannot and a reboot is the answer. Which one it is
  is a question the machine can be asked, so it is asked instead of guessed.
- The **driver is installed and simply not loaded**, which is the state a machine is in when
  somebody installed it earlier and never rebooted. Nothing needs installing at all here; the same
  attempt is the whole fix, and until now the suite called this state ready and then failed every
  GPU check with a driver error.

**Some hosts cannot be changed at all**, and offering there wastes the operator's time and leaves
them worse off. Live media has a root that vanishes on reboot, and a container has somebody else's
kernel, so a module and a reboot both mean nothing. Those are detected and declined with a reason
rather than attempted.
"""

from __future__ import annotations

import os
import platform
import re
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import procutil

# The states a machine can be in, from this feature's point of view.
STATE_READY = "ready"
STATE_TOOLKIT_MISSING = "toolkit-missing"
STATE_DRIVER_MISSING = "driver-missing"
STATE_DRIVER_NOT_LOADED = "driver-not-loaded"
STATE_NO_CARD = "no-card"


def cuda_state(summary: Dict[str, Any]) -> str:
    """What is missing, if anything, for this machine to certify its NVIDIA GPU.

    Ordered by how much has to happen next, because that is what the offer is about: no card at all,
    then a driver that is not installed, then one installed but not loaded, then a toolkit that
    cannot build.

    **``nvidia-smi`` existing is not the same as the driver working**, which is what this used to
    assume. The tool is userspace and ships with the driver packages, so it is present on a machine
    that installed the driver and has not rebooted, while the kernel module is nowhere. That machine
    was called ready and then failed all four GPU checks with an error about not being able to talk
    to the driver. So the driver is checked rather than the tool merely found, and a machine in that
    state gets a state of its own, because the fix for it is not an install.
    """
    from .validate.nvidia import find_nvcc

    if not hwquery_nvidia(summary):
        return STATE_NO_CARD
    if procutil.find_tool("nvidia-smi") is None:
        return STATE_DRIVER_MISSING
    if "nvidia" not in loaded_nvidia_modules():
        # Read from /proc/modules **before** anything runs nvidia-smi, which is not only cheaper but
        # the only order with no side effect. NVML forks setuid ``nvidia-modprobe`` when it cannot
        # reach the driver, and that runs ``modprobe nvidia`` and creates the device nodes. So as
        # root, asking nvidia-smi whether the driver is up is liable to make it up: the state would
        # come back ready, having loaded a module into somebody's kernel to find out, with no offer
        # and nobody's consent. This way that only happens after somebody says yes.
        return STATE_DRIVER_NOT_LOADED
    if not smi_check()[0]:
        # Loaded and still not answering. A different machine from the one above, and the same fix
        # is worth trying, since nvidia_uvm may be what is missing.
        return STATE_DRIVER_NOT_LOADED
    if find_nvcc() is None:
        return STATE_TOOLKIT_MISSING
    return STATE_READY


def hwquery_nvidia(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The NVIDIA GPUs the inventory reports.

    Imported through the validation module rather than reimplemented, because "which of these is an
    NVIDIA card" has exactly one right answer and this project has already got it wrong once by
    having two.
    """
    from .validate.nvidia import nvidia_gpus

    return nvidia_gpus(summary)


# --- hosts that cannot be changed -------------------------------------------------------


def on_live_media() -> bool:
    """Whether this looks like a booted live image, by the signals Anaconda's live media set.

    Its own predicate because live media is not a reason to refuse the driver, only to caveat it: a
    live ISO's root is a writable overlay that takes the install, and the running kernel takes the
    modprobe, so the driver loads for this session's run - which is the whole of what a one-shot
    certification run needs. Nothing survives the reboot, but a run does not need it to. Refusing
    here was a regression; the modprobe is exactly what live media can do.
    """
    cmdline = procutil.read_file("/proc/cmdline") or ""
    if "rd.live.image" in cmdline or "root=live:" in cmdline:
        return True
    return os.path.isdir("/run/initramfs/live")


def read_only_root_reason() -> Optional[str]:
    """Why nothing can be installed on this root, or None. The general immutable/image case."""
    root = _root_mount()
    if root and "ro" in root:
        return "the root filesystem is mounted read-only, so nothing can be installed on it"
    return None


def install_impossible_reason() -> Optional[str]:
    """Why installing and loading the driver cannot work here at all, or None if it can be tried.

    Deliberately narrower than ``live_media_reason``: live media is impermanent, not impossible, so
    it is absent here. What is genuinely impossible is a container - the kernel is the host's, so a
    module cannot be loaded into it - and a read-only root, which takes no writes. Neither a
    modprobe nor a reboot changes either, so these are the only cases where the offer is not made.
    """
    return foreign_kernel_reason() or read_only_root_reason()


def live_media_reason() -> Optional[str]:
    """Why this host cannot usefully install anything, or None if it can.

    The union it always was, for a caller that wants a single reason string: live media, a
    container, or a read-only root. A caller that means to *attempt* on live media (the driver
    loads there for this session) uses ``install_impossible_reason`` and ``on_live_media`` instead,
    since only the latter two of those three are genuine dead ends.
    """
    if on_live_media():
        return (
            "this looks like live media, so an installed package would not survive the reboot the "
            "driver needs"
        )
    return install_impossible_reason()


def foreign_kernel_reason() -> Optional[str]:
    """Why the running kernel is not this host's to change, or None if it is.

    Deliberately narrower than ``live_media_reason``, because the two questions have different
    answers and conflating them would refuse the one case where loading a module is the *only* thing
    that could work. Live media and a read-only root stop an install from being worth doing; neither
    stops a module already on disk from being loaded into the kernel that is running, and on live
    media a modprobe is the whole of what is available, since nothing installed there survives.

    A container is different in kind: the kernel is the host's, so there is nothing here to load
    into and nothing to reboot.
    """
    for marker in _CONTAINER_MARKERS:
        if os.path.exists(marker):
            return _CONTAINER
    # containerd and CRI-O write none of those files, and a privileged GPU pod on Kubernetes is the
    # container this suite is most likely to meet. systemd sets this for anything it starts inside
    # one, and the runtimes set it for themselves.
    if "container=" in (procutil.read_file("/proc/1/environ") or ""):
        return _CONTAINER
    return None


# A constant rather than a literal in the loop, so a test can empty it. The alternative is patching
# ``os.path.exists``, which patches it for everything, pytest's own traceback machinery
# included, and that has already brought one run down with an INTERNALERROR.
_CONTAINER_MARKERS = ("/run/.containerenv", "/.dockerenv", "/run/systemd/container")

_CONTAINER = (
    "this is a container, so the kernel belongs to the host: an NVIDIA kernel module cannot be "
    "installed or loaded here, and there is nothing to reboot"
)


def cannot_load_reason() -> Optional[str]:
    """Why loading a module into this kernel would mean nothing, or None if it would mean something.

    Not folded into ``live_media_reason``, because they are different questions with different
    answers. A kickstart ``%post`` is a perfectly good place to install packages, which is most of
    what it is for, and a useless place to load a kernel module: the kernel running there is the
    installer's and the target system has not booted yet. So installing stays allowed there and only
    the loading is refused.

    The second check is the general one, and it catches what no marker file does: a chroot,
    systemd-nspawn, a ``%post``, and any container runtime that leaves no trace. If the running
    kernel's modules are not in this filesystem, this is not the system that booted. Without it,
    modprobe in a ``%post`` names the installer's release as the directory it could not find the
    module in, and the operator gets either a reboot promise or a warning that their driver package
    and their kernel do not match, in the install log of a machine that is perfectly fine.
    """
    foreign = foreign_kernel_reason()
    if foreign:
        return foreign
    running = platform.release()
    if not os.path.exists(os.path.join("/lib/modules", running, "modules.dep")):
        return (
            "the running kernel is %s and there are no modules for it in this filesystem, so this "
            "is not the system that booted: a chroot, an installer environment such as a kickstart "
            "%%post, or a container. Nothing loaded here would mean anything" % running
        )
    return None


def _root_mount() -> Optional[Sequence[str]]:
    """The mount options of ``/``, as a list, or None if they cannot be read."""
    mounts = procutil.read_file("/proc/mounts")
    if mounts is None:
        return None
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[1] == "/":
            return fields[3].split(",")
    return None


# --- what to say ------------------------------------------------------------------------


def describe(state: str, summary: Dict[str, Any]) -> str:
    """One sentence naming what is missing and what installing it would cost.

    The reboot is named only where it is real, and it is no longer promised where it is merely
    likely. Saying "a reboot will be required" when only the toolkit is missing would be false and
    would talk somebody out of a change that costs them nothing; saying it of the driver, which can
    often be loaded into the running kernel, sends somebody off to reboot a machine that was about
    to work.
    """
    cards = hwquery_nvidia(summary)
    names = ", ".join(
        (gpu.get("smi_name") or _lspci_name(gpu) or "an NVIDIA GPU") for gpu in cards
    ) or "an NVIDIA GPU"
    if state == STATE_DRIVER_MISSING:
        return (
            "%s is present and the NVIDIA driver is not installed, so the GPU checks cannot run. "
            "Installing the driver and the CUDA stack is a large download. Afterwards the driver "
            "will be loaded into the running kernel if it can be, and this run can then certify "
            "the card; if it cannot be, a reboot and a second run will be needed." % names
        )
    if state == STATE_DRIVER_NOT_LOADED:
        return (
            "%s is present and the NVIDIA driver is installed but not loaded, which is the state a "
            "machine is in when the driver was installed and never rebooted. Nothing needs "
            "installing: loading it into the running kernel is worth trying and often works, and "
            "a reboot is the fallback. It may change what the console shows, because the vendor's "
            "own configuration brings the display half of the driver with it." % names
        )
    if state == STATE_TOOLKIT_MISSING:
        return (
            "%s is present and its driver answers, but there is no CUDA toolkit, so the compute "
            "checks cannot run. Installing the toolkit needs no reboot and this run can carry on "
            "and certify the card." % names
        )
    return ""


def _lspci_name(gpu: Dict[str, Any]) -> str:
    ids = gpu.get("pci_ids") or {}
    return (ids.get("device") or "").strip()


# --- what to run ------------------------------------------------------------------------


class Step(NamedTuple):
    """One command, with the reason it is being run.

    A description per step rather than one for the whole plan, because the plan is printed before
    anything happens and "install epel-release" on its own does not explain itself on a machine
    somebody is about to hand over to a certification suite.
    """

    why: str
    argv: List[str]
    # Recomputed immediately before the step runs, for a command whose right form is not knowable
    # when the plan is printed: the driver version that matches this kernel can only be asked for
    # once the repository the plan itself adds exists. Returns the argv to run, or None to run
    # ``argv`` unchanged.
    resolve: Optional[Callable[[], Optional[List[str]]]] = None


# AlmaLinux ships a release package that configures NVIDIA's repository, which is the route the
# distribution supports and therefore the one to use where it exists. It does not exist for 8.
_ALMA_RELEASE_PACKAGE = "almalinux-release-nvidia-driver"
_ALMA_NATIVE_MAJORS = ("9", "10")

# NVIDIA's own network repository, for AlmaLinux 8 and for any other EL rebuild. ``sbsa`` is their
# name for 64-bit Arm; everywhere else the directory is the machine's own arch.
_NVIDIA_REPO = (
    "https://developer.download.nvidia.com/compute/cuda/repos/rhel%s/%s/cuda-rhel%s.repo"
)


def repo_arch(machine: Optional[str] = None) -> str:
    """NVIDIA's directory name for this architecture."""
    machine = machine or platform.machine()
    return "sbsa" if machine == "aarch64" else machine


def repo_is_configured(*, major: str, is_almalinux: bool = True) -> bool:
    """Whether the packages can be reached without adding a repository first.

    True on AlmaLinux 9 and 10, where the driver and the toolkit come from ``extras`` by way of a
    release package, and ``extras`` is enabled by default. Installing from there is the same act as
    installing clpeak, which the suite already does unasked.

    False on 8 and on other rebuilds, where NVIDIA's own repository has to be added. That is a
    third-party step and it needs somebody to say yes, which is what ``setup-gpu`` is for.
    """
    return is_almalinux and major in _ALMA_NATIVE_MAJORS


def install_plan(
    state: str,
    *,
    major: str,
    is_almalinux: bool = True,
    machine: Optional[str] = None,
    kernel_release: Optional[str] = None,
) -> List[Step]:
    """The commands that would install what ``state`` says is missing.

    Two routes, and the difference is which the distribution supports rather than which is more
    convenient. On AlmaLinux 9 and 10 the repository comes from a release package the distribution
    maintains. On 8, and on any other rebuild, it comes from NVIDIA's own network repository, which
    needs a matched kernel-devel, CRB or PowerTools, EPEL for DKMS, and on 8 the ``nvidia-driver``
    module stream enabled before anything will resolve.

    **The toolkit-only case installs ``cuda-toolkit``, not ``cuda``.** That keeps the promise made
    in ``describe``: this machine's driver already works, ``cuda-toolkit`` pulls no driver, and
    replacing a working driver would risk both breaking it and needing the reboot the operator was
    just told they would not need.

    **Every transacting step passes ``-y``, and that is load-bearing.** Consent was already taken
    once, for the whole plan, before any of this runs. A step without it would stop half way through
    to ask something on its own account - most likely about importing a repository's GPG key, since
    both repositories ship ``gpgcheck=1`` with a remote key - and in a provisioning script or a
    kickstart there is nobody to answer, so it would hang rather than fail. No separate key import
    step is needed or wanted: ``-y`` covers it. ``config-manager`` and ``clean`` never ask.
    """
    steps: List[Step] = []
    native = is_almalinux and major in _ALMA_NATIVE_MAJORS
    if native:
        steps.append(Step(
            "configure NVIDIA's repository through the package AlmaLinux maintains for it",
            ["dnf", "-y", "install", _ALMA_RELEASE_PACKAGE],
        ))
    else:
        kernel = kernel_release or platform.release()
        steps.extend([
            Step(
                "install the kernel headers the driver's modules are built against",
                ["dnf", "-y", "install", "kernel-devel-%s" % kernel, "kernel-headers"],
            ),
            Step(
                "enable the builder repository, which DKMS needs",
                ["dnf", "config-manager", "--set-enabled",
                 "powertools" if major == "8" else "crb"],
            ),
            Step("install EPEL, which provides DKMS", ["dnf", "-y", "install", "epel-release"]),
            Step(
                "add NVIDIA's CUDA repository",
                ["dnf", "config-manager", "--add-repo",
                 _NVIDIA_REPO % (major, repo_arch(machine), major)],
            ),
        ])
        if major == "8":
            steps.append(Step(
                "select the open-source kernel module stream, which 8 requires before the "
                "packages will resolve",
                ["dnf", "-y", "module", "enable", "nvidia-driver:open-dkms"],
            ))
        steps.append(Step(
            "refresh the metadata for the repository just added",
            ["dnf", "clean", "expire-cache"],
        ))

    if state == STATE_TOOLKIT_MISSING:
        steps.append(Step(
            "install the CUDA toolkit, which pulls no driver and needs no reboot",
            ["dnf", "-y", "install", "cuda-toolkit"],
        ))
    elif state == STATE_DRIVER_MISSING:
        # ``cuda-toolkit`` rather than ``cuda``: ``cuda`` requires ``cuda-13-4``, which pulls the
        # driver stack in with it, so it would undo the pin on the same command line.
        steps.append(Step(
            "install the newest open kernel module this kernel can load, and the CUDA toolkit",
            ["dnf", "-y", "install", "nvidia-open", "cuda-toolkit"],
            resolve=_pin_driver_install if native else None,
        ))
    else:
        # Named rather than defaulted. This used to be an ``else`` that installed the driver, so
        # ``driver-not-loaded`` - a machine whose driver is already there - would have reinstalled
        # the whole stack to fix a module that only needed loading.
        raise ValueError("no install plan for state %r" % state)
    return steps


# --- picking a driver whose module this kernel will take --------------------------------
#
# AlmaLinux's NVIDIA repository ships precompiled kABI-tracking modules: each build of
# ``kmod-nvidia-open`` requires around 700 ``kernel(<symbol>) = <checksum>`` pairs, and the kernel
# packages provide the matching side. A build loads exactly when every one of those is satisfied,
# which makes "which driver can this kernel take" a set comparison rather than a guess.
#
# It has to be asked. On AlmaLinux Kitten the kernel moves faster than the driver builds, so the
# newest driver frequently cannot load: measured against the repository, of five Kitten kernels two
# had no matching build at all and the other three each wanted a different one. Plain
# ``dnf install nvidia-open`` takes the newest and satisfies its kernel symbols by installing a
# newer kernel, which leaves a module for a kernel nobody is running. That is what "Module nvidia
# not found in directory /lib/modules/<running>" is.
_KERNEL_PACKAGES = ("kernel", "kernel-core", "kernel-modules", "kernel-modules-core")
_KSYM = re.compile(r"^(kernel\([^)]+\))\s*=\s*(\S+)")


def _ksyms(text: str) -> Dict[str, str]:
    """The ``kernel(symbol) = checksum`` lines of an rpm or repoquery listing, as a mapping."""
    found = {}
    for line in (text or "").splitlines():
        match = _KSYM.match(line.strip())
        if match:
            found[match.group(1)] = match.group(2)
    return found


def running_kernel_ksyms(release: Optional[str] = None) -> Dict[str, str]:
    """The kABI symbols the running kernel provides.

    The union of the installed kernel subpackages rather than ``kernel-core`` alone. A few dozen of
    the symbols a driver needs come from ``kernel-modules-core``, and against kernel-core by itself
    a build that matches perfectly reads as a near miss.
    """
    release = release or platform.release()
    found: Dict[str, str] = {}
    for name in _KERNEL_PACKAGES:
        try:
            res = procutil.run_cmd(
                ["rpm", "-q", "--provides", "%s-%s" % (name, release)], timeout=120)
        except procutil.CommandNotFound:
            return {}
        if res.ok:
            found.update(_ksyms(res.stdout))
    return found


def _evr_of(nevra: str, package: str) -> str:
    """``kmod-nvidia-open-610.57.04-1.el10.x86_64`` to ``610.57.04-1.el10``."""
    evr = nevra[len(package) + 1:] if nevra.startswith(package + "-") else nevra
    return evr.rsplit(".", 1)[0]


def _version_key(evr: str) -> List[Any]:
    """Sort key for an EVR. Numeric where it is numeric, so 610 sorts above 95."""
    return [int(part) if part.isdigit() else part
            for part in re.split(r"[^0-9A-Za-z]+|(?<=\d)(?=[A-Za-z])|(?<=[A-Za-z])(?=\d)", evr)
            if part]


def kmod_builds(package: str, *, timeout: int = 300) -> List[Tuple[str, Dict[str, str]]]:
    """Every available build of ``package``, with the kABI symbols each one requires."""
    try:
        listing = procutil.run_cmd(
            ["dnf", "repoquery", "--quiet", "--showduplicates",
             "--qf", "%{name}-%{evr}.%{arch}", package], timeout=timeout)
    except procutil.CommandNotFound:
        return []
    if not listing.ok:
        return []
    builds = []
    for nevra in sorted({line.strip() for line in listing.stdout.splitlines() if line.strip()}):
        requires = procutil.run_cmd(
            ["dnf", "repoquery", "--quiet", "--requires", nevra], timeout=timeout)
        if not requires.ok:
            continue
        syms = _ksyms(requires.stdout)
        if syms:
            builds.append((nevra, syms))
    return builds


def loadable_driver(package: str = "kmod-nvidia-open") -> Tuple[Optional[str], Optional[str]]:
    """The newest driver version whose module this kernel will take, or why there is none.

    Returns ``(evr, None)`` or ``(None, reason)``. The reason is recorded rather than acted on: a
    kernel with no matching build is a fact about the repository on the day of the run, and it is
    the one thing that explains an install that succeeds and a driver that never appears.
    """
    have = running_kernel_ksyms()
    if not have:
        return None, "the running kernel publishes no kABI symbols, so nothing could be matched"
    builds = kmod_builds(package)
    if not builds:
        return None, "no %s builds are available from the configured repositories" % package
    missing = {
        nevra: sum(1 for sym, csum in syms.items() if have.get(sym) != csum)
        for nevra, syms in builds
    }
    fits = sorted((n for n, count in missing.items() if not count),
                  key=lambda n: _version_key(_evr_of(n, package)))
    if not fits:
        closest = min(missing, key=lambda n: missing[n])
        return None, (
            "no available %s matches the running kernel %s: the closest, %s, needs %d kABI symbols "
            "this kernel does not have" % (package, platform.release(), closest, missing[closest])
        )
    return _evr_of(fits[-1], package), None


def _pin_driver_install() -> Optional[List[str]]:
    """The install command with the driver pinned to a version this kernel can load.

    Both the module and the metapackage are named, because ``nvidia-open`` requires its own version
    of the module: pinning one and not the other lets dnf satisfy the pair by taking the newest of
    both. The epoch is left off, which dnf matches on name-version-release.

    The module is pinned by its full EVR and the metapackage by version alone, because their
    releases are not the same thing and do not track each other. The module is rebuilt per kernel
    minor, so one version of it has several releases (``615.71.09-1.el10_2`` beside
    ``595.71.05-1.el10_1``), while ``nvidia-open`` is ``1.el10`` for every version in both the
    stable and the Kitten repository. Pinning the metapackage to the module's release asked for
    ``nvidia-open-615.71.09-1.el10_2``, which does not exist, and the install failed outright.
    Version alone is also how the packages refer to each other: ``nvidia-open`` requires
    ``kmod-nvidia-open = 3:615.71.09``, with no release in it.
    """
    evr, reason = loadable_driver()
    if evr is None:
        _record["kabi_no_match"] = reason
        return None
    _record["kabi_match"] = evr
    return ["dnf", "-y", "install",
            "kmod-nvidia-open-%s" % evr,
            "nvidia-open-%s" % evr.partition("-")[0],
            "cuda-toolkit"]


def run_plan(steps: Sequence[Step], *, log=print) -> Optional[Step]:
    """Run the plan in order. Returns the step that failed, or None if all of them worked.

    Stops at the first failure rather than pressing on, because every step here is a precondition
    for the ones after it: installing packages from a repository that was not added would fail more
    confusingly than the missing repository did.
    """
    for step in steps:
        if step.resolve is not None:
            replacement = step.resolve()
            if replacement:
                step = step._replace(argv=replacement)
        log("  %s" % step.why)
        log("  $ %s" % " ".join(step.argv))
        try:
            # Passed through as it arrives, because these are the longest commands the suite runs:
            # the CUDA stack is gigabytes and several minutes, and by default ``run_cmd`` buffers
            # until exit, so it showed nothing at all and was indistinguishable from a hang.
            #
            # dnf's own words rather than a summary invented from them: it knows what it is
            # doing, and anything we printed instead would be a guess at its output.
            def show(line: str) -> None:
                if line.strip():
                    log("    %s" % line)

            res = procutil.run_cmd(step.argv, timeout=1800, on_line=show)
        except procutil.CommandNotFound:
            return step
        if not res.ok:
            # The last lines again, at the end, so the reason is next to the failure rather than
            # somewhere above in whatever else scrolled past.
            for line in (res.stderr or res.stdout or "").strip().splitlines()[-5:]:
                log("    %s" % line)
            return step
    return None


# --- what the report has to say about it -------------------------------------------------
#
# A driver this suite loaded minutes before the test is not the same evidence as one that came up
# at boot: nouveau suppression lands on the kernel command line and in the initramfs, which no boot
# has seen yet, and ``nvidia-driver-cuda`` itself makes ``dnf needs-restarting -r`` exit 1 for the
# rest of that boot. Whether a hot-loaded pass may certify is lumina's call, so this records facts
# only: the modules before, the commands run, what each said, and the modules after.
#
# Module-level because the offer happens before the run exists (``_offer_gpu_setup`` runs ahead of
# ``_start_run``), as with ``procutil``'s debug sink: one run per process.
_record: Dict[str, Any] = {}


def note_starting_state() -> None:
    """Record what was loaded before this suite touched anything. The first call wins.

    Called before the first thing that might load the driver as a side effect, which is nearer than
    it sounds: NVML forks setuid ``nvidia-modprobe`` when it cannot reach the driver, so as root
    even asking ``nvidia-smi`` whether the driver is up can load it.
    """
    if "modules_before" not in _record:
        _record["modules_before"] = loaded_nvidia_modules()
        # The in-tree driver too, which ``loaded_nvidia_modules`` does not list because it matches
        # on the vendor module's own name. It is the most common reason a GPU run certifies nothing
        # after a successful install, and without it the report showed an empty before, an empty
        # after, and no hint of why: the operator's terminal said "nouveau is holding the card", and
        # stderr does not reach the log.
        conflicting = [name for name in _CONFLICTING_MODULES if name in _loaded_module_names()]
        if conflicting:
            _record["conflicting_modules_before"] = conflicting


def note_install(steps: Sequence[Step], failed: Optional[Step]) -> None:
    """Record that the driver packages were installed during the run, and how far it got.

    Worth its own note because ``environment.installed_packages`` cannot show it: that list is
    ``pkg.newly_installed``, which only tracks what went through ``alma_certify.pkg``, and this
    transaction is a ``dnf`` the plan runs directly. So the report did not say the driver had been
    installed during the run at all.
    """
    _record["installed_during_run"] = [" ".join(step.argv) for step in steps]
    if failed is not None:
        _record["install_failed_at"] = " ".join(failed.argv)


def load_record() -> Optional[Dict[str, Any]]:
    """The raw provenance of the driver, or None if this run never looked.

    Absent rather than empty on a machine with no NVIDIA card, so nothing changes in the reports of
    the many machines this does not apply to.
    """
    if not _record:
        return None
    record = dict(_record)
    # Derived here rather than left to the reader, because it is an observation and not a judgement:
    # either this process ran the modprobe or it did not, and only this process knows.
    record["loaded_by_alma_cert"] = bool(record.get("attempts"))
    record["present_before_run"] = bool(record.get("modules_before"))
    return record


def _reset_record_for_tests() -> None:
    """Clear the record. Only the tests call this; a run is one process."""
    _record.clear()


# --- bringing the driver up in the kernel that is already running -----------------------

# What has to be in the running kernel before the driver counts as up, in the order it loads.
#
# ``nvidia`` alone. It is the driver; if it is in and nvidia-smi answers, the card is reachable.
_REQUIRED_MODULES = ("nvidia",)

# ``nvidia_drm`` is deliberately not here. It is display-only (nvidia-smi and CUDA do not need it,
# no test touches DRM), and on load it takes the console: ``kmod-nvidia-open`` defaults to
# ``modeset=1`` and ``fbdev=1`` and removes the firmware framebuffer, and AlmaLinux 9
# (``CONFIG_FB_EFI=y``, no simpledrm) has nothing to fall back to. The vendor's
# ``softdep nvidia post: nvidia-uvm nvidia-drm`` pulls it in anyway; that is not fought, since
# suppressing it would also drop the ``options nvidia`` lines from the same file. So it is not asked
# for, and the offer says the display may change.
#
# ``nvidia_uvm`` is asked for and not required. It loads on demand (``nvidia-modprobe`` brings it
# in with the first CUDA context), so absent right after a modprobe is the ordinary state, and
# requiring it made a healthy machine look broken. Attempting it is free and catches a softdep that
# did nothing; the CUDA checks are the arbiter, since they fail with a CUDA error if it cannot load.
_OPTIONAL_MODULES: Tuple[str, ...] = ("nvidia_uvm",)


# Why each one, for printing the plan before running it. Every module above needs an entry: the
# operator is being shown commands that change their running kernel, and "modprobe nvidia_uvm" does
# not explain itself.
_MODULE_WHY = {
    "nvidia": "load the driver itself",
    "nvidia_uvm": "load the memory manager every CUDA call needs and nvidia-smi does not",
}


def load_plan() -> List[Step]:
    """The commands ``try_load`` would run, in order, for showing before it runs them."""
    return [
        Step(_MODULE_WHY[name], ["modprobe", name])
        for name in _REQUIRED_MODULES + _OPTIONAL_MODULES
    ]


class LoadAttempt(NamedTuple):
    """What came of trying to load the driver.

    ``reboot_may_help`` exists so the caller does not have to guess. "Reboot and run again" is good
    advice for a module built against a different kernel and actively bad advice for one Secure Boot
    will not accept, where the reboot achieves nothing and the operator has to enroll a key.
    """

    ok: bool
    modules: List[str]
    reason: Optional[str]
    reboot_may_help: bool


def loaded_nvidia_modules() -> List[str]:
    """The NVIDIA modules in the running kernel, as ``/proc/modules`` names them.

    A file read rather than ``lsmod``, which is a formatter over this same file.
    """
    found = []
    for line in (procutil.read_file("/proc/modules") or "").splitlines():
        name = line.split(" ", 1)[0]
        if name == "nvidia" or name.startswith("nvidia_"):
            found.append(name)
    return found


# The in-tree drivers for NVIDIA cards, which the vendor driver cannot share a card with.
# ``nova_core`` is nouveau's Rust successor and is on AlmaLinux 10; the driver packages blacklist
# both by name, so both belong here.
_CONFLICTING_MODULES = ("nouveau", "nova_core")


def _loaded_module_names() -> set:
    """Every module in the running kernel, by name."""
    return {
        line.split(" ", 1)[0]
        for line in (procutil.read_file("/proc/modules") or "").splitlines()
        if line.strip()
    }


_PCI_DEVICES = "/sys/bus/pci/devices"
_NVIDIA_PCI_VENDOR = "0x10de"


def nvidia_cards_by_driver() -> List[Tuple[str, Optional[str]]]:
    """Each NVIDIA PCI device and the driver bound to it, newest sysfs answer, ``None`` for none.

    sysfs rather than lspci: this runs before the inventory is collected, it needs no tool that
    might not be installed, and the binding is a kernel fact. The ``driver`` symlink is simply
    absent when nothing has claimed the device, which is the state that matters here.
    """
    cards: List[Tuple[str, Optional[str]]] = []
    try:
        names = sorted(os.listdir(_PCI_DEVICES))
    except OSError:
        return cards
    for name in names:
        path = os.path.join(_PCI_DEVICES, name)
        vendor = (procutil.read_file(os.path.join(path, "vendor")) or "").strip().lower()
        if vendor != _NVIDIA_PCI_VENDOR:
            continue
        try:
            driver = os.path.basename(os.readlink(os.path.join(path, "driver")))
        except OSError:
            # No such symlink is the ordinary "nothing has claimed this device" state.
            driver = None
        cards.append((name, driver))
    return cards


def nouveau_holding() -> Optional[str]:
    """Why an in-tree driver makes this attempt pointless, or None if none is loaded.

    Not "unload it and carry on", which was the first thing considered and is wrong twice over.

    It would not work: ``rmmod`` refuses a module anything holds, and an open DRM file descriptor is
    enough, so a display server, plymouth, logind, or the framebuffer console all pin it. There is
    no force to fall back on either, because AlmaLinux 9 and 10 both ship with
    ``CONFIG_MODULE_FORCE_UNLOAD`` unset, where the kernel silently ignores the flag: the code would
    look like it had worked and have done nothing.

    And it would not be honest even if it worked. Suppressing nouveau is not only a modprobe
    blacklist: ``nvidia-kmod-common`` adds ``rd.driver.blacklist=nouveau`` to the kernel command
    line and the kmod's ``%posttrans`` regenerates the initramfs, and both of those are boot-time by
    construction. A card certified after a live module swap would have been certified in a software
    configuration nobody is ever going to boot into.

    So this is the one case where the reboot really is the answer, and saying so beats a modprobe
    that was going to fail anyway: nouveau holds the card, and the NVIDIA module cannot bind it.

    Bound, not merely loaded, and that distinction is the whole of the second version of this.
    Loaded was what it asked first, and on a machine with a card too new for the in-tree driver it
    said nouveau was holding a card nouveau had never claimed. The modprobe it then refused to run
    was the one that would have reported the real fault: the driver package had no module for the
    running kernel, which ``classify_load_failure`` names exactly and nobody ever saw. One card
    still free is enough to go on, because that is the card the vendor module can bind.
    """
    loaded = _loaded_module_names()
    if not any(name in loaded for name in _CONFLICTING_MODULES):
        return None
    cards = nvidia_cards_by_driver()
    held = [(addr, driver) for addr, driver in cards if driver in _CONFLICTING_MODULES]
    # Nothing held, or something still free: attempt it. Where sysfs cannot be read at all this
    # lands here too, which is the right way round - a modprobe that fails says why, and a refusal
    # on a guess says nothing.
    if not held or len(held) < len(cards):
        return None
    drivers = " and ".join(sorted({driver for _, driver in held if driver}))
    subject = ("every NVIDIA card here is bound to %s, so the NVIDIA module has nothing it can bind"
               % drivers if len(held) > 1 else
               "%s is bound to the card, so the NVIDIA module cannot bind it" % drivers)
    return (
        "%s. Installing the driver blacklists it on the kernel command line and in the initramfs, "
        "and both of those only take effect at boot, so this is a case where the reboot really is "
        "the answer" % subject
    )


def smi_check() -> Tuple[bool, str]:
    """Whether ``nvidia-smi`` can talk to the driver right now, and what it said about it.

    The only proof worth having. ``modprobe`` exiting 0 means a module was inserted, not that the
    driver came up: a module can load and then find no device it supports, and the reverse happens
    too, where a dependency loaded on its own account and the one that matters did not.

    Its own words are kept and quoted back, because ``NVIDIA-SMI has failed because it couldn't
    communicate with the NVIDIA driver`` is a better sentence than anything invented from an exit
    code, and because the failures worth telling apart differ only in that text.
    """
    smi = procutil.find_tool("nvidia-smi")
    if smi is None:
        return False, "nvidia-smi is not installed"
    try:
        # Bounded, because a card wedged in a bad state can make this hang, and a certification run
        # that never returns is worse than one that reports a card it could not talk to.
        res = procutil.run_cmd([smi, "-L"], timeout=120)
    except procutil.CommandNotFound:
        return False, "nvidia-smi is not installed"
    if res.timed_out:
        return False, "%s: nvidia-smi did not return in time" % _TIMED_OUT
    said = ((res.stdout or "") + (res.stderr or "")).strip()
    # ``-L`` lists GPUs one per line. On a driver that answers with no devices it says so and exits
    # non-zero, which is a real finding rather than a failure to load, and it is reported as itself.
    return bool(res.ok and said), said


def newer_kernel_installed(
    running: Optional[str] = None, *, modules_dir: str = "/lib/modules",
) -> Optional[bool]:
    """Whether a kernel newer than the running one has its modules installed. None if unreadable.

    This is what decides whether "reboot and run again" is honest, so it is worth getting from
    something that cannot be out of date: a directory under ``/lib/modules`` with a ``modules.dep``
    in it is a kernel whose modules are installed and which can therefore be booted into.

    ``modules_dir`` is a seam for the tests and nothing else. Patching ``os.path.exists`` to fake a
    directory tree patches it for everything, pytest's own traceback machinery included, which is a
    mistake this project has made once already with ``glob``.

    Not ``dnf needs-restarting -r``, which answers a broader question and comes from
    ``dnf-plugins-core``, absent from a minimal AlmaLinux; and not ``rpm -q kernel-core``, which is
    a subprocess and an rpm version comparison to do by hand anyway.
    """
    running = running or platform.release()
    try:
        entries = os.listdir(modules_dir)
    except OSError:
        return None
    here = _release_key(running)
    flavour = _flavour(running)
    for entry in entries:
        # Same flavour only. ``5.14.0-687.38.1.el9_8.x86_64+debug`` is a real AlmaLinux release and
        # a real directory name here, and it sorts above the plain kernel however the comparison is
        # done, so a host with kernel-debug installed was told a newer kernel was waiting and to
        # reboot into it. Rebooting could never make the running kernel the newest, because the
        # suffix always wins, so the advice would repeat forever and never come true. ``+rt`` and
        # aarch64's ``+64k`` are the same shape.
        if _flavour(entry) != flavour:
            continue
        if not os.path.exists(os.path.join(modules_dir, entry, "modules.dep")):
            continue
        if _release_key(entry) > here:
            return True
    return False


def _flavour(release: str) -> str:
    """A kernel's variant: what follows ``+``, or empty for the ordinary one."""
    return release.split("+", 1)[1] if "+" in release else ""


def _release_key(release: str) -> List[Tuple[int, int, str]]:
    """A comparable key for a kernel release string, ordering the way rpm does.

    ``5.14.0-570.12.1.el9_6.x86_64`` against ``5.14.0-570.9.1.el9_6.x86_64``: split into runs of
    digits and letters, compare digits numerically so 12 beats 9, and rank a numeric run above an
    alphabetic one where the two line up, which is rpm's rule. Tuples throughout because Python will
    not compare an int with a str, and this has to total-order whatever it is handed.
    """
    key = []
    for chunk in re.findall(r"\d+|[A-Za-z]+", release):
        if chunk.isdigit():
            key.append((1, int(chunk), ""))
        else:
            key.append((0, 0, chunk))
    return key


# The failure modes, by the words modprobe uses for them. Taken from kmod's own message strings
# rather than from memory, and reproduced against kmod 34: a module absent for the running kernel
# is ``FATAL: Module nvidia not found in directory /lib/modules/<release>``, and everything else
# arrives as ``ERROR: could not insert 'nvidia': <reason>``, where the reason is either one of
# kmod's two special cases or plain ``strerror``. ``run_cmd`` sets ``LC_ALL=C``, so these stay
# English.
_SECURE_BOOT = ("Required key not available", "Key was rejected by service")
_WRONG_KERNEL = ("not found in directory", "Exec format error", "Unknown symbol in module")
_NO_DEVICE = ("No such device",)
_REFUSED = ("Operation not permitted", "Permission denied")


def classify_load_failure(stderr: str, *, newer_kernel: Optional[bool]) -> Tuple[str, bool]:
    """Why a module would not load, and whether rebooting would change it.

    Ordered by how specific the evidence is. Secure Boot first, because it is the case where the
    obvious advice is wrong: the module is fine and the kernel will not have it, so a reboot changes
    nothing and the key has to be enrolled instead.
    """
    text = stderr or ""
    if _TIMED_OUT in text:
        # First, because it is the one outcome here that is about the hardware. A modprobe or an
        # nvidia-smi that hangs is what a card that has fallen off the bus looks like, and sending
        # somebody away to reboot would lose the most interesting thing the run found.
        return (
            "it did not return. That is what a card in a bad state looks like, and it is a finding "
            "about the hardware rather than something a reboot answers",
            False,
        )
    if any(marker in text for marker in _SECURE_BOOT):
        return (
            "the kernel refused the module's signature, which is Secure Boot: the module is there "
            "and cannot be loaded. Rebooting will not change that. Either enroll the key "
            "(mokutil --import) or turn Secure Boot off",
            False,
        )
    if any(marker in text for marker in _WRONG_KERNEL):
        if newer_kernel:
            return (
                "the module is not built for the kernel that is running, and a newer kernel is "
                "installed. Rebooting into it should bring the driver up",
                True,
            )
        if newer_kernel is None:
            return (
                "the module is not built for the kernel that is running. A reboot is the usual fix",
                True,
            )
        return (
            "the module is not built for the kernel that is running, and there is no newer kernel "
            "installed for it to have been built against, so a reboot would come back to this. On "
            "AlmaLinux 9 and 10 the kmod is kABI-tracking and weak-modules links it into every "
            "kernel whose kABI it matches, so this is that match failing. On AlmaLinux 8, where "
            "DKMS builds it locally, it is what a failed build looks like: every line of the "
            "kmod's install script ends in ``|| :``, so dnf reports success either way, and the "
            "reason is in /var/lib/dkms/nvidia/*/build/make.log",
            False,
        )
    if any(marker in text for marker in _NO_DEVICE):
        # Not "there is no card": this only ever runs on a machine where lspci found one. ENODEV
        # here is the module declining to attach to it, and AlmaLinux's own documentation attributes
        # this exact message to a driver and running kernel that do not match. A reboot is a fair
        # thing to suggest, on the same evidence as the case above.
        return (
            "the module loaded and did not attach to the card. That is what a driver built against "
            "a different kernel looks like, and what another driver still holding the card looks "
            "like",
            newer_kernel is not False,
        )
    if any(marker in text for marker in _REFUSED):
        return (
            "the kernel refused to load the module outright, which means either this is not root "
            "or the kernel is locked down",
            False,
        )
    last = [line for line in text.strip().splitlines() if line.strip()]
    return (
        last[-1] if last else "modprobe failed and said nothing about why",
        newer_kernel is not False,
    )


def try_load(*, log=print) -> LoadAttempt:
    """Try to bring the driver up in the kernel that is already running.

    Worth attempting rather than assuming, and it does work sometimes and not others. Which of the
    two depends on how the module got onto the machine, and both routes this suite uses are better
    than they sound:

    - **AlmaLinux 9 and 10** install ``kmod-nvidia-open``, a precompiled kABI-tracking module. Its
      dependencies are ``kernel(<symbol>) = <checksum>`` pairs rather than one kernel version, and
      its scriptlets run ``weak-modules``, which links it into every installed kernel whose kABI it
      matches, the running one included. So if dnf could install it at all, it usually loads.
    - **AlmaLinux 8 and other rebuilds** install ``kmod-nvidia-open-dkms``, which DKMS builds
      against the running kernel during the transaction. That one is built for this kernel by
      definition.

    Neither is a guarantee. The transaction may have brought a newer kernel with it, and Secure Boot
    may reject the module. So this attempts, then asks the machine rather than believing
    ``modprobe``'s exit code, and reports which kind of failure it was, because "reboot and run
    again" is right for a module built against another kernel and wrong for one the kernel will not
    accept.

    Three things it does not do, each for its own reason: it never unloads nouveau, it does not
    attempt anything at all while nouveau holds the card (see ``nouveau_holding``), and it does not
    ask for the display half of the driver (see ``_OPTIONAL_MODULES``).
    """
    note_starting_state()
    pointless = cannot_load_reason()
    if pointless:
        # Guarded here as well as at the offer. This is the function that must not run where it
        # would mean nothing, and a caller added later would not know to check first.
        return LoadAttempt(False, loaded_nvidia_modules(), pointless, False)
    modprobe = procutil.find_tool("modprobe")
    if modprobe is None:
        return LoadAttempt(False, loaded_nvidia_modules(), "modprobe is not installed", False)
    holding = nouveau_holding()
    if holding:
        return LoadAttempt(False, loaded_nvidia_modules(), holding, True)

    failures: List[str] = []
    attempts: List[Dict[str, Any]] = []
    _record["attempts"] = attempts
    for name in _REQUIRED_MODULES:
        log("  $ modprobe %s" % name)
        problem = _modprobe(modprobe, name, attempts)
        if problem:
            failures.append(problem)
            # Kept going rather than stopping: nvidia_uvm can fail on its own account, and the
            # verification below decides what the machine can actually do either way.
    for name in _OPTIONAL_MODULES:
        _modprobe(modprobe, name, attempts)

    modules = loaded_nvidia_modules()
    ok, said = smi_check()
    _record["modules_after"] = modules
    _record["nvidia_smi"] = said
    _record["running_kernel"] = platform.release()
    # Both kernels, because a transaction that pulled a newer one leaves a machine whose next boot
    # is not the one these results came from, and the report recorded only the running kernel.
    _record["newer_kernel_installed"] = newer_kernel_installed()
    if ok:
        return LoadAttempt(True, modules, None, False)
    newer = newer_kernel_installed()
    reason, reboot = classify_load_failure(
        "\n".join(failures) if failures else said, newer_kernel=newer,
    )
    return LoadAttempt(False, modules, reason, reboot)


# How a timed-out command is named, in a form ``classify_load_failure`` recognizes. A sentinel
# rather than a flag threaded through, because the same treatment is right for a modprobe that
# never returns and for an nvidia-smi that never returns, and they arrive by different routes.
_TIMED_OUT = "TIMED-OUT"


def _modprobe(
    modprobe: str, name: str, attempts: Optional[List[Dict[str, Any]]] = None,
) -> Optional[str]:
    """Load one module. Its stderr on failure, None when it loaded or was already there.

    ``attempts`` collects what was run and what it said, verbatim, for the report.
    """
    try:
        # A minute is generous for an insert. It is not unbounded, because a module that wedges on
        # load would otherwise take the whole run with it.
        res = procutil.run_cmd([modprobe, name], timeout=60)
    except procutil.CommandNotFound:
        return "modprobe is not installed"
    if attempts is not None:
        attempts.append({
            "argv": ["modprobe", name],
            "returncode": res.returncode,
            "timed_out": bool(res.timed_out),
            # Verbatim. A message this collector does not recognize is exactly the one a reviewer
            # needs, and summarizing it here would be the collector deciding.
            "stderr": (res.stderr or "").strip(),
        })
    if res.ok:
        return None
    if res.timed_out:
        # Its own outcome. Without this the empty stderr fell through every named category to the
        # last branch, and came out as a guess about rebooting drawn from a signal that has nothing
        # to do with it.
        return "%s: modprobe %s did not return" % (_TIMED_OUT, name)
    return ((res.stderr or "") + (res.stdout or "")).strip() or "modprobe %s failed" % name
