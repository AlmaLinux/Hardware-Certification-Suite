"""Deciding whether to offer to install the NVIDIA stack, and what to say about it.

The offer is only worth making when it would work and when it would help, so this is mostly about
the cases where it must not be made: no card, nothing missing, or a host whose root will not survive
the reboot the driver needs.

Consent is not tested here because there is nothing to consent to yet: this module decides *what* to
offer, and the prompting follows ``hostos.confirm``, which already refuses rather than hangs when
there is no terminal.
"""

import pytest

from alma_certify import cli, gpusetup
from alma_certify.gpusetup import (
    STATE_DRIVER_MISSING,
    STATE_DRIVER_NOT_LOADED,
    STATE_NO_CARD,
    STATE_READY,
    STATE_TOOLKIT_MISSING,
    cuda_state,
    describe,
    live_media_reason,
)

# What nvidia-smi says in each of the two states that matter here, in its own words.
SMI_OK = "GPU 0: NVIDIA L40S (UUID: GPU-6b5e6f6e-0000-0000-0000-000000000000)"
SMI_NO_DRIVER = (
    "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver. "
    "Make sure that the latest NVIDIA driver is installed and running."
)

L40S = {
    "pci": "41:00.0",
    "pci_ids": {"vendor": "NVIDIA Corporation [10de]", "device": "AD102GL [L40S] [26b9]"},
    "driver": "nvidia",
    "smi_name": "NVIDIA L40S",
}
MATROX = {
    "pci_ids": {"vendor": "Matrox Electronics Systems Ltd. [102b]",
                "device": "Integrated Matrox G200eW3 Graphics Controller [0536]"},
    "driver": "mgag200",
}


@pytest.fixture
def nothing_installed(monkeypatch):
    monkeypatch.setattr(gpusetup.procutil, "find_tool", lambda tool, extra_dirs=(): None)
    monkeypatch.setattr("alma_certify.validate.nvidia.find_nvcc", lambda: None)


# The modules a working driver has in the running kernel. Named here because every fixture below
# needs one answer or the other, and "which modules" is the question the state now turns on.
LOADED = ["nvidia", "nvidia_uvm"]


@pytest.fixture
def driver_only(monkeypatch):
    """The driver installed, loaded, *and* answering, with no toolkit."""
    monkeypatch.setattr(
        gpusetup.procutil, "find_tool",
        lambda tool, extra_dirs=(): "/usr/bin/nvidia-smi" if tool == "nvidia-smi" else None,
    )
    monkeypatch.setattr(gpusetup, "loaded_nvidia_modules", lambda: LOADED)
    monkeypatch.setattr(gpusetup, "smi_check", lambda: (True, SMI_OK))
    monkeypatch.setattr("alma_certify.validate.nvidia.find_nvcc", lambda: None)


@pytest.fixture
def driver_not_loaded(monkeypatch):
    """The state a machine is in when the driver was installed and never rebooted.

    ``nvidia-smi`` is on the disk, because it is userspace and ships with the driver packages, and
    there is no kernel module for it to talk to.
    """
    monkeypatch.setattr(
        gpusetup.procutil, "find_tool", lambda tool, extra_dirs=(): "/usr/bin/" + tool,
    )
    monkeypatch.setattr(gpusetup, "loaded_nvidia_modules", lambda: [])
    monkeypatch.setattr(gpusetup, "smi_check", lambda: (False, SMI_NO_DRIVER))
    monkeypatch.setattr("alma_certify.validate.nvidia.find_nvcc", lambda: None)


@pytest.fixture
def everything(monkeypatch):
    monkeypatch.setattr(
        gpusetup.procutil, "find_tool", lambda tool, extra_dirs=(): "/usr/bin/" + tool,
    )
    monkeypatch.setattr(gpusetup, "loaded_nvidia_modules", lambda: LOADED)
    monkeypatch.setattr(gpusetup, "smi_check", lambda: (True, SMI_OK))
    monkeypatch.setattr(
        "alma_certify.validate.nvidia.find_nvcc", lambda: "/usr/local/cuda-13.3/bin/nvcc",
    )


@pytest.fixture
def installable(monkeypatch, tmp_path):
    """A machine whose root will still be there after a reboot."""
    monkeypatch.setattr(
        gpusetup.procutil, "read_file",
        lambda path, default=None: {
            "/proc/cmdline": "BOOT_IMAGE=/vmlinuz root=/dev/mapper/rl-root ro",
            "/proc/mounts": "/dev/mapper/rl-root / xfs rw,relatime 0 0\n",
        }.get(path, default),
    )
    monkeypatch.setattr(gpusetup.os.path, "isdir", lambda path: False)
    monkeypatch.setattr(gpusetup.os.path, "exists", lambda path: False)


# --- what is missing -----------------------------------------------------------


def test_no_nvidia_card_means_no_offer(nothing_installed):
    """A machine with only a management display adapter is not a machine somebody wants a CUDA
    toolkit on."""
    assert cuda_state({"gpus": [MATROX]}) == STATE_NO_CARD


def test_no_gpus_at_all_means_no_offer(nothing_installed):
    assert cuda_state({"gpus": []}) == STATE_NO_CARD


def test_a_card_with_no_driver_needs_the_driver(nothing_installed):
    assert cuda_state({"gpus": [L40S]}) == STATE_DRIVER_MISSING


def test_a_card_with_a_driver_and_no_toolkit_needs_the_toolkit(driver_only):
    """The state the maintainer's own machine was in after installing the driver."""
    assert cuda_state({"gpus": [L40S]}) == STATE_TOOLKIT_MISSING


def test_a_ready_machine_is_offered_nothing(everything):
    assert cuda_state({"gpus": [L40S]}) == STATE_READY


def test_a_driver_installed_but_not_loaded_is_its_own_state(driver_not_loaded):
    """The machine that has everything on disk and no module in the kernel. It used to be called
    ready, and then all four GPU checks failed with an error about not reaching the driver."""
    assert cuda_state({"gpus": [L40S]}) == STATE_DRIVER_NOT_LOADED


def test_the_driver_is_checked_rather_than_the_tool_merely_found(monkeypatch):
    """``nvidia-smi`` existing is not the driver working, which is what this used to assume.

    And nothing is run to find out, which is the second half of it. NVML forks setuid
    ``nvidia-modprobe`` when it cannot reach the driver, and that loads the module and creates the
    device nodes: as root, asking nvidia-smi whether the driver is up is liable to make it up. So
    the state would have come back ready, having loaded a module into somebody's kernel to find out,
    with no offer and nobody's consent. /proc/modules answers the same question for free.
    """
    monkeypatch.setattr(
        gpusetup.procutil, "find_tool", lambda tool, extra_dirs=(): "/usr/bin/" + tool,
    )
    monkeypatch.setattr("alma_certify.validate.nvidia.find_nvcc", lambda: None)
    monkeypatch.setattr(
        gpusetup.procutil, "read_file",
        lambda path, default=None: (
            "xfs 2109440 2 - Live 0x0\n" if path == "/proc/modules" else default
        ),
    )
    calls = []
    monkeypatch.setattr(
        gpusetup.procutil, "run_cmd", lambda argv, **kw: calls.append(argv) or None,
    )

    assert cuda_state({"gpus": [L40S]}) == STATE_DRIVER_NOT_LOADED
    assert not calls, "nothing should have been run to find this out"


def test_a_loaded_driver_that_does_not_answer_is_the_same_state(driver_not_loaded, monkeypatch):
    """A different machine, and the same fix is worth trying: nvidia_uvm may be what is missing."""
    monkeypatch.setattr(gpusetup, "loaded_nvidia_modules", lambda: ["nvidia"])

    assert cuda_state({"gpus": [L40S]}) == STATE_DRIVER_NOT_LOADED


# --- what it says --------------------------------------------------------------


def test_the_driver_case_names_the_reboot_as_the_fallback_not_the_plan(driver_only):
    """It used to say the run could not certify the card whatever happened, because a module cannot
    load into a kernel that has no business with it yet.

    That is not true of the modules this suite installs. AlmaLinux 9 and 10 ship a kABI-tracking
    ``kmod-nvidia-open`` that ``weak-modules`` links into the running kernel, and 8 uses DKMS, which
    builds against the running kernel during the transaction. So the reboot is what happens when the
    load fails, and promising it up front sends somebody off to reboot a machine that was about to
    work.
    """
    text = describe(STATE_DRIVER_MISSING, {"gpus": [L40S]})

    assert "reboot" in text, "still worth naming: it is the fallback"
    for claim in ("will not be able to certify", "needs a reboot afterwards"):
        assert claim not in text


def test_the_not_loaded_case_says_nothing_needs_installing(driver_not_loaded):
    """The download is the expensive part and there is none here, so somebody weighing the offer
    needs to know that this one is three modprobes."""
    text = describe(STATE_DRIVER_NOT_LOADED, {"gpus": [L40S]})

    assert "not loaded" in text
    assert "Nothing needs installing" in text
    assert "NVIDIA L40S" in text


def test_the_toolkit_case_says_a_reboot_is_not_needed(driver_only):
    """Saying a reboot is required when it is not would be false, and would talk somebody out of a
    change that costs them nothing.

    Saying so explicitly rather than staying silent about it, because the driver case does warn
    about one and a reader who has seen that message once will otherwise assume this one means the
    same thing.
    """
    text = describe(STATE_TOOLKIT_MISSING, {"gpus": [L40S]})

    assert "needs no reboot" in text
    assert "carry on" in text
    for claim in ("a reboot will be required", "needs a reboot", "reboot afterwards"):
        assert claim not in text


def test_the_card_is_named_so_the_offer_is_about_something(driver_only):
    text = describe(STATE_TOOLKIT_MISSING, {"gpus": [L40S]})

    assert "NVIDIA L40S" in text


def test_a_card_with_no_marketing_name_still_reads(driver_only):
    """Before the driver is installed there is no nvidia-smi to give the marketing name, so the
    lspci device string is all there is. That is exactly the state the driver offer happens in."""
    bare = {"pci_ids": {"vendor": "NVIDIA Corporation [10de]", "device": "AD102GL [L40S] [26b9]"}}

    text = describe(STATE_DRIVER_MISSING, {"gpus": [bare]})

    assert "AD102GL [L40S]" in text


def test_a_ready_machine_has_nothing_to_say(everything):
    assert describe(STATE_READY, {"gpus": [L40S]}) == ""


# --- hosts that cannot be changed ----------------------------------------------


def test_an_ordinary_installed_system_can_be_changed(installable):
    assert live_media_reason() is None


@pytest.mark.parametrize("cmdline", [
    "BOOT_IMAGE=/isolinux/vmlinuz root=live:CDLABEL=AlmaLinux-10 rd.live.image quiet",
    "root=live:/dev/sr0 rd.live.dir=/LiveOS",
])
def test_live_media_is_refused(monkeypatch, cmdline):
    """The case the maintainer named. A live ISO's root is an overlay over a read-only squashfs, so
    a package installed there is gone at the reboot the driver needs, and the operator would have
    waited for a download to achieve nothing."""
    monkeypatch.setattr(
        gpusetup.procutil, "read_file",
        lambda path, default=None: cmdline if path == "/proc/cmdline" else default,
    )
    monkeypatch.setattr(gpusetup.os.path, "isdir", lambda path: False)
    monkeypatch.setattr(gpusetup.os.path, "exists", lambda path: False)

    reason = live_media_reason()

    assert reason is not None
    assert "live media" in reason
    assert "reboot" in reason


def test_the_live_directory_is_also_enough(monkeypatch):
    """A second signal, because a live image booted by something other than Anaconda's own boot
    entry may not carry the cmdline argument."""
    monkeypatch.setattr(
        gpusetup.procutil, "read_file", lambda path, default=None: "root=/dev/sda1 ro",
    )
    monkeypatch.setattr(
        gpusetup.os.path, "isdir", lambda path: path == "/run/initramfs/live",
    )
    monkeypatch.setattr(gpusetup.os.path, "exists", lambda path: False)

    assert "live media" in (live_media_reason() or "")


@pytest.mark.parametrize("marker", ["/run/.containerenv", "/.dockerenv"])
def test_a_container_is_refused(monkeypatch, marker):
    """The kernel belongs to the host, so a module installed here cannot load and there is nothing
    to reboot. Different reason, same answer."""
    monkeypatch.setattr(
        gpusetup.procutil, "read_file", lambda path, default=None: "root=/dev/sda1 ro",
    )
    monkeypatch.setattr(gpusetup.os.path, "isdir", lambda path: False)
    monkeypatch.setattr(gpusetup.os.path, "exists", lambda path: path == marker)

    reason = live_media_reason()

    assert reason is not None
    assert "container" in reason
    assert "loaded here" in reason


def test_a_read_only_root_is_refused(monkeypatch):
    """The general case of the same thing, for an image-based or immutable deployment."""
    monkeypatch.setattr(
        gpusetup.procutil, "read_file",
        lambda path, default=None: {
            "/proc/cmdline": "root=/dev/sda1 ro",
            "/proc/mounts": "/dev/sda1 / xfs ro,relatime 0 0\n",
        }.get(path, default),
    )
    monkeypatch.setattr(gpusetup.os.path, "isdir", lambda path: False)
    monkeypatch.setattr(gpusetup.os.path, "exists", lambda path: False)

    assert "read-only" in (live_media_reason() or "")


def test_a_writable_root_whose_options_mention_ro_elsewhere_is_fine(monkeypatch):
    """``rw`` and a later option containing the letters r and o are not the same thing. The check
    splits the option list rather than searching the line, so "errors=remount-ro" does not refuse a
    perfectly ordinary machine."""
    monkeypatch.setattr(
        gpusetup.procutil, "read_file",
        lambda path, default=None: {
            "/proc/cmdline": "root=/dev/sda1 ro",
            "/proc/mounts": "/dev/sda1 / ext4 rw,relatime,errors=remount-ro 0 0\n",
        }.get(path, default),
    )
    monkeypatch.setattr(gpusetup.os.path, "isdir", lambda path: False)
    monkeypatch.setattr(gpusetup.os.path, "exists", lambda path: False)

    assert live_media_reason() is None


def test_an_unreadable_mount_table_does_not_refuse(monkeypatch):
    """Not being able to tell is not evidence of a read-only root, and refusing on it would decline
    to help on a machine that was fine."""
    monkeypatch.setattr(
        gpusetup.procutil, "read_file",
        lambda path, default=None: "root=/dev/sda1 ro" if path == "/proc/cmdline" else default,
    )
    monkeypatch.setattr(gpusetup.os.path, "isdir", lambda path: False)
    monkeypatch.setattr(gpusetup.os.path, "exists", lambda path: False)

    assert live_media_reason() is None


# --- live media is impermanent, not impossible ----------------------------------
#
# The reported regression: an install was refused on live media because it "would not survive a
# reboot". But the run happens now, in this session: the overlay takes the install and the running
# kernel takes the modprobe, so the driver loads for the run. Only a container and a read-only root
# are genuine dead ends, and those still refuse.


def test_on_live_media_detects_the_cmdline_signal(monkeypatch):
    monkeypatch.setattr(
        gpusetup.procutil, "read_file",
        lambda path, default=None: "BOOT_IMAGE=/vmlinuz root=live:CDLABEL=Alma rd.live.image quiet",
    )
    monkeypatch.setattr(gpusetup.os.path, "isdir", lambda path: False)

    assert gpusetup.on_live_media() is True


def test_on_live_media_is_false_on_an_ordinary_system(monkeypatch):
    monkeypatch.setattr(
        gpusetup.procutil, "read_file", lambda path, default=None: "root=/dev/sda1 ro quiet",
    )
    monkeypatch.setattr(gpusetup.os.path, "isdir", lambda path: False)

    assert gpusetup.on_live_media() is False


def test_install_is_not_impossible_on_live_media(monkeypatch):
    """The crux of the fix. Live media is live media (so it is caveated), but installing and loading
    are not impossible there, so ``install_impossible_reason`` - the gate the offer now blocks on -
    is None and the offer is still made."""
    monkeypatch.setattr(
        gpusetup.procutil, "read_file",
        lambda path, default=None: {
            "/proc/cmdline": "root=live:CDLABEL=Alma rd.live.image quiet",
            "/proc/mounts": "overlay / overlay rw,relatime 0 0\n",
            "/proc/1/environ": "",
        }.get(path, default),
    )
    monkeypatch.setattr(gpusetup.os.path, "isdir", lambda path: False)
    monkeypatch.setattr(gpusetup.os.path, "exists", lambda path: False)

    assert gpusetup.on_live_media() is True
    assert gpusetup.install_impossible_reason() is None


def test_install_is_impossible_in_a_container(monkeypatch):
    monkeypatch.setattr(
        gpusetup.procutil, "read_file", lambda path, default=None: "root=/dev/sda1 ro",
    )
    monkeypatch.setattr(gpusetup.os.path, "isdir", lambda path: False)
    monkeypatch.setattr(gpusetup.os.path, "exists", lambda path: path == "/run/.containerenv")

    reason = gpusetup.install_impossible_reason()
    assert reason is not None
    assert "container" in reason


def test_install_is_impossible_on_a_read_only_root(monkeypatch):
    monkeypatch.setattr(
        gpusetup.procutil, "read_file",
        lambda path, default=None: {
            "/proc/cmdline": "root=/dev/sda1 ro",
            "/proc/mounts": "/dev/sda1 / xfs ro,relatime 0 0\n",
            "/proc/1/environ": "",
        }.get(path, default),
    )
    monkeypatch.setattr(gpusetup.os.path, "isdir", lambda path: False)
    monkeypatch.setattr(gpusetup.os.path, "exists", lambda path: False)

    assert "read-only" in (gpusetup.install_impossible_reason() or "")


# --- what would be run ---------------------------------------------------------
#
# Two routes, and the difference is which the distribution supports rather than which is more
# convenient. AlmaLinux 9 and 10 have a release package that configures NVIDIA's repository; 8 does
# not, so it takes NVIDIA's own network repository with everything that needs first.


def _argv(steps):
    return [" ".join(step.argv) for step in steps]


@pytest.mark.parametrize("major", ["9", "10"])
def test_the_supported_route_is_two_commands(major):
    """The route AlmaLinux maintains. Anything longer than this on 9 or 10 means reaching past the
    distribution for no reason."""
    steps = gpusetup.install_plan(
        gpusetup.STATE_DRIVER_MISSING, major=major, machine="x86_64",
    )

    assert _argv(steps) == [
        "dnf -y install almalinux-release-nvidia-driver",
        "dnf -y install nvidia-open cuda-toolkit",
    ]


def test_eight_takes_nvidias_repository_with_its_prerequisites():
    """No release package exists for 8, so this is NVIDIA's documented path: matched kernel headers,
    PowerTools for DKMS's build dependencies, EPEL for DKMS itself, the repository, and the module
    stream that 8 needs before any of the packages will resolve."""
    steps = gpusetup.install_plan(
        gpusetup.STATE_DRIVER_MISSING, major="8", machine="x86_64",
        kernel_release="4.18.0-553.el8.x86_64",
    )

    assert _argv(steps) == [
        "dnf -y install kernel-devel-4.18.0-553.el8.x86_64 kernel-headers",
        "dnf config-manager --set-enabled powertools",
        "dnf -y install epel-release",
        "dnf config-manager --add-repo https://developer.download.nvidia.com/compute/cuda/"
        "repos/rhel8/x86_64/cuda-rhel8.repo",
        "dnf -y module enable nvidia-driver:open-dkms",
        "dnf clean expire-cache",
        "dnf -y install nvidia-open cuda-toolkit",
    ]


def test_nine_uses_crb_rather_than_powertools_when_it_takes_the_nvidia_route():
    """Which it only does for a rebuild that is not AlmaLinux. The builder repository was renamed
    after 8, and asking for powertools on 9 fails."""
    steps = gpusetup.install_plan(
        gpusetup.STATE_DRIVER_MISSING, major="9", is_almalinux=False, machine="x86_64",
        kernel_release="5.14.0-503.el9.x86_64",
    )

    assert "dnf config-manager --set-enabled crb" in _argv(steps)
    assert "dnf -y module enable nvidia-driver:open-dkms" not in _argv(steps), (
        "the module stream is an 8-only requirement"
    )


def test_arm_uses_nvidias_name_for_the_architecture():
    """NVIDIA calls 64-bit Arm sbsa, and the directory does not exist under aarch64."""
    steps = gpusetup.install_plan(
        gpusetup.STATE_DRIVER_MISSING, major="8", machine="aarch64", kernel_release="k",
    )

    assert any("/sbsa/cuda-rhel8.repo" in line for line in _argv(steps))
    assert gpusetup.repo_arch("x86_64") == "x86_64"
    assert gpusetup.repo_arch("aarch64") == "sbsa"


def test_the_toolkit_only_case_does_not_touch_the_driver():
    """This is what keeps the promise made in ``describe``. The machine's driver already works;
    ``cuda-toolkit`` pulls no driver, so nothing is replaced and no reboot becomes necessary. Asking
    for ``cuda`` here would install a driver over a working one and make the message a lie."""
    steps = gpusetup.install_plan(
        gpusetup.STATE_TOOLKIT_MISSING, major="10", machine="x86_64",
    )

    assert _argv(steps)[-1] == "dnf -y install cuda-toolkit"
    assert not any("nvidia-open" in line for line in _argv(steps))


def test_every_step_says_why_it_is_there():
    """The plan is printed before anything happens, and "install epel-release" does not explain
    itself on a machine somebody is about to hand to a certification suite."""
    steps = gpusetup.install_plan(
        gpusetup.STATE_DRIVER_MISSING, major="8", machine="x86_64", kernel_release="k",
    )

    for step in steps:
        assert step.why and not step.why.endswith(".")


def test_the_plan_stops_at_the_first_failure(monkeypatch):
    """Every step is a precondition for the ones after it: installing from a repository that was
    never added fails more confusingly than the missing repository did."""
    calls = []

    class Result:
        ok = False
        stdout = ""
        stderr = "No match for argument: nvidia-open"

    def fake_run(argv, timeout=None, **kw):
        calls.append(argv)
        return Result()

    monkeypatch.setattr(gpusetup.procutil, "run_cmd", fake_run)
    steps = gpusetup.install_plan(
        gpusetup.STATE_DRIVER_MISSING, major="10", machine="x86_64",
    )

    failed = gpusetup.run_plan(steps, log=lambda msg: None)

    assert failed is steps[0]
    assert len(calls) == 1


def test_a_plan_that_works_reports_nothing_failed(monkeypatch):
    class Result:
        ok = True
        stdout = ""
        stderr = ""

    monkeypatch.setattr(gpusetup.procutil, "run_cmd", lambda argv, timeout=None, **kw: Result())
    steps = gpusetup.install_plan(gpusetup.STATE_TOOLKIT_MISSING, major="9", machine="x86_64")

    assert gpusetup.run_plan(steps, log=lambda msg: None) is None


# --- consent -------------------------------------------------------------------


def test_no_terminal_refuses_rather_than_hanging(monkeypatch, capsys):
    """A prompt into a closed stdin blocks a CI job or a kickstart %post forever, and reading EOF as
    consent would opt somebody into a repository and a driver silently. The same rule
    ``hostos.confirm`` already follows for the unsupported-OS prompt."""
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)

    assert cli._confirm_gpu_setup(assume_yes=False) is False
    assert "Pass --yes" in capsys.readouterr().err


def test_the_flag_is_consent(capsys):
    assert cli._confirm_gpu_setup(assume_yes=True) is True
    assert "--yes" in capsys.readouterr().out


@pytest.mark.parametrize("answer,expected", [
    ("y", True), ("yes", True), ("Y", True),
    ("", False), ("n", False), ("no", False), ("maybe", False),
])
def test_the_surprising_outcome_has_to_be_typed(monkeypatch, answer, expected):
    """Defaulting to no, because installing a third-party repository and a kernel driver is the
    consequential answer and a bare Enter must not choose it."""
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)

    assert cli._confirm_gpu_setup(assume_yes=False) is expected


def test_every_transacting_step_is_non_interactive():
    """Consent is taken once, for the whole plan, before any of it runs.

    A step that asks something on its own account would stop half way through, and the likeliest
    thing it would ask about is importing a repository's GPG key, since both repositories ship
    ``gpgcheck=1`` with a remote key. In a provisioning script or a kickstart there is nobody to
    answer, so it would hang rather than fail, which is the exact outcome the consent design exists
    to avoid. ``-y`` covers the key import, so no separate import step is needed.

    Asserted over every route and both states, because the risk is a step added later.
    """
    plans = [
        gpusetup.install_plan(state, major=major, is_almalinux=alma, machine=machine,
                              kernel_release="4.18.0-553.el8.x86_64")
        for state in (gpusetup.STATE_DRIVER_MISSING, gpusetup.STATE_TOOLKIT_MISSING)
        for major, alma in (("8", True), ("9", True), ("10", True), ("9", False), ("10", False))
        for machine in ("x86_64", "aarch64")
    ]
    assert plans

    # These read or edit configuration and never prompt; anything else that reaches dnf transacts.
    quiet = {("config-manager",), ("clean",)}
    for steps in plans:
        for step in steps:
            assert step.argv[0] == "dnf", step.argv
            if (step.argv[1],) in quiet:
                continue
            assert step.argv[1] == "-y", (
                "%s can stop to ask something, and there may be nobody there" % " ".join(step.argv)
            )


# --- the hint has to reach somebody ---------------------------------------------
#
# Reported: a benchmark run on a machine with an NVIDIA card and no stack for it skipped its whole
# GPU category, and nothing prompted or explained. Two separate faults behind one symptom.


def test_a_benchmark_run_offers_the_setup():
    """The first fault. The offer was wired into validate and run and not into benchmark, which is
    the run where it matters most: every GPU benchmark needs either a vendor OpenCL runtime or the
    CUDA toolkit, so on a bare machine the entire category skips.

    It now lives once in the shared engine rather than in each command wrapper, so validate,
    benchmark and run all reach it through the same path."""
    import inspect

    assert "_offer_gpu_setup" in inspect.getsource(cli._execute_tests)


def test_the_token_prompt_comes_before_the_gpu_offer():
    """Both are human steps, and the device code is the quick one. Asking for it before the driver
    install means the operator is not made to wait through a download before the prompt they could
    have answered in seconds. See _execute_tests step 0b."""
    import inspect

    source = inspect.getsource(cli._execute_tests)

    assert source.index("_preflight_token") < source.index("_offer_gpu_setup")


def test_the_os_question_comes_before_the_gpu_offer():
    """A note about a graphics card ahead of "is it all right to run on this distribution at all"
    answers the second question before the first. The OS guard is in the command wrapper and the
    offer is in the engine it calls, so the guard runs first by construction; pinned here so the
    wrapper cannot be reordered to reach the engine before guarding."""
    import inspect

    source = inspect.getsource(cli.cmd_run_all)

    assert source.index("_guard_supported_os") < source.index("_execute")


def test_the_skip_reasons_name_the_command(monkeypatch):
    """The second and larger fault. The stderr note is for somebody watching; the reviewable
    artifacts are the log and report.json, and a reason recorded there is the only form of this hint
    that survives the run."""
    from alma_certify import gpubuild
    from alma_certify.benchmarks import gpu as gpu_bench
    from alma_certify.config import Config
    from alma_certify.registry import RunContext
    from alma_certify.validate import nvidia as nvidia_mod

    # The bundled clpeak tarball is a packaging artifact, fetched into the tree or supplied as a
    # Source by the spec, and it is not in git. Stubbed so this test exercises the branch it is
    # about: without it ``applicable`` returns "no clpeak source shipped" first, and this passed
    # only on a tree where somebody had run ``make clpeak-source``.
    monkeypatch.setattr(gpubuild, "source_archive", lambda: "/data/clpeak-2.0.18.tar.gz")

    ctx = RunContext(
        Config.load("/nonexistent"), "/tmp", inventory={"summary": {"gpus": [L40S]}},
    )
    monkeypatch.setattr(gpu_bench, "runtimes_present", lambda: {
        "opencl": False, "vulkan": False, "cuda": False, "rocm": False,
    })
    monkeypatch.setattr(nvidia_mod, "find_nvcc", lambda: None)
    monkeypatch.setattr(
        nvidia_mod.procutil, "find_tool", lambda tool, extra_dirs=(): "/usr/bin/" + tool,
    )

    for reason in (
        gpu_bench.Clpeak().applicable(ctx),
        gpu_bench.CudaBandwidth().applicable(ctx),
        nvidia_mod.CudaVectorAdd().applicable(ctx),
    ):
        assert "alma-certify setup-gpu" in reason, reason


def test_clpeak_does_not_send_an_amd_card_to_an_nvidia_installer(monkeypatch):
    """``setup-gpu`` installs NVIDIA's driver, which provides NVIDIA's OpenCL ICD and nothing for a
    Radeon. Naming it there would waste somebody's time twice."""
    from alma_certify import gpubuild
    from alma_certify.benchmarks import gpu as gpu_bench
    from alma_certify.config import Config
    from alma_certify.registry import RunContext
    # The bundled clpeak tarball is a packaging artifact, fetched into the tree or supplied as a
    # Source by the spec, and it is not in git. Stubbed so this test exercises the branch it is
    # about: without it ``applicable`` returns "no clpeak source shipped" first, and this passed
    # only on a tree where somebody had run ``make clpeak-source``.
    monkeypatch.setattr(gpubuild, "source_archive", lambda: "/data/clpeak-2.0.18.tar.gz")

    amd = {
        "pci_ids": {"vendor": "Advanced Micro Devices, Inc. [AMD/ATI] [1002]",
                    "device": "Navi 31 [Radeon Pro W7900] [744c]"},
        "driver": "amdgpu", "driver_version": "6.10",
    }
    ctx = RunContext(Config.load("/nonexistent"), "/tmp", inventory={"summary": {"gpus": [amd]}})
    # The whole inventory, not two of its four entries: this machine has real Vulkan ICDs in
    # /usr/share/vulkan/icd.d, and patching only OpenCL and nvcc let them leak in, so the gate found
    # a runtime the test believed it had taken away.
    monkeypatch.setattr(gpu_bench, "runtimes_present", lambda: {
        "opencl": False, "vulkan": False, "cuda": False, "rocm": False,
    })

    reason = gpu_bench.Clpeak().applicable(ctx)

    assert "no GPU compute runtime" in reason
    assert "setup-gpu" not in reason
    # Every backend named, present or absent. "Vulkan was never mentioned" is what a reader said
    # about the version that only reported the two it happened to check.
    for backend in ("opencl", "vulkan", "cuda", "rocm"):
        assert backend in reason


# --- installing it rather than only mentioning it -------------------------------
#
# The reasoning is in ``_offer_gpu_setup``: the suite already installs sysbench and stress-ng from
# EPEL, a third-party repository it adds itself, unasked. Being more cautious about AlmaLinux's own
# default-enabled extras than about that is not a defensible line.


@pytest.mark.parametrize("major,alma,expected", [
    ("9", True, True), ("10", True, True),
    ("8", True, False), ("9", False, False), ("10", False, False),
])
def test_only_a_configured_repository_is_used_unasked(major, alma, expected):
    """9 and 10 reach the packages through ``extras``, which is enabled by default, so installing is
    the same act as installing clpeak. 8 and other rebuilds need NVIDIA's repository added, which is
    a third-party step and needs somebody to say yes."""
    assert gpusetup.repo_is_configured(major=major, is_almalinux=alma) is expected


def _attempt(ok=True, reason=None, reboot=False, modules=("nvidia", "nvidia_uvm")):
    return gpusetup.LoadAttempt(
        ok=ok, modules=list(modules), reason=reason, reboot_may_help=reboot,
    )


def _run_scene(
    monkeypatch, state, *, major="10.1", alma=True, answer=None, flags=None,
    load=None, after_load=None, reboot_answer="continue",
):
    """One offer, with the machine and the operator's answer both decided by the caller.

    ``load`` is the ``LoadAttempt`` a stubbed ``try_load`` returns, and ``loaded`` in the result
    says whether it was called. Stubbed rather than allowed through, because the real one runs
    modprobe, and a test suite that inserts kernel modules into the machine running it is not one.

    ``reboot_answer`` answers the separate pause that fires when an installed driver fails to load
    and a reboot could fix it. It defaults to "continue" so a scene that only cares about the
    install still runs on; a test about the pause itself sets it. Returns ``(ran, asked, loaded)``
    and, so a pausing scene can be driven, the offer's own proceed/stop is not needed by callers.
    """
    ran = []
    asked = []
    loaded = []
    states = [state] + list(after_load or [])
    monkeypatch.setattr(cli, "_gpu_summary", lambda: {"gpus": [L40S]})
    monkeypatch.setattr(
        cli.gpusetup, "cuda_state",
        lambda summary: states[min(len(loaded), len(states) - 1)],
    )
    # The offer no longer gates the install on live_media_reason: it blocks only on
    # install_impossible_reason (container / read-only root) and caveats live media. Both stubbed
    # off here so an ordinary scene is neither, and a CI runner inside a container does not trip it.
    # cannot_load_reason is the same guard on the *load* path and needs the same treatment: without
    # it every load scene here fails on AlmaLinux CI, which runs the suite inside a container.
    monkeypatch.setattr(cli.gpusetup, "install_impossible_reason", lambda: None)
    monkeypatch.setattr(cli.gpusetup, "cannot_load_reason", lambda: None)
    monkeypatch.setattr(cli.gpusetup, "on_live_media", lambda: False)
    monkeypatch.setattr(cli.gpusetup, "foreign_kernel_reason", lambda: None)
    monkeypatch.setattr(
        cli.gpusetup, "try_load",
        lambda log=None: loaded.append(1) or (load if load is not None else _attempt()),
    )
    monkeypatch.setattr(
        cli.hostos, "detect",
        lambda *a, **k: cli.hostos.HostOS(
            "almalinux" if alma else "rocky", major, "test",
        ),
    )
    monkeypatch.setattr(
        cli.gpusetup, "run_plan", lambda steps, log=None: ran.append(steps) or None,
    )
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: answer is not None)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: answer is not None)
    if answer is not None:
        # The reboot pause has its own prompt; route to reboot_answer so a load-failure scene that
        # is not about the pause carries on rather than stopping on the shared install answer.
        def _input(prompt=""):
            asked.append(prompt)
            return reboot_answer if "Reboot" in prompt else answer
        monkeypatch.setattr("builtins.input", _input)
    flags = flags or {}
    cli._offer_gpu_setup(
        gpu_setup=bool(flags.get("gpu_setup", False)),
        no_gpu_setup=bool(flags.get("no_gpu_setup", False)),
    )
    return ran, asked, loaded


def test_it_asks_rather_than_naming_another_command(monkeypatch, capsys):
    """The change asked for. Sending somebody to ``setup-gpu`` when they have already typed a
    command, on a machine whose card is sitting there unusable, is making them relay a message to
    themselves."""
    ran, asked, _ = _run_scene(monkeypatch, gpusetup.STATE_TOOLKIT_MISSING, answer="y")

    assert asked, "it should have asked rather than printed advice"
    assert "install this now?" in asked[0]
    assert ran, "answering yes should install"
    assert "setup-gpu" not in capsys.readouterr().err, (
        "the point is that it no longer sends somebody to another command"
    )


def test_no_is_the_default_and_is_respected(monkeypatch, capsys):
    """Installing a driver is the consequential answer, so a bare Enter must not choose it."""
    ran, _, loaded = _run_scene(monkeypatch, gpusetup.STATE_DRIVER_MISSING, answer="")

    assert not ran
    assert "continuing without it" in capsys.readouterr().err


def test_the_install_is_recorded_for_the_report(monkeypatch):
    """``environment.installed_packages`` cannot show it: that list is ``pkg.newly_installed``,
    which tracks only what went through ``alma_certify.pkg``, and this is a dnf the plan runs
    directly. So the report did not say the driver had been installed during the run at all,
    which is half of what "the driver was not there at boot" means."""
    ran, _, _ = _run_scene(monkeypatch, gpusetup.STATE_DRIVER_MISSING, answer="yes")

    assert ran
    record = gpusetup.load_record()
    assert any("nvidia-open" in cmd for cmd in record["installed_during_run"])


def test_a_driver_can_now_be_installed_from_the_run(monkeypatch, capsys):
    """It used to be refused here and deferred to another command. With a prompt there is no reason
    to refuse: the operator is being asked."""
    ran, _, loaded = _run_scene(monkeypatch, gpusetup.STATE_DRIVER_MISSING, answer="yes")

    assert ran
    assert loaded, "installing the driver has to be followed by trying to load it"
    err = capsys.readouterr().err
    assert "can certify the card after all" in err


def test_a_driver_that_loads_is_not_sent_off_to_reboot(monkeypatch, capsys):
    """The whole point of trying. Telling somebody to reboot a machine whose driver is up and
    answering wastes their time and loses the run they have already paid for.

    Asserted on the advice rather than on the word: the offer itself names a reboot as what happens
    if the load fails, and saying so before is honest.
    """
    _run_scene(monkeypatch, gpusetup.STATE_DRIVER_MISSING, answer="yes", load=_attempt(ok=True))

    err = capsys.readouterr().err
    for advice in ("Reboot and run again", "# reboot", "will skip"):
        assert advice not in err
    assert "can certify the card after all" in err


def test_a_driver_that_does_not_load_says_so_and_the_run_goes_on(monkeypatch, capsys):
    """The CPU, memory, and storage work is still worth having; only the GPU part is out of
    reach."""
    ran, _, _ = _run_scene(
        monkeypatch, gpusetup.STATE_DRIVER_MISSING, answer="yes",
        load=_attempt(ok=False, reason="the module is not built for the running kernel",
                      reboot=True, modules=[]),
    )

    assert ran
    err = capsys.readouterr().err
    assert "not built for the running kernel" in err
    assert "will skip on this run" in err
    assert "Reboot and run again" in err


def test_a_failure_a_reboot_cannot_fix_is_not_answered_with_a_reboot(monkeypatch, capsys):
    """Secure Boot rejecting the signature is the case where the obvious advice is wrong: the module
    is there, the kernel will not have it, and rebooting comes back to the same place."""
    _run_scene(
        monkeypatch, gpusetup.STATE_DRIVER_MISSING, answer="yes",
        load=_attempt(ok=False, reason="the kernel refused the module's signature", reboot=False,
                      modules=[]),
    )

    err = capsys.readouterr().err
    assert "refused the module's signature" in err
    assert "Reboot and run again" not in err


# --- pausing so the operator can reboot -----------------------------------------
#
# Reported: when the driver installs but will not load, the message saying to reboot scrolled past
# and the run carried on for hours without the GPU. The tester almost always wants to reboot at that
# point, so the run stops there and asks rather than gliding by. ``_stop_to_reboot`` is that
# decision, and it only ever stops where a reboot could actually help.


def test_a_failed_load_stops_by_default_so_the_operator_can_reboot(monkeypatch):
    """A bare Enter stops the run: the reboot is what the tester came for, so it is the default, and
    the consequential answer (carry on without the GPU) is the one that has to be typed."""
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    assert cli._stop_to_reboot(_attempt(ok=False, reboot=True, modules=[]), live=False) is True


def test_continuing_is_possible_when_the_operator_would_rather_not_reboot(monkeypatch):
    """The default is not a trap. Someone who wants the CPU/memory/storage coverage now and the GPU
    on a later run types continue, and the run goes on without the GPU."""
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    for word in ("continue", "c"):
        monkeypatch.setattr("builtins.input", lambda prompt="", word=word: word)
        assert cli._stop_to_reboot(_attempt(ok=False, reboot=True, modules=[]), live=False) is False


def test_a_reboot_that_cannot_help_does_not_pause(monkeypatch):
    """Secure Boot rejecting the signature is not fixed by a reboot, so pausing to offer one would
    strand the operator at a prompt whose yes changes nothing. It must not even ask."""
    asked = []
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "")

    assert cli._stop_to_reboot(_attempt(ok=False, reboot=False, modules=[]), live=False) is False
    assert not asked


def test_live_media_does_not_pause_to_reboot(monkeypatch):
    """A reboot loses the whole install on live media, so it is the one place the reboot is bad
    advice even when the load could otherwise be fixed by one. No pause."""
    asked = []
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "")

    assert cli._stop_to_reboot(_attempt(ok=False, reboot=True, modules=[]), live=True) is False
    assert not asked


def test_an_unattended_run_carries_on_rather_than_hanging_at_the_pause(monkeypatch):
    """There is nobody to answer a prompt on a kickstart or a CI runner, and reading EOF as "stop"
    would abort a run somebody wanted to complete. With no terminal it carries on, message already
    printed."""
    asked = []
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "")

    assert cli._stop_to_reboot(_attempt(ok=False, reboot=True, modules=[]), live=False) is False
    assert not asked


def test_the_offer_stops_the_run_when_the_operator_chooses_to_reboot(monkeypatch, capsys):
    """End to end through the offer: a driver that installs and will not load, with a bare Enter at
    the pause, makes the offer return False so the engine can stop before spending hours on a run
    that was going to miss GPU coverage anyway."""
    monkeypatch.setattr(cli, "_gpu_summary", lambda: {"gpus": [L40S]})
    monkeypatch.setattr(cli.gpusetup, "cuda_state", lambda summary: gpusetup.STATE_DRIVER_MISSING)
    monkeypatch.setattr(cli.gpusetup, "install_impossible_reason", lambda: None)
    monkeypatch.setattr(cli.gpusetup, "on_live_media", lambda: False)
    monkeypatch.setattr(cli.gpusetup, "run_plan", lambda steps, log=None: None)
    monkeypatch.setattr(
        cli.gpusetup, "try_load",
        lambda log=None: _attempt(ok=False, reason="not built for the running kernel", reboot=True,
                                  modules=[]),
    )
    monkeypatch.setattr(
        cli.hostos, "detect",
        lambda *a, **k: cli.hostos.HostOS("almalinux", "10.1", "test"),
    )
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    # Yes to the install, then Enter (the default) at the reboot pause.
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": "" if "Reboot" in prompt else "yes",
    )

    assert cli._offer_gpu_setup(gpu_setup=False, no_gpu_setup=False) is False
    assert "stopping so you can reboot" in capsys.readouterr().err


def test_the_engine_stops_before_the_tests_when_the_offer_says_to_reboot(monkeypatch):
    """The abort path in ``_execute_tests``: when the offer returns False the run ends at EXIT_OK
    with a line telling the operator to run the same command again after the reboot, rather than
    grinding through the CPU/memory/storage tests it can still do."""
    import inspect

    source = inspect.getsource(cli._execute_tests)
    # The offer's result gates the return, and the abort returns EXIT_OK (a stop the operator asked
    # for is not a failure) with the re-run hint. Distinct from the --require-gpu path beside it,
    # which is a fault and returns EXIT_ERROR.
    assert "proceed = _offer_gpu_setup(" in source
    assert "if not proceed:" in source
    assert "return EXIT_OK" in source
    assert "run the same" in source
    reboot_branch = source.split("if not proceed:")[1][:400]
    assert "EXIT_ERROR" not in reboot_branch, (
        "choosing to reboot is not a failure and must not share the fault exit code"
    )


# --- the driver is installed and simply not loaded ------------------------------
#
# No install, no repository, no download: the whole fix is a modprobe. Worth its own path because
# printing an install plan for a machine that already has the packages misdescribes what is about to
# happen to it.


def test_an_installed_driver_is_offered_a_load_rather_than_an_install(monkeypatch, capsys):
    ran, asked, loaded = _run_scene(
        monkeypatch, gpusetup.STATE_DRIVER_NOT_LOADED, answer="y",
        after_load=[gpusetup.STATE_READY],
    )

    assert asked and "load this now?" in asked[0]
    assert not ran, "nothing should be installed: the packages are already there"
    assert loaded
    out = capsys.readouterr().err
    assert "modprobe nvidia" in out
    assert "dnf" not in out


def test_a_load_that_works_lets_the_run_carry_on_to_the_toolkit(monkeypatch, capsys):
    """The two can both be wrong at once: a machine that never rebooted after installing the driver
    may have no toolkit either, and the state is asked again rather than assumed."""
    ran, asked, loaded = _run_scene(
        monkeypatch, gpusetup.STATE_DRIVER_NOT_LOADED, answer="y",
        after_load=[gpusetup.STATE_TOOLKIT_MISSING],
    )

    assert loaded
    assert ran, "with the driver up and no toolkit, the toolkit is the next offer"
    assert len(asked) == 2, asked
    assert "load this now?" in asked[0]
    assert "install this now?" in asked[1]


def test_a_load_that_fails_does_not_go_on_to_offer_anything_else(monkeypatch, capsys):
    ran, _, loaded = _run_scene(
        monkeypatch, gpusetup.STATE_DRIVER_NOT_LOADED, answer="y",
        load=_attempt(ok=False, reason="the module is not built for the running kernel",
                      reboot=True, modules=[]),
        after_load=[gpusetup.STATE_TOOLKIT_MISSING],
    )

    assert loaded
    assert not ran
    assert "will skip on this run" in capsys.readouterr().err


def test_declining_the_load_leaves_the_kernel_alone(monkeypatch, capsys):
    """Inserting a module changes the running kernel, so it is asked rather than just done."""
    ran, _, loaded = _run_scene(monkeypatch, gpusetup.STATE_DRIVER_NOT_LOADED, answer="")

    assert not loaded
    assert not ran
    assert "continuing without it" in capsys.readouterr().err


def test_the_flag_loads_without_asking(monkeypatch):
    """Somebody who passed --gpu-setup to have a driver installed unattended wants it loaded too."""
    _, asked, loaded = _run_scene(
        monkeypatch, gpusetup.STATE_DRIVER_NOT_LOADED, answer=None,
        flags={"gpu_setup": True}, after_load=[gpusetup.STATE_READY],
    )

    assert loaded
    assert not asked


def test_a_container_is_not_offered_a_load(monkeypatch, capsys):
    """The kernel belongs to the host, so there is nothing here to load into.

    Checked with ``foreign_kernel_reason`` rather than ``live_media_reason``, which is the wider
    question: live media and a read-only root both stop an install being worth doing, and neither
    stops a module already on the disk from being loaded. On live media a modprobe is the only thing
    that could possibly help.
    """

    loaded = []
    monkeypatch.setattr(cli, "_gpu_summary", lambda: {"gpus": [L40S]})
    monkeypatch.setattr(
        cli.gpusetup, "cuda_state", lambda summary: gpusetup.STATE_DRIVER_NOT_LOADED,
    )
    monkeypatch.setattr(
        cli.gpusetup, "foreign_kernel_reason", lambda: "this is a container, so the kernel belongs "
        "to the host",
    )
    monkeypatch.setattr(cli.gpusetup, "try_load", lambda log=None: loaded.append(1))

    cli._offer_gpu_setup(gpu_setup=False, no_gpu_setup=False)

    assert not loaded
    assert "container" in capsys.readouterr().err


def test_eight_says_it_is_adding_a_third_party_repository(monkeypatch, capsys):
    """Material information for consent, and the reason a prompt is the right mechanism here rather
    than the silent install a configured repository would justify."""
    ran, _, loaded = _run_scene(
        monkeypatch, gpusetup.STATE_TOOLKIT_MISSING, major="8.10", answer="y",
    )

    assert ran
    assert "not part of AlmaLinux" in capsys.readouterr().err


def test_a_configured_repository_is_not_called_third_party(monkeypatch, capsys):
    _run_scene(monkeypatch, gpusetup.STATE_TOOLKIT_MISSING, answer="y")

    assert "not part of AlmaLinux" not in capsys.readouterr().err


def test_no_terminal_skips_and_names_the_flags(monkeypatch, capsys):
    """Prompting into a closed stdin would hang a kickstart %post forever, and reading EOF as
    consent would opt somebody in silently."""
    ran, _, loaded = _run_scene(monkeypatch, gpusetup.STATE_TOOLKIT_MISSING, answer=None)

    assert not ran
    # ``--gpu-setup``, which is the flag this path actually has. It used to name ``--yes``, which
    # belongs to the ``setup-gpu`` command: somebody in a kickstart told to pass a flag that does
    # not exist here is left with nothing to try.
    assert "--gpu-setup" in capsys.readouterr().err


def test_the_flag_installs_without_asking(monkeypatch, capsys):
    """For provisioning and CI, where there is nobody to ask."""
    ran, _, loaded = _run_scene(
        monkeypatch, gpusetup.STATE_DRIVER_MISSING, answer=None, flags={"gpu_setup": True},
    )

    assert ran


def test_live_media_is_attempted_with_a_caveat(monkeypatch, capsys):
    """Live media is impermanent, not impossible: the install lands in the session's overlay and the
    driver loads into the running kernel, which is the whole of what a one-shot run needs. So it is
    attempted - with a caveat that nothing survives a reboot - rather than skipped. Skipping it was
    the reported regression."""

    ran = []
    monkeypatch.setattr(cli, "_gpu_summary", lambda: {"gpus": [L40S]})
    monkeypatch.setattr(
        cli.gpusetup, "cuda_state", lambda summary: gpusetup.STATE_DRIVER_MISSING,
    )
    # Not a container and not a read-only root, but it IS live media.
    monkeypatch.setattr(cli.gpusetup, "install_impossible_reason", lambda: None)
    monkeypatch.setattr(cli.gpusetup, "on_live_media", lambda: True)
    monkeypatch.setattr(
        cli.hostos, "detect", lambda *a, **k: cli.hostos.HostOS("almalinux", "10", "test"),
    )
    monkeypatch.setattr(
        cli.gpusetup, "run_plan", lambda steps, log=None: ran.append(steps) or None,
    )
    monkeypatch.setattr(
        cli.gpusetup, "try_load",
        lambda log=None: gpusetup.LoadAttempt(
            ok=True, modules=["nvidia"], reason=None, reboot_may_help=False,
        ),
    )

    # --gpu-setup so the consequential answer is given without a terminal.
    cli._offer_gpu_setup(gpu_setup=True, no_gpu_setup=False)

    assert ran, "live media should be attempted (install + modprobe), not skipped"
    err = capsys.readouterr().err
    assert "live media" in err
    assert "not offering to install it" not in err


def test_the_flag_declines_everything(monkeypatch):

    called = []
    monkeypatch.setattr(cli, "_gpu_summary", lambda: called.append(1) or {"gpus": []})

    cli._offer_gpu_setup(no_gpu_setup=True)

    assert not called, "--no-gpu-setup should not even look"


# --- a long install has to look alive -------------------------------------------
#
# ``run_cmd`` uses ``communicate`` by default, which buffers until the process exits, and the tee
# writes afterwards. So a CUDA stack install, gigabytes and several minutes, showed nothing at all
# until it finished and was indistinguishable from a hang.


def test_the_commands_output_is_reported_as_it_arrives(tmp_path):
    """dnf's own words rather than a summary invented from them: it knows what it is doing and we
    would only be guessing from its output anyway."""
    seen = []
    steps = [gpusetup.Step("install things", ["sh", "-c", "echo first; echo second"])]

    assert gpusetup.run_plan(steps, log=seen.append) is None

    body = "\n".join(seen)
    assert "first" in body and "second" in body


def test_output_arrives_before_the_command_finishes():
    """The whole point. Collected at the end it would be a log, not a sign of life.

    Counted rather than matched on content, which is how the first version of this test fooled
    itself: ``run_plan`` echoes the command before running it, and with ``sh -c`` the script text
    *is* the command, so looking for a word the script echoes found it in the echo. Any message
    beyond the two preamble lines can only have come from the running command.
    """
    import threading
    import time

    steps = [gpusetup.Step("slow thing", ["sh", "-c", "echo alive; sleep 2"])]
    preamble = 2  # the step's reason, then the command line
    streamed = threading.Event()
    seen = []

    def log(message):
        seen.append(message)
        if len(seen) > preamble:
            streamed.set()

    started = time.monotonic()
    thread = threading.Thread(target=gpusetup.run_plan, args=(steps,), kwargs={"log": log})
    thread.start()
    appeared = streamed.wait(timeout=1.5)
    thread.join(timeout=10)

    assert appeared, "output should arrive while the command is still running, not after it"
    assert time.monotonic() - started >= 1.5, "the premise: the command really did take a while"
    assert any("alive" in line for line in seen[preamble:])


def test_a_failure_still_ends_with_its_reason(tmp_path):
    """Streamed output scrolls past, so the reason has to be repeated next to the failure rather
    than left somewhere above."""
    seen = []
    steps = [gpusetup.Step(
        "fail", ["sh", "-c", "echo noise; echo 'No match for argument: nope' >&2; exit 1"],
    )]

    failed = gpusetup.run_plan(steps, log=seen.append)

    assert failed is steps[0]
    assert "No match for argument: nope" in seen[-1]


# --- what the operator is shown before consenting -------------------------------
#
# The plan is the consent. A plan that lists four dnf commands and then also inserts modules into
# the running kernel has taken consent for something narrower than what happens.


def _setup_gpu(monkeypatch, state, *, dry_run=True, answer=None):
    """``alma-certify setup-gpu``, with the machine decided by the caller and nothing installed."""
    import argparse

    monkeypatch.setattr(cli, "_gpu_summary", lambda: {"gpus": [L40S]})
    monkeypatch.setattr(cli.gpusetup, "cuda_state", lambda summary: state)
    monkeypatch.setattr(cli.gpusetup, "install_impossible_reason", lambda: None)
    monkeypatch.setattr(cli.gpusetup, "on_live_media", lambda: False)
    monkeypatch.setattr(cli.gpusetup, "cannot_load_reason", lambda: None)
    monkeypatch.setattr(
        cli.hostos, "detect", lambda *a, **k: cli.hostos.HostOS("almalinux", "10.1", "test"),
    )
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: answer is not None)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: answer is not None)
    if answer is not None:
        monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    return cli.cmd_setup_gpu(argparse.Namespace(dry_run=dry_run, yes=False))


def test_the_dry_run_for_a_missing_driver_shows_the_modprobes_too(monkeypatch, capsys):
    """It used to promise four dnf commands and say nothing about inserting modules into the running
    kernel, which is the part a change-control reviewer or a provisioning author needs to see."""
    assert _setup_gpu(monkeypatch, gpusetup.STATE_DRIVER_MISSING) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert "dnf -y install nvidia-open cuda" in out
    for step in gpusetup.load_plan():
        assert " ".join(step.argv) in out


def test_the_toolkit_dry_run_does_not_promise_modprobes(monkeypatch, capsys):
    """Nothing is loaded on that path: the driver is already up, and only nvcc was missing."""
    assert _setup_gpu(monkeypatch, gpusetup.STATE_TOOLKIT_MISSING) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert "cuda-toolkit" in out
    assert "modprobe" not in out


def test_a_driver_that_only_needs_loading_is_shown_no_dnf(monkeypatch, capsys):
    assert _setup_gpu(monkeypatch, gpusetup.STATE_DRIVER_NOT_LOADED) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert "modprobe nvidia" in out
    assert "dnf" not in out


# --- asking through whatever is driving the run ----------------------------------
#
# Reported by a tester: the interface hung on a progress bar, with a status line still naming the
# previous step ("checking your submission token"), on a machine with the NVIDIA drivers installed
# but no CUDA packages. That is exactly this offer: the state is TOOLKIT_MISSING, so it asks - and
# it asked by calling ``input()``. Textual owns the terminal during a resident run, so the prompt
# was never drawn and nothing could be typed at it. The same command in plain output asked, was
# answered, and carried on with the certification.
#
# The question now goes through RunHooks, so each front-end asks it in its own way.


class _Driver(cli.RunHooks):
    """Records what a run reported and answers its questions, the way a front-end would."""

    def __init__(self, answer=True):
        self.answer = answer
        self.asked = []
        self.statuses = []
        self.notes = []

    def status(self, msg):
        self.statuses.append(msg)

    def note(self, text=""):
        self.notes.append(text)

    def confirm(self, question, *, yes_label="y", no_label="n", default=False):
        self.asked.append(question)
        return self.answer


def _scene_hooks(monkeypatch, state, driver, **flags):
    """The same machine _run_scene builds, driven by ``driver`` rather than a terminal."""
    monkeypatch.setattr(cli, "_gpu_summary", lambda: {"gpus": [L40S]})
    monkeypatch.setattr(cli.gpusetup, "cuda_state", lambda summary: state)
    monkeypatch.setattr(cli.gpusetup, "install_impossible_reason", lambda: None)
    monkeypatch.setattr(cli.gpusetup, "cannot_load_reason", lambda: None)
    monkeypatch.setattr(cli.gpusetup, "on_live_media", lambda: False)
    monkeypatch.setattr(cli.gpusetup, "run_plan", lambda steps, log=None: None)
    monkeypatch.setattr(cli.gpusetup, "try_load", lambda log=None: _attempt())
    monkeypatch.setattr(
        cli.hostos, "detect", lambda *a, **k: cli.hostos.HostOS("almalinux", "10.1", "test"),
    )
    # If anything here still reaches for stdin the test must fail rather than block forever.
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": pytest.fail("the offer read stdin instead of asking its driver"),
    )
    return cli._offer_gpu_setup(hooks=driver, **flags)


def test_the_offer_asks_its_driver_and_never_stdin(monkeypatch):
    """The hang, pinned. Under the interface stdin belongs to Textual: a run that prompts there
    blocks on a question nobody can see, which is what happened on a machine with the drivers and
    no CUDA packages."""
    driver = _Driver(answer=True)

    assert _scene_hooks(monkeypatch, gpusetup.STATE_TOOLKIT_MISSING, driver) is True
    assert driver.asked == ["install this now?"]


def test_the_driver_load_offer_asks_the_same_way(monkeypatch):
    """The other question on this path: installed drivers that are not in the running kernel."""
    driver = _Driver(answer=False)

    _scene_hooks(monkeypatch, gpusetup.STATE_DRIVER_NOT_LOADED, driver)

    assert driver.asked == ["load this now?"]


def test_the_step_being_waited_on_is_the_one_reported(monkeypatch):
    """The other half of the report: the status line still said "checking your submission token",
    because that was the last step to set one and the GPU offer set none. A question with the wrong
    step named above it reads as a hang even once it is answerable."""
    driver = _Driver(answer=True)

    _scene_hooks(monkeypatch, gpusetup.STATE_TOOLKIT_MISSING, driver)

    assert driver.statuses, "the offer must say which step it is"
    assert "GPU" in driver.statuses[0]
    assert any("install" in status for status in driver.statuses), (
        "and say so again before the long part, which is the download"
    )


def test_a_driver_that_cannot_ask_declines_and_names_the_flag(monkeypatch):
    """A headless driver is not a no: the run carries on without the GPU and says what to pass to
    have it installed unattended, the same as a CLI run with no terminal."""
    class _Headless(_Driver):
        def confirm(self, question, *, yes_label="y", no_label="n", default=False):
            self.asked.append(question)
            return None

    driver = _Headless()
    installed = []
    monkeypatch.setattr(cli.gpusetup, "note_install", lambda steps, failed: installed.append(1))

    assert _scene_hooks(monkeypatch, gpusetup.STATE_TOOLKIT_MISSING, driver) is True
    assert not installed, "it must not install without an answer"
    assert any("--gpu-setup" in note for note in driver.notes)


def test_what_it_is_about_to_change_reaches_the_driver_too(monkeypatch):
    """The reason for the question traveled on stderr, which a resident run has no way to show.
    Somebody asked to approve a third-party repository has to be able to read what it is."""
    driver = _Driver(answer=False)

    _scene_hooks(monkeypatch, gpusetup.STATE_TOOLKIT_MISSING, driver)

    assert any("dnf" in note for note in driver.notes), "the commands it would run"
    assert driver.notes, "the description of what is missing"


def test_the_engine_hands_its_driver_to_the_offer():
    """Guard: the offer defaults to a terminal driver, so forgetting to pass hooks here would put
    the prompt back on stdin and hang the interface again, silently."""
    import inspect

    source = inspect.getsource(cli._execute_tests)
    assert "hooks=hooks," in source[source.index("_offer_gpu_setup"):]


# --- --require-gpu ----------------------------------------------------------------
#
# Reported from CI: a GPU job where the driver stack failed to install reported success on every
# step. The suite was right - an install that fails is a note, the GPU checks skip and say why, and
# a person certifying a machine gets to decide what to do about it - but a CI job whose entire
# purpose was to prove the stack installs then exits 0 having proved nothing.
#
# So the behavior stays and the flag opts into the other answer.


def _require_scene(monkeypatch, state, *, summary=None, install_fails=False, load_ok=True,
                   blocked=None, require=True):
    """The offer on a machine the caller describes, with --require-gpu on unless told otherwise."""
    monkeypatch.setattr(cli, "_gpu_summary",
                        lambda: summary if summary is not None else {"gpus": [L40S]})
    monkeypatch.setattr(cli.gpusetup, "cuda_state", lambda s: state)
    monkeypatch.setattr(cli.gpusetup, "install_impossible_reason", lambda: blocked)
    monkeypatch.setattr(cli.gpusetup, "cannot_load_reason", lambda: None)
    monkeypatch.setattr(cli.gpusetup, "on_live_media", lambda: False)
    monkeypatch.setattr(cli.gpusetup, "note_install", lambda steps, failed: None)
    monkeypatch.setattr(
        cli.gpusetup, "run_plan",
        lambda steps, log=None: (steps[0] if install_fails else None),
    )
    monkeypatch.setattr(
        cli.gpusetup, "try_load",
        lambda log=None: _attempt(ok=load_ok, reason=None if load_ok else "no such device"),
    )
    monkeypatch.setattr(
        cli.hostos, "detect", lambda *a, **k: cli.hostos.HostOS("almalinux", "10.1", "test"),
    )
    return cli._offer_gpu_setup(gpu_setup=True, require=require, hooks=_Driver(answer=True))


def test_without_the_flag_a_failed_install_lets_the_run_continue(monkeypatch):
    """The behavior that has to stay. Somebody certifying a machine gets the rest of their run and
    a note saying the GPU checks skipped, rather than an aborted three-hour pass."""
    assert _require_scene(monkeypatch, gpusetup.STATE_TOOLKIT_MISSING,
                          install_fails=True, require=False) is True


def test_a_failed_install_is_fatal_with_the_flag(monkeypatch):
    """The reported case: this is what reported success on every step."""
    with pytest.raises(cli.GpuNotUsable) as excinfo:
        _require_scene(monkeypatch, gpusetup.STATE_TOOLKIT_MISSING, install_fails=True)

    assert "could not" in str(excinfo.value)


def test_no_card_at_all_is_fatal_with_the_flag(monkeypatch):
    """A GPU runner with no GPU visible is a broken job, not an idle one. This is also what a
    missing pciutils looks like: lspci is the only way the suite enumerates cards, so without it
    every machine reports no-card and the setup finds nothing to do."""
    with pytest.raises(cli.GpuNotUsable) as excinfo:
        _require_scene(monkeypatch, gpusetup.STATE_NO_CARD, summary={"gpus": []})

    assert "no GPU was found" in str(excinfo.value)


def test_a_driver_that_will_not_load_is_fatal_with_the_flag(monkeypatch):
    with pytest.raises(cli.GpuNotUsable) as excinfo:
        _require_scene(monkeypatch, gpusetup.STATE_DRIVER_MISSING, load_ok=False)

    assert "would not load" in str(excinfo.value)


def test_being_unable_to_install_here_is_fatal_with_the_flag(monkeypatch):
    """A container or a read-only root. Without the flag this is advice; with it, it means the job
    was pointed at a machine that could never have passed."""
    with pytest.raises(cli.GpuNotUsable) as excinfo:
        _require_scene(monkeypatch, gpusetup.STATE_TOOLKIT_MISSING,
                       blocked="this is a container")

    assert "container" in str(excinfo.value)


def test_no_gpu_setup_and_require_gpu_together_are_fatal(monkeypatch):
    """Contradictory flags. --no-gpu-setup guarantees nothing gets installed, so a run that also
    demands a usable GPU cannot be satisfied; saying so beats skipping every check and exiting 0."""
    with pytest.raises(cli.GpuNotUsable):
        cli._offer_gpu_setup(no_gpu_setup=True, require=True, hooks=_Driver(answer=True))


def test_a_ready_machine_passes_the_requirement(monkeypatch):
    """The flag must not fail a job on the machine it was written for."""
    assert _require_scene(monkeypatch, gpusetup.STATE_READY) is True


def test_the_run_exits_non_zero_rather_than_reporting_success(monkeypatch):
    """End to end through the engine: the exception has to become an exit code, because the exit
    code is the only thing CI reads."""
    import inspect

    source = inspect.getsource(cli._execute_tests)
    assert "except GpuNotUsable" in source
    handler = source.split("except GpuNotUsable")[1].split("# 1. inventory")[0]
    assert "return EXIT_ERROR" in handler


def test_the_flag_is_honored_in_one_place_only():
    """The guard against the next path being forgotten. Every way out that leaves the machine
    without a usable GPU calls give_up(), which is the only thing that raises, so a new branch
    either goes through it or is visibly not doing so.

    Written as "one raiser" rather than by counting returns, because most of the returns are
    success paths and counting them says nothing about correctness.
    """
    import inspect
    import re

    source = inspect.getsource(cli._offer_gpu_setup)
    assert source.count("raise GpuNotUsable") == 1, "give_up must be the only raiser"

    reasons = re.findall(r'give_up\(\s*"([^"]{4,40})', source)
    assert len(reasons) >= 5, "only %d give-up paths route through the flag: %s" % (
        len(reasons), reasons,
    )


def test_a_card_that_disappears_after_the_load_is_not_a_pass(monkeypatch):
    """STATE_NO_CARD used to be bundled with STATE_READY here, so a machine reporting no card after
    its driver loaded returned success. Unlikely, and exactly the shape of the bug this flag exists
    to catch."""
    monkeypatch.setattr(cli, "_gpu_summary", lambda: {"gpus": [L40S]})
    monkeypatch.setattr(cli.gpusetup, "cuda_state",
                        lambda s: gpusetup.STATE_DRIVER_NOT_LOADED)
    monkeypatch.setattr(cli.gpusetup, "cannot_load_reason", lambda: None)
    monkeypatch.setattr(cli.gpusetup, "install_impossible_reason", lambda: None)
    monkeypatch.setattr(cli.gpusetup, "on_live_media", lambda: False)
    monkeypatch.setattr(cli, "_offer_driver_load",
                        lambda *a, **k: (gpusetup.STATE_NO_CARD, True))

    with pytest.raises(cli.GpuNotUsable) as excinfo:
        cli._offer_gpu_setup(gpu_setup=True, require=True, hooks=_Driver(answer=True))

    assert "no GPU was found" in str(excinfo.value)


def test_a_require_gpu_failure_prints_what_it_saw(monkeypatch):
    """Reported from CI: the job failed saying the driver would not come up, with no error anywhere
    in the output to explain it, while the nvidia-smi step afterwards worked fine.

    Both were true. try_load reports failure when nvidia-smi answers but nvidia_uvm never loaded,
    because kmod ignores softdep errors and nvidia-smi does not need uvm - so there is no dnf error
    to find, and the only evidence is in the fields recorded here. They were going into the report
    and not into the log, and a CI failure is read from the log.
    """
    import inspect

    source = inspect.getsource(cli._execute_tests)
    handler = source.split("except GpuNotUsable")[1].split("return EXIT_ERROR")[0]

    assert "load_record()" in handler, (
        "the failure has to print what was recorded, or the log says only that it failed"
    )
    for field in ("attempts", "installed_during_run"):
        assert field in handler, (
            "%s is the per-command detail; it must not be summarized away" % field
        )
