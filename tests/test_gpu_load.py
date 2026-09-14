"""Bringing an installed NVIDIA driver up in the kernel that is already running.

The suite used to tell anybody who installed a driver to reboot, on the reasoning that a kernel
module cannot be loaded into a kernel that has no business with it yet. That is not true of the
modules this suite installs, and the maintainer was right to ask:

- **AlmaLinux 9 and 10** install ``kmod-nvidia-open``, and its dependencies are
  ``kernel(<symbol>) = <checksum>`` pairs rather than one kernel version, with ``weak-modules`` in
  its scriptlets to link it into every installed kernel whose kABI it matches. Verified from the
  repository's own metadata: there is no DKMS package in it at all.
- **AlmaLinux 8 and other rebuilds** install ``kmod-nvidia-open-dkms``, which DKMS builds against
  the running kernel during the transaction.

So it usually works, and when it does not the reason matters: a module built for a different kernel
is fixed by a reboot and one Secure Boot will not accept is not. Nothing here runs modprobe for
real; what modprobe *says* was taken from kmod's own message strings and reproduced against kmod 34.
"""

import pytest

from alma_certify import gpusetup

SMI_OK = "GPU 0: NVIDIA L40S (UUID: GPU-6b5e6f6e-0000-0000-0000-000000000000)"
SMI_NO_DRIVER = (
    "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver. "
    "Make sure that the latest NVIDIA driver is installed and running."
)

# Real messages, in the shape modprobe emits them.
NOT_BUILT = (
    "modprobe: FATAL: Module nvidia not found in directory /lib/modules/5.14.0-570.el9.x86_64"
)
UNSIGNED = "modprobe: ERROR: could not insert 'nvidia': Required key not available"
REJECTED = "modprobe: ERROR: could not insert 'nvidia': Key was rejected by service"
WRONG_FORMAT = "modprobe: ERROR: could not insert 'nvidia': Exec format error"
NO_DEVICE = "modprobe: ERROR: could not insert 'nvidia': No such device"
NOT_ROOT = "modprobe: ERROR: could not insert 'nvidia': Operation not permitted"


class Res:
    """A ``run_cmd`` result, as much of one as this needs."""

    def __init__(self, ok=True, stdout="", stderr="", timed_out=False, returncode=None):
        self.ok = ok
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.returncode = returncode if returncode is not None else (0 if ok else 1)


class Machine:
    """A machine that answers modprobe and keeps a ``/proc/modules`` reflecting the result.

    Enough of one to matter: a module that loads appears in ``/proc/modules`` and one that fails
    does not, which is the difference the code has to see. ``answers`` is keyed on the module name,
    so a test can have ``nvidia`` load and ``nvidia_uvm`` fail, and ``preloaded`` seeds what was
    already there, which is how nouveau gets into the picture.
    """

    def __init__(self, preloaded=(), cards=None):
        self.calls = []
        self.answers = {}
        self.loaded = list(preloaded)
        # What sysfs says is bound to each NVIDIA card, which is a separate question from what is
        # loaded: the default is one card with nothing bound to it.
        self.cards = [("0000:01:00.0", None)] if cards is None else list(cards)

    def run_cmd(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0].endswith("nvidia-smi"):
            return self.answers.get("smi", Res(ok=True, stdout=SMI_OK))
        name = argv[-1]
        res = self.answers.get(name, Res(ok=True))
        if res.ok and name not in self.loaded:
            self.loaded.append(name)
        return res

    def read_file(self, path, default=None):
        if path != "/proc/modules":
            return default
        return "".join("%s 122880 0 - Live 0x0\n" % name for name in self.loaded)

    @property
    def modprobed(self):
        return [argv[-1] for argv in self.calls if argv[0].endswith("modprobe")]


@pytest.fixture
def machine(monkeypatch):
    """The default machine: modprobe present, everything loads, nothing loaded yet."""
    return _machine(monkeypatch)


def _machine(monkeypatch, preloaded=(), cards=None):
    box = Machine(preloaded, cards)
    monkeypatch.setattr(gpusetup, "nvidia_cards_by_driver", lambda: list(box.cards))
    monkeypatch.setattr(
        gpusetup.procutil, "find_tool",
        lambda tool, extra_dirs=(): "/usr/sbin/" + tool,
    )
    monkeypatch.setattr(gpusetup.procutil, "run_cmd", box.run_cmd)
    monkeypatch.setattr(gpusetup.procutil, "read_file", box.read_file)
    # A host whose kernel is its own. Stubbed rather than left to the real one, because CI runs
    # these tests inside an AlmaLinux container, where the real answer is "this is a container" and
    # every test of the attempt itself would be testing the refusal instead.
    monkeypatch.setattr(gpusetup, "cannot_load_reason", lambda: None)
    monkeypatch.setattr(
        gpusetup, "newer_kernel_installed", lambda running=None, modules_dir=None: False,
    )
    return box


# --- what is loaded -------------------------------------------------------------


def test_the_loaded_modules_come_from_proc_modules(monkeypatch):
    """A file read rather than lsmod, which is a formatter over this same file."""
    monkeypatch.setattr(
        gpusetup.procutil, "read_file",
        lambda path, default=None: (
            "nvidia_drm 122880 4 - Live 0xffffffffc2c00000\n"
            "nvidia_modeset 1642496 5 nvidia_drm, Live 0xffffffffc2a00000\n"
            "nvidia_uvm 6754304 0 - Live 0xffffffffc2200000\n"
            "nvidia 62066688 106 nvidia_uvm,nvidia_modeset, Live 0xffffffffbe400000\n"
            "xfs 2109440 2 - Live 0xffffffffc0800000\n"
        ),
    )

    assert gpusetup.loaded_nvidia_modules() == [
        "nvidia_drm", "nvidia_modeset", "nvidia_uvm", "nvidia",
    ]


def test_nothing_loaded_is_an_empty_list_not_a_crash(monkeypatch):
    monkeypatch.setattr(gpusetup.procutil, "read_file", lambda path, default=None: None)

    assert gpusetup.loaded_nvidia_modules() == []


def test_a_module_merely_starting_with_nvidia_is_not_counted(monkeypatch):
    """``nvidiafb`` is the framebuffer driver for Riva cards and is not part of this stack. Matched
    on the boundary so a name that happens to share the prefix is not read as ours."""
    monkeypatch.setattr(
        gpusetup.procutil, "read_file",
        lambda path, default=None: "nvidiafb 45056 0 - Live 0xffffffffc0100000\n",
    )

    assert gpusetup.loaded_nvidia_modules() == []


# --- what proves it -------------------------------------------------------------


def test_the_driver_is_proved_by_nvidia_smi_not_by_modprobes_exit_code(machine):
    """modprobe exiting 0 means a module was inserted, not that the driver came up."""
    ok, said = gpusetup.smi_check()

    assert ok
    assert said == SMI_OK
    assert machine.calls[0][1:] == ["-L"]


def test_nvidia_smis_own_words_are_kept(machine):
    """``NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver`` is a better
    sentence than anything invented from an exit code, and the failures worth telling apart differ
    only in that text."""
    machine.answers["smi"] = Res(ok=False, stderr=SMI_NO_DRIVER)

    ok, said = gpusetup.smi_check()

    assert not ok
    assert "couldn't communicate with the NVIDIA driver" in said


def test_a_driver_that_answers_with_no_output_is_not_taken_as_working(machine):
    """Exit zero and nothing to say is not evidence of a card."""
    machine.answers["smi"] = Res(ok=True, stdout="   \n")

    assert gpusetup.smi_check()[0] is False


def test_no_nvidia_smi_at_all_says_so(monkeypatch):
    monkeypatch.setattr(gpusetup.procutil, "find_tool", lambda tool, extra_dirs=(): None)

    ok, said = gpusetup.smi_check()

    assert not ok
    assert "not installed" in said


def test_the_smi_call_is_bounded(monkeypatch):
    """A card wedged in a bad state can make this hang, and a certification run that never returns
    is worse than one reporting a card it could not talk to."""
    seen = []
    monkeypatch.setattr(
        gpusetup.procutil, "find_tool", lambda tool, extra_dirs=(): "/usr/bin/" + tool,
    )
    monkeypatch.setattr(
        gpusetup.procutil, "run_cmd",
        lambda argv, **kw: seen.append(kw.get("timeout")) or Res(ok=True, stdout=SMI_OK),
    )

    gpusetup.smi_check()

    assert seen and seen[0] and seen[0] > 0


# --- is a reboot even worth suggesting ------------------------------------------


def test_a_newer_installed_kernel_is_found(tmp_path):
    """Through the real filesystem and the real ``os.path.exists``. Faking that one by patching it
    patches it for everything, pytest's own traceback machinery included, which is how the first
    attempt at this test brought the whole run down with an INTERNALERROR."""
    for release in ("5.14.0-570.9.1.el9_6.x86_64", "5.14.0-570.12.1.el9_6.x86_64"):
        (tmp_path / release).mkdir()
        (tmp_path / release / "modules.dep").write_text("")

    assert gpusetup.newer_kernel_installed(
        "5.14.0-570.9.1.el9_6.x86_64", modules_dir=str(tmp_path)) is True
    assert gpusetup.newer_kernel_installed(
        "5.14.0-570.12.1.el9_6.x86_64", modules_dir=str(tmp_path)) is False


def test_twelve_is_newer_than_nine():
    """String comparison gets this backwards, and it decides whether the operator is told to reboot
    a machine that a reboot would not change."""
    key = gpusetup._release_key

    assert key("5.14.0-570.12.1.el9_6.x86_64") > key("5.14.0-570.9.1.el9_6.x86_64")
    assert key("5.14.0-570.9.1.el9_6.x86_64") < key("5.14.0-571.1.1.el9_6.x86_64")
    assert key("6.12.0-55.el10.x86_64") > key("5.14.0-570.12.1.el9_6.x86_64")


def test_a_release_key_never_compares_an_int_with_a_str():
    """Python will not, so a total order over whatever shape a release string turns out to have is
    the requirement rather than a nicety."""
    key = gpusetup._release_key

    assert key("6.12.0-55.el10.x86_64") > key("6.12.0-rc1.el10.x86_64")
    assert sorted([key("4.18.0-553.el8"), key("6.12.0-55.el10"), key("5.14.0-570.el9")])


def test_a_directory_without_modules_is_not_a_kernel(tmp_path):
    """A leftover directory is not something that can be booted into. This machine has fifty of
    them under /lib/modules from kernels long since removed."""
    (tmp_path / "5.99.0-1.el9.x86_64").mkdir()

    assert gpusetup.newer_kernel_installed(
        "5.14.0-570.el9.x86_64", modules_dir=str(tmp_path)) is False


def test_an_unreadable_lib_modules_is_unknown_rather_than_no():
    """None, not False. "No newer kernel" is a claim, and this cannot make it."""
    assert gpusetup.newer_kernel_installed(
        "5.14.0-570.el9.x86_64", modules_dir="/no/such/directory") is None


# --- why it would not load ------------------------------------------------------


def test_secure_boot_is_not_answered_with_a_reboot():
    """The case where the obvious advice is wrong. The module is there and the kernel will not have
    it, so a reboot comes back to the same place; the key has to be enrolled."""
    for text in (UNSIGNED, REJECTED):
        reason, reboot = gpusetup.classify_load_failure(text, newer_kernel=True)

        assert reboot is False, text
        assert "Secure Boot" in reason
        assert "mokutil" in reason


def test_a_module_built_for_another_kernel_with_a_newer_one_installed_says_reboot():
    for text in (NOT_BUILT, WRONG_FORMAT):
        reason, reboot = gpusetup.classify_load_failure(text, newer_kernel=True)

        assert reboot is True, text
        assert "not built for the kernel that is running" in reason


def test_a_module_built_for_another_kernel_with_nothing_newer_does_not_say_reboot():
    """There is nothing to reboot into, so the honest answer is that the package and this kernel do
    not match rather than sending somebody round the loop again."""
    reason, reboot = gpusetup.classify_load_failure(NOT_BUILT, newer_kernel=False)

    assert reboot is False
    assert "no newer kernel" in reason
    # And it names where the answer is on 8, where DKMS builds the module locally and every line of
    # the kmod's install script ends in ``|| :``, so a failed build still leaves dnf exiting 0 and
    # this message is what the operator sees instead.
    assert "make.log" in reason


def test_not_knowing_whether_a_kernel_is_pending_still_suggests_the_usual_fix():
    reason, reboot = gpusetup.classify_load_failure(NOT_BUILT, newer_kernel=None)

    assert reboot is True
    assert "usual fix" in reason


def test_a_module_that_will_not_attach_names_both_reasons_for_it():
    """Not "there is no card": this only runs on a machine where lspci found one, so ENODEV is the
    module declining to attach to it. AlmaLinux's own documentation attributes this exact message to
    a driver and a running kernel that do not match, so the reboot is worth suggesting."""
    reason, reboot = gpusetup.classify_load_failure(NO_DEVICE, newer_kernel=True)

    assert reboot is True
    assert "did not attach to the card" in reason
    assert "different kernel" in reason


def test_being_refused_outright_names_both_reasons_for_it():
    reason, reboot = gpusetup.classify_load_failure(NOT_ROOT, newer_kernel=True)

    assert reboot is False
    assert "root" in reason
    assert "locked down" in reason


def test_an_unrecognized_failure_is_quoted_rather_than_guessed_at():
    """A message this does not know is still the best thing available to print, and inventing a
    category for it would be worse than passing it through."""
    reason, reboot = gpusetup.classify_load_failure(
        "modprobe: ERROR: could not insert 'nvidia': Cannot allocate memory", newer_kernel=False,
    )

    assert "Cannot allocate memory" in reason
    assert reboot is False


def test_a_failure_with_nothing_to_say_still_says_something():
    reason, _ = gpusetup.classify_load_failure("   \n", newer_kernel=None)

    assert reason


# --- the attempt ----------------------------------------------------------------


def test_the_modules_the_checks_need_are_loaded(machine):
    """``nvidia_uvm`` is the one that is easy to miss: without it nvidia-smi is perfectly happy and
    every CUDA call fails, so the run would report a working card and a broken toolkit. Nothing
    depends on it, so modprobe never pulls it in on its own."""
    attempt = gpusetup.try_load(log=lambda msg: None)

    assert attempt.ok
    assert "nvidia" in machine.modprobed
    assert "nvidia_uvm" in machine.modprobed
    assert machine.modprobed.index("nvidia") < machine.modprobed.index("nvidia_uvm")


def test_nouveau_is_never_unloaded(monkeypatch):
    """On a machine that booted with nouveau driving the console it is pinned by the framebuffer and
    will not go, and forcing the point on somebody's console is not something a certification run
    gets to do. The driver packages blacklist it and rebuild the initramfs, so the reboot this falls
    back to is what fixes that case."""
    box = _machine(monkeypatch, preloaded=["nouveau"])

    gpusetup.try_load(log=lambda msg: None)

    flat = " ".join(" ".join(argv) for argv in box.calls)
    assert "nouveau" not in flat
    assert "rmmod" not in flat
    assert " -r" not in flat, "modprobe -r is a removal"


def test_the_display_module_is_not_asked_for(machine):
    """``nvidia_drm`` was the module the maintainer named and the first version of this loaded it.
    It is not loaded now, and that is the deliberate part.

    It is display-only: nvidia-smi does not need it, no CUDA call goes through it, and no test here
    touches DRM. What it does is take the console. ``modinfo`` on the shipping ``kmod-nvidia-open``
    gives ``modeset`` and ``fbdev`` both defaulting to 1, and the module imports
    ``aperture_remove_conflicting_pci_devices``, so it unregisters the firmware framebuffer for that
    card and registers its own. On AlmaLinux 9, with CONFIG_FB_EFI, no simpledrm, and no deferred
    takeover, that is a live re-modeset of somebody's console with nothing to fall back to, and the
    documented way back is the ``modprobe -r`` this never does. All risk, no coverage.
    """
    gpusetup.try_load(log=lambda msg: None)

    assert "nvidia_drm" not in machine.modprobed
    assert "nvidia_modeset" not in machine.modprobed


def test_the_vendor_loading_it_anyway_is_not_fought(machine):
    """``nvidia-kmod-common`` ships ``softdep nvidia post: nvidia-uvm nvidia-drm``, so AlmaLinux
    loads nvidia_drm on ``modprobe nvidia`` whatever this asks for. Suppressing that would mean
    discarding the ``options nvidia`` lines in the same file and leaving the run not matching boot
    state, so nothing here tries to. Not asking is the whole of the position.
    """
    machine.loaded.extend(["nvidia_drm", "nvidia_modeset"])  # as the softdep would

    attempt = gpusetup.try_load(log=lambda msg: None)

    assert attempt.ok
    assert "nvidia_drm" in attempt.modules, "reported as loaded, because it is"
    assert "--ignore-install" not in " ".join(" ".join(a) for a in machine.calls)


def test_a_required_module_failing_is_reported_with_its_reason(machine):
    machine.answers["nvidia"] = Res(ok=False, stderr=NOT_BUILT)
    machine.answers["smi"] = Res(ok=False, stderr=SMI_NO_DRIVER)

    attempt = gpusetup.try_load(log=lambda msg: None)

    assert not attempt.ok
    assert "not built for the kernel that is running" in attempt.reason


def test_the_second_module_is_still_tried_after_the_first_fails(machine):
    """``nvidia_uvm`` can fail on its own account, and what the machine can actually do is decided
    by the check at the end rather than by the first thing to go wrong."""
    machine.answers["nvidia"] = Res(ok=False, stderr=NO_DEVICE)
    machine.answers["smi"] = Res(ok=False, stderr=SMI_NO_DRIVER)

    gpusetup.try_load(log=lambda msg: None)

    assert "nvidia_uvm" in machine.modprobed


def test_modprobe_succeeding_while_the_driver_stays_silent_is_a_failure(machine):
    """The reason nvidia-smi is asked at all. Every insert can work and the driver still not come
    up, and a run that took modprobe's word for it would certify a card it never spoke to."""
    machine.answers["smi"] = Res(ok=False, stderr=SMI_NO_DRIVER)

    attempt = gpusetup.try_load(log=lambda msg: None)

    assert not attempt.ok
    assert "couldn't communicate" in attempt.reason


def test_nouveau_holding_the_card_is_not_even_attempted(monkeypatch):
    """Verified against the packaging, and it changes the answer. Suppressing nouveau is not only a
    modprobe blacklist: ``nvidia-kmod-common`` puts ``rd.driver.blacklist=nouveau`` on the kernel
    command line and the kmod's ``%posttrans`` regenerates the initramfs. Both are boot-time by
    construction, so a card certified after a live module swap would have been certified in a
    software configuration nobody will ever boot into - and the modprobe would fail anyway, because
    nouveau holds the device.
    """
    box = _machine(monkeypatch, preloaded=["nouveau"],
                   cards=[("0000:01:00.0", "nouveau")])

    attempt = gpusetup.try_load(log=lambda msg: None)

    assert not attempt.ok
    assert attempt.reboot_may_help is True, "this is the case where a reboot really is the answer"
    assert "nouveau" in attempt.reason
    assert "only take effect at boot" in attempt.reason
    assert not box.modprobed, "nothing should have been attempted"


def test_novas_successor_counts_too(monkeypatch):
    """``nova_core`` is nouveau's Rust replacement and is on AlmaLinux 10. The driver packages
    blacklist both by name, so both have to be recognized here."""
    _machine(monkeypatch, preloaded=["nova_core"], cards=[("0000:01:00.0", "nova_core")])

    assert "nova_core" in (gpusetup.nouveau_holding() or "")


# --- loaded is not the same as holding ------------------------------------------
#
# Reported from a machine whose card was too new for the in-tree driver. The suite said nouveau was
# holding it, refused to modprobe, and so never reported the real fault, which was that the driver
# package had no module for the running kernel. ``modprobe nvidia`` by hand said exactly that.


def test_an_in_tree_driver_loaded_but_bound_to_nothing_does_not_block(monkeypatch):
    """lspci showed the card with no driver at all, so nothing was holding it and the vendor module
    had every chance of binding it."""
    _machine(monkeypatch, preloaded=["nouveau"], cards=[("0000:01:00.0", None)])

    assert gpusetup.nouveau_holding() is None


def test_the_real_fault_is_what_gets_reported(monkeypatch):
    """The whole point of not refusing: the modprobe runs, fails for the reason it actually failed,
    and ``classify_load_failure`` names it. This is the reported machine, end to end."""
    box = _machine(monkeypatch, preloaded=["nouveau"], cards=[("0000:01:00.0", None)])
    box.answers["nvidia"] = Res(ok=False, stderr=NOT_BUILT)
    box.answers["smi"] = Res(ok=False, stderr="NVIDIA-SMI has failed")

    attempt = gpusetup.try_load(log=lambda msg: None)

    assert box.modprobed[:1] == ["nvidia"], "refused to even try"
    assert "not built for the kernel that is running" in attempt.reason
    assert "nouveau" not in attempt.reason, "blamed the wrong thing"


def test_one_card_held_and_another_free_is_still_worth_attempting(monkeypatch):
    """nouveau can hold one card and not another, and the free one is the one the vendor module
    binds."""
    box = _machine(monkeypatch, preloaded=["nouveau"],
                   cards=[("0000:01:00.0", "nouveau"), ("0000:c1:00.0", None)])

    gpusetup.try_load(log=lambda msg: None)

    assert "nvidia" in box.modprobed


def test_every_card_held_is_still_refused(monkeypatch):
    """Two cards, both nouveau's, and there is nothing left for the vendor module to bind."""
    box = _machine(monkeypatch, preloaded=["nouveau"],
                   cards=[("0000:01:00.0", "nouveau"), ("0000:c1:00.0", "nouveau")])

    attempt = gpusetup.try_load(log=lambda msg: None)

    assert not box.modprobed
    assert "every NVIDIA card here is bound to nouveau" in attempt.reason


def test_sysfs_that_cannot_be_read_attempts_rather_than_guesses(monkeypatch):
    """A modprobe that fails says why. A refusal on a guess says nothing."""
    box = _machine(monkeypatch, preloaded=["nouveau"], cards=[])

    gpusetup.try_load(log=lambda msg: None)

    assert "nvidia" in box.modprobed


def test_the_bindings_come_from_sysfs(monkeypatch, tmp_path):
    """The reader itself, against a sysfs tree: an NVIDIA card bound to nouveau, one bound to
    nothing, and somebody else's device that is not ours to report."""
    root = tmp_path / "devices"
    for addr, vendor, driver in (
        ("0000:01:00.0", "0x10de", "nouveau"),
        ("0000:c1:00.0", "0x10de", None),
        ("0000:00:02.0", "0x8086", "i915"),
    ):
        device = root / addr
        device.mkdir(parents=True)
        (device / "vendor").write_text(vendor + "\n")
        if driver:
            target = tmp_path / "drivers" / driver
            target.mkdir(parents=True, exist_ok=True)
            (device / "driver").symlink_to(target)
    monkeypatch.setattr(gpusetup, "_PCI_DEVICES", str(root))

    assert gpusetup.nvidia_cards_by_driver() == [
        ("0000:01:00.0", "nouveau"), ("0000:c1:00.0", None),
    ]


def test_no_sysfs_at_all_is_not_an_error(monkeypatch):
    monkeypatch.setattr(gpusetup, "_PCI_DEVICES", "/nonexistent/pci")

    assert gpusetup.nvidia_cards_by_driver() == []


def test_a_machine_with_neither_is_not_held_back(monkeypatch):
    _machine(monkeypatch, preloaded=["xfs", "nvme"])

    assert gpusetup.nouveau_holding() is None


def test_a_driver_up_without_uvm_is_still_up(machine):
    """uvm is loaded on demand, so its absence right after a modprobe is the ordinary state.

    This used to be a load failure, on the reasoning that every CUDA call needs uvm and nvidia-smi
    does not, so a machine could be called ready and then fail three CUDA checks. True, but it made
    the common case look broken: nvidia-modprobe brings uvm in when something first opens a CUDA
    context, and the softdep brings it in at boot. Reported as a failure it cost a CI job that had a
    perfectly working card, with nvidia-smi answering in the next step.

    It is still attempted, and what actually loaded is still recorded. The CUDA checks decide.
    """
    machine.answers["nvidia_uvm"] = Res(ok=False, stderr=NO_DEVICE)

    attempt = gpusetup.try_load(log=lambda msg: None)

    assert attempt.ok, "nvidia-smi answered; uvm arriving later is not this function's call"
    assert "nvidia" in attempt.modules
    assert "nvidia_uvm" in machine.modprobed, "still asked for, so a dead softdep is still caught"


def test_uvm_missing_without_any_modprobe_error_is_recorded_not_judged(machine, monkeypatch):
    """The softdep case: nothing failed and the module is not there.

    Still visible, because the modules actually in the running kernel are read from /proc/modules
    rather than taken from what modprobe said. It is reported rather than treated as a broken
    driver, so a reviewer reading the run can see uvm did not come up while the run carries on to
    let the CUDA checks answer for themselves.
    """
    real = machine.run_cmd

    def sneaky(argv, **kwargs):
        res = real(argv, **kwargs)
        if argv[-1] == "nvidia_uvm" and "nvidia_uvm" in machine.loaded:
            machine.loaded.remove("nvidia_uvm")  # reported success, and is not there
        return res

    monkeypatch.setattr(gpusetup.procutil, "run_cmd", sneaky)

    attempt = gpusetup.try_load(log=lambda msg: None)

    assert attempt.ok
    assert "nvidia_uvm" not in attempt.modules, "what did load is reported truthfully"
    assert gpusetup.load_record()["modules_after"] == attempt.modules


def test_a_command_that_never_returns_is_a_finding_not_a_reboot(machine):
    """A modprobe or an nvidia-smi that hangs is what a card that has fallen off the bus looks like,
    and it is the most interesting thing a certification run could find. Sending the operator away
    to reboot would lose it. Before this the empty stderr fell through every named category to the
    last branch and came out as a guess about rebooting, drawn from the unrelated newer-kernel
    signal.
    """
    machine.answers["nvidia"] = Res(ok=False, timed_out=True)
    machine.answers["smi"] = Res(ok=False, stderr=SMI_NO_DRIVER)

    attempt = gpusetup.try_load(log=lambda msg: None)

    assert not attempt.ok
    assert attempt.reboot_may_help is False
    assert "did not return" in attempt.reason
    assert "hardware" in attempt.reason


def test_an_nvidia_smi_that_hangs_is_the_same_finding(machine):
    machine.answers["smi"] = Res(ok=False, timed_out=True)

    attempt = gpusetup.try_load(log=lambda msg: None)

    assert not attempt.ok
    assert attempt.reboot_may_help is False
    assert "did not return" in attempt.reason


def test_a_kernel_flavour_is_not_a_newer_kernel(tmp_path):
    """A verified bug in the first version of this. ``+debug``, ``+rt``, and aarch64's ``+64k`` are
    real AlmaLinux releases and real directory names, and every one of them sorts above the plain
    kernel, so a host with kernel-debug installed was told to reboot into a newer kernel. Rebooting
    could never make the running kernel the newest, because the suffix always wins, so the advice
    would have repeated forever and never come true.
    """
    running = "5.14.0-687.38.1.el9_8.x86_64"
    for release in (running, running + "+debug", running + "+rt"):
        (tmp_path / release).mkdir()
        (tmp_path / release / "modules.dep").write_text("")

    assert gpusetup.newer_kernel_installed(running, modules_dir=str(tmp_path)) is False


def test_a_newer_kernel_of_the_same_flavour_still_counts(tmp_path):
    """The other half: somebody running kernel-debug should still be told about a newer one."""
    for release in ("5.14.0-687.38.1.el9_8.x86_64+debug", "5.14.0-687.40.1.el9_8.x86_64+debug"):
        (tmp_path / release).mkdir()
        (tmp_path / release / "modules.dep").write_text("")

    assert gpusetup.newer_kernel_installed(
        "5.14.0-687.38.1.el9_8.x86_64+debug", modules_dir=str(tmp_path)) is True


def test_nothing_is_attempted_where_it_would_mean_nothing(monkeypatch):
    """Guarded inside ``try_load`` and not only at the offer, because this is the function that must
    not run on such a host and a caller added later would not know to check."""
    box = _machine(monkeypatch)
    monkeypatch.setattr(gpusetup, "cannot_load_reason", lambda: "this is a container")

    attempt = gpusetup.try_load(log=lambda msg: None)

    assert not attempt.ok
    assert not box.modprobed
    assert attempt.reboot_may_help is False
    assert "container" in attempt.reason


def test_no_modprobe_on_the_machine_is_not_a_reboot_problem(monkeypatch):
    # The container check comes first and would answer for this one, which is what happens on
    # AlmaLinux CI: the suite runs inside a container, so try_load reports the host's kernel
    # rather than ever looking for modprobe.
    monkeypatch.setattr(gpusetup, "cannot_load_reason", lambda: None)
    monkeypatch.setattr(gpusetup.procutil, "find_tool", lambda tool, extra_dirs=(): None)

    attempt = gpusetup.try_load(log=lambda msg: None)

    assert not attempt.ok
    assert attempt.reboot_may_help is False
    assert "modprobe is not installed" in attempt.reason


def test_every_module_it_loads_can_be_explained():
    """The plan is printed to somebody before it changes their running kernel, and
    ``modprobe nvidia_uvm`` does not explain itself."""
    plan = gpusetup.load_plan()

    assert [step.argv[-1] for step in plan] == list(
        gpusetup._REQUIRED_MODULES + gpusetup._OPTIONAL_MODULES
    )
    for step in plan:
        assert step.argv[0] == "modprobe"
        assert step.why and step.why != step.argv[-1]


def test_the_plan_matches_what_is_run(machine):
    """Two lists that could drift, and a plan that printed one thing and did another would be a lie
    to somebody who is about to consent to it."""
    plan = [step.argv[-1] for step in gpusetup.load_plan()]
    gpusetup.try_load(log=lambda msg: None)

    assert machine.modprobed == plan


# --- what the state means for installing ----------------------------------------


def test_there_is_no_install_plan_for_a_driver_that_only_needs_loading():
    """This used to fall through an ``else`` and install the entire driver and CUDA stack to fix a
    module that needed a modprobe."""
    with pytest.raises(ValueError):
        gpusetup.install_plan(gpusetup.STATE_DRIVER_NOT_LOADED, major="10")


def test_a_container_with_no_marker_file_is_still_a_container(monkeypatch):
    """containerd and CRI-O write neither /run/.containerenv nor /.dockerenv, and a privileged GPU
    pod on Kubernetes is the container this suite is most likely to meet. systemd sets
    ``container=`` for anything it starts inside one and the runtimes set it for themselves, so
    /proc/1/environ answers where the marker files do not.
    """
    monkeypatch.setattr(gpusetup, "_CONTAINER_MARKERS", ())
    monkeypatch.setattr(
        gpusetup.procutil, "read_file",
        lambda path, default=None: (
            "container=podman\x00HOME=/root\x00" if path == "/proc/1/environ" else default
        ),
    )

    assert "container" in (gpusetup.foreign_kernel_reason() or "")


def test_an_ordinary_hosts_pid_one_does_not_say_container(monkeypatch):
    monkeypatch.setattr(gpusetup, "_CONTAINER_MARKERS", ())
    monkeypatch.setattr(
        gpusetup.procutil, "read_file",
        lambda path, default=None: (
            "HOME=/\x00init=/usr/lib/systemd/systemd\x00" if path == "/proc/1/environ" else default
        ),
    )

    assert gpusetup.foreign_kernel_reason() is None


def test_a_container_stops_a_load_and_live_media_does_not(monkeypatch):
    """Two different questions. Live media and a read-only root stop an *install* being worth doing;
    neither stops a module already on the disk from being loaded, and on live media a modprobe is
    the only thing that could possibly help, since nothing installed there survives."""
    monkeypatch.setattr(
        gpusetup.procutil, "read_file",
        lambda path, default=None: "BOOT_IMAGE=/vmlinuz root=live:/dev/sr0 rd.live.image",
    )
    monkeypatch.setattr(gpusetup.os.path, "isdir", lambda path: False)
    monkeypatch.setattr(gpusetup.os.path, "exists", lambda path: False)

    assert "live media" in (gpusetup.live_media_reason() or "")
    assert gpusetup.foreign_kernel_reason() is None

    monkeypatch.setattr(gpusetup.os.path, "exists", lambda path: path == "/run/.containerenv")

    assert gpusetup.foreign_kernel_reason() is not None


# --- what the report says about it ----------------------------------------------
#
# A report that cannot tell a card whose driver came up at boot from one this suite loaded ten
# seconds before the test is a report that cannot be reviewed properly, and with attestation coupled
# to the verdict that difference can become a validation level. Facts, not a verdict: whether a
# hot-loaded pass may certify is lumina's to decide, and keeping the decision there is what lets it
# be revised without shipping a new collector to every certifying partner.


def test_a_machine_this_never_looked_at_records_nothing():
    """Absent rather than empty, so nothing changes in the reports of the many machines with no
    NVIDIA card in them."""
    assert gpusetup.load_record() is None


def test_loading_the_driver_is_recorded_as_ours(machine):
    gpusetup.try_load(log=lambda msg: None)
    record = gpusetup.load_record()

    assert record["loaded_by_alma_cert"] is True
    assert record["present_before_run"] is False
    assert [a["argv"] for a in record["attempts"]] == [["modprobe", "nvidia"],
                                                      ["modprobe", "nvidia_uvm"]]
    assert record["modules_after"] == ["nvidia", "nvidia_uvm"]
    assert record["running_kernel"]


def test_a_driver_that_was_already_up_is_not_claimed_as_ours(monkeypatch):
    """The ordinary case, and the one the flag exists to be false for: the machine booted with the
    driver loaded and this suite only looked."""
    _machine(monkeypatch, preloaded=["nvidia", "nvidia_uvm"])
    gpusetup.note_starting_state()
    record = gpusetup.load_record()

    assert record["loaded_by_alma_cert"] is False
    assert record["present_before_run"] is True


def test_the_baseline_is_taken_before_anything_could_load_it(monkeypatch):
    """``note_starting_state`` is called before the offer runs anything, and the first call wins, so
    a later one cannot overwrite the baseline with the state this suite created."""
    box = _machine(monkeypatch)
    gpusetup.note_starting_state()
    gpusetup.try_load(log=lambda msg: None)
    gpusetup.note_starting_state()  # as a second command path would

    assert gpusetup.load_record()["modules_before"] == []
    assert box.modprobed


def test_what_each_command_said_is_kept_verbatim(machine):
    """A message this collector does not recognize is exactly the one a reviewer needs, and
    summarizing it here would be the collector deciding."""
    machine.answers["nvidia"] = Res(ok=False, stderr=UNSIGNED, returncode=1)
    machine.answers["smi"] = Res(ok=False, stderr=SMI_NO_DRIVER)

    gpusetup.try_load(log=lambda msg: None)
    record = gpusetup.load_record()

    assert record["attempts"][0]["stderr"] == UNSIGNED
    assert record["attempts"][0]["returncode"] == 1
    assert "couldn't communicate" in record["nvidia_smi"]


def test_a_timeout_is_recorded_as_one(machine):
    machine.answers["nvidia"] = Res(ok=False, timed_out=True)
    machine.answers["smi"] = Res(ok=False, stderr=SMI_NO_DRIVER)

    gpusetup.try_load(log=lambda msg: None)

    assert gpusetup.load_record()["attempts"][0]["timed_out"] is True


def test_a_refused_attempt_is_still_not_claimed_as_a_load(monkeypatch):
    """Nothing ran, so nothing was loaded by us, and the record must not imply otherwise."""
    _machine(monkeypatch, preloaded=["nouveau"], cards=[("0000:01:00.0", "nouveau")])

    gpusetup.try_load(log=lambda msg: None)

    assert gpusetup.load_record()["loaded_by_alma_cert"] is False


def test_installing_the_driver_during_the_run_is_recorded():
    """``environment.installed_packages`` cannot show this: that list is ``pkg.newly_installed``,
    which tracks only what went through ``alma_certify.pkg``, and this is a dnf the plan runs
    directly. So the report did not say the driver had been installed during the run at all."""
    steps = gpusetup.install_plan(gpusetup.STATE_DRIVER_MISSING, major="10")
    gpusetup.note_install(steps, None)
    record = gpusetup.load_record()

    assert any("nvidia-open" in cmd for cmd in record["installed_during_run"])
    assert "install_failed_at" not in record


def test_an_install_that_failed_part_way_says_where():
    steps = gpusetup.install_plan(gpusetup.STATE_DRIVER_MISSING, major="10")
    gpusetup.note_install(steps, steps[-1])

    assert "nvidia-open" in gpusetup.load_record()["install_failed_at"]


def test_the_record_reaches_the_report(machine):
    """The whole point. The offer runs before ``_start_run``, so there is no RunState to hang this
    on when it happens, and ``capture_environment`` at finalize is where it has to arrive."""
    from alma_certify import report

    gpusetup.try_load(log=lambda msg: None)
    environment = report.capture_environment([])

    assert environment["nvidia_driver"]["loaded_by_alma_cert"] is True


def test_the_report_says_nothing_about_machines_with_no_card():
    from alma_certify import report

    assert "nvidia_driver" not in report.capture_environment([])


def test_the_driver_holding_the_card_is_recorded(monkeypatch):
    """``loaded_nvidia_modules`` cannot show this, because it matches on the vendor module's name.
    nouveau holding the card is the usual reason a GPU run certifies nothing after a successful
    install, and without it the report had an empty before, an empty after, and no hint of why: the
    operator's terminal said so and stderr does not reach the log."""
    _machine(monkeypatch, preloaded=["nouveau", "xfs"])

    gpusetup.note_starting_state()

    assert gpusetup.load_record()["conflicting_modules_before"] == ["nouveau"]


def test_an_ordinary_machine_records_no_conflict(machine):
    gpusetup.note_starting_state()

    assert "conflicting_modules_before" not in gpusetup.load_record()


# --- picking a driver this kernel can actually load -----------------------------
#
# Measured against the AlmaLinux Kitten repository: of five kernels in BaseOS, two had no matching
# kmod-nvidia-open build at all and the other three each wanted a different one, while plain
# ``dnf install nvidia-open`` took the newest every time. A driver whose kABI does not match is
# installed into some other kernel's module directory, which is the "Module nvidia not found in
# directory /lib/modules/<running>" that started this.

KERNEL_PROVIDES = """\
kernel(alpha) = 0x1111
kernel(beta) = 0x2222
kernel-core = 6.12.0-250.el10
"""
MODULES_CORE_PROVIDES = "kernel(gamma) = 0x3333\n"
BUILDS = "kmod-nvidia-open-610.57.04-1.el10.x86_64\nkmod-nvidia-open-615.71.09-1.el10.x86_64\n"
# The older build matches; the newest wants a symbol this kernel does not have.
REQUIRES = {
    "kmod-nvidia-open-610.57.04-1.el10.x86_64":
        "kernel(alpha) = 0x1111\nkernel(gamma) = 0x3333\n/usr/sbin/weak-modules\n",
    "kmod-nvidia-open-615.71.09-1.el10.x86_64":
        "kernel(alpha) = 0x1111\nkernel(delta) = 0x4444\n",
}


def _repo(monkeypatch, *, builds=BUILDS, requires=None, modules_core=MODULES_CORE_PROVIDES):
    requires = REQUIRES if requires is None else requires
    calls = []

    def run_cmd(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "rpm":
            target = argv[-1]
            if target.startswith("kernel-core-"):
                return Res(ok=True, stdout=KERNEL_PROVIDES)
            if target.startswith("kernel-modules-core-"):
                return Res(ok=True, stdout=modules_core)
            return Res(ok=False, stdout="package %s is not installed" % target)
        if argv[:3] == ["dnf", "repoquery", "--quiet"]:
            if "--showduplicates" in argv:
                return Res(ok=True, stdout=builds)
            return Res(ok=True, stdout=requires.get(argv[-1], ""))
        return Res(ok=True)

    monkeypatch.setattr(gpusetup.procutil, "run_cmd", run_cmd)
    monkeypatch.setattr(gpusetup.platform, "release", lambda: "6.12.0-250.el10.x86_64")
    return calls


def test_the_kernels_symbols_come_from_every_kernel_subpackage(monkeypatch):
    """kernel-core alone is not the kernel: a few dozen of the symbols a driver needs come from
    kernel-modules-core, and without them a build that fits reads as a near miss."""
    _repo(monkeypatch)

    have = gpusetup.running_kernel_ksyms()

    assert have == {"kernel(alpha)": "0x1111", "kernel(beta)": "0x2222", "kernel(gamma)": "0x3333"}


def test_the_newest_build_that_fits_wins_not_the_newest_build(monkeypatch):
    _repo(monkeypatch)

    evr, reason = gpusetup.loadable_driver()

    assert (evr, reason) == ("610.57.04-1.el10", None)


def test_the_newest_of_several_that_fit_is_taken(monkeypatch):
    """Kernel 250 in the Kitten repository has two: 610.43.02-7 and 610.57.04-1. Taking the older
    would work and would ship a machine an older driver than it can run."""
    _repo(monkeypatch,
          builds="kmod-nvidia-open-610.43.02-7.el10.x86_64\n"
                 "kmod-nvidia-open-610.57.04-1.el10.x86_64\n",
          requires={
              "kmod-nvidia-open-610.43.02-7.el10.x86_64": "kernel(alpha) = 0x1111\n",
              "kmod-nvidia-open-610.57.04-1.el10.x86_64": "kernel(beta) = 0x2222\n",
          })

    assert gpusetup.loadable_driver()[0] == "610.57.04-1.el10"


def test_a_kernel_nothing_matches_says_so_and_names_the_closest(monkeypatch):
    """Two of five Kitten kernels are in this state, and no install command changes it."""
    _repo(monkeypatch, requires={
        "kmod-nvidia-open-610.57.04-1.el10.x86_64": "kernel(alpha) = 0xdead\n",
        "kmod-nvidia-open-615.71.09-1.el10.x86_64": "kernel(alpha) = 0xbeef\nkernel(x) = 0x1\n",
    })

    evr, reason = gpusetup.loadable_driver()

    assert evr is None
    assert "no available kmod-nvidia-open matches the running kernel 6.12.0-250" in reason
    assert "610.57.04-1.el10" in reason, "the closest is the useful one to name"
    assert "1 kABI symbols" in reason


def test_an_empty_repository_is_reported_rather_than_guessed_at(monkeypatch):
    _repo(monkeypatch, builds="")

    evr, reason = gpusetup.loadable_driver()

    assert evr is None and "no kmod-nvidia-open builds are available" in reason


def test_a_kernel_with_no_kabi_symbols_is_reported_too(monkeypatch):
    _repo(monkeypatch, modules_core="")
    monkeypatch.setattr(gpusetup, "running_kernel_ksyms", lambda release=None: {})

    evr, reason = gpusetup.loadable_driver()

    assert evr is None and "publishes no kABI symbols" in reason


def test_the_install_step_pins_both_halves(monkeypatch):
    """``nvidia-open`` requires its own version of the module, so pinning one and not the other lets
    dnf satisfy the pair by taking the newest of both."""
    _repo(monkeypatch)

    argv = gpusetup._pin_driver_install()

    assert argv == ["dnf", "-y", "install", "kmod-nvidia-open-610.57.04-1.el10",
                    "nvidia-open-610.57.04", "cuda-toolkit"]


def test_the_metapackage_is_pinned_by_version_not_by_the_modules_release(monkeypatch):
    """Their releases are different things. The module is rebuilt per kernel minor, so it carries
    releases like ``1.el10_2``, while ``nvidia-open`` is ``1.el10`` at every version in both
    repositories. Reported from CI: "No match for argument: nvidia-open-615.71.09-1.el10_2"."""
    _repo(monkeypatch,
          builds="kmod-nvidia-open-615.71.09-1.el10_2.x86_64\n",
          requires={"kmod-nvidia-open-615.71.09-1.el10_2.x86_64": "kernel(alpha) = 0x1111\n"})

    argv = gpusetup._pin_driver_install()

    assert argv == ["dnf", "-y", "install",
                    "kmod-nvidia-open-615.71.09-1.el10_2",  # the module keeps its own release
                    "nvidia-open-615.71.09", "cuda-toolkit"]


def test_the_pin_is_recorded_for_the_report(monkeypatch):
    _repo(monkeypatch)
    gpusetup._record.clear()

    gpusetup._pin_driver_install()

    assert gpusetup.load_record()["kabi_match"] == "610.57.04-1.el10"


def test_no_match_is_recorded_too_and_leaves_the_plan_alone(monkeypatch):
    """The install still runs. It will not produce a loadable driver, and the record is what says
    why, rather than leaving a modprobe failure to be puzzled over."""
    _repo(monkeypatch, requires={
        "kmod-nvidia-open-610.57.04-1.el10.x86_64": "kernel(alpha) = 0xdead\n",
        "kmod-nvidia-open-615.71.09-1.el10.x86_64": "kernel(alpha) = 0xbeef\n",
    })
    gpusetup._record.clear()

    assert gpusetup._pin_driver_install() is None
    assert "no available kmod-nvidia-open matches" in gpusetup.load_record()["kabi_no_match"]


def test_the_plan_resolves_the_step_just_before_running_it(monkeypatch):
    """The repository the pin is queried from is added by an earlier step of this same plan, so the
    version cannot be known when the plan is printed for consent."""
    _repo(monkeypatch)
    ran = []
    monkeypatch.setattr(gpusetup.procutil, "run_cmd",
                        lambda argv, **kw: ran.append(argv) or Res(ok=True))
    steps = [gpusetup.Step("install it", ["dnf", "-y", "install", "nvidia-open"],
                           resolve=lambda: ["dnf", "-y", "install", "nvidia-open-1.2-3"])]

    assert gpusetup.run_plan(steps, log=lambda m: None) is None
    assert ran == [["dnf", "-y", "install", "nvidia-open-1.2-3"]]


def test_a_step_with_no_resolver_runs_as_printed(monkeypatch):
    ran = []
    monkeypatch.setattr(gpusetup.procutil, "run_cmd",
                        lambda argv, **kw: ran.append(argv) or Res(ok=True))

    gpusetup.run_plan([gpusetup.Step("go", ["dnf", "-y", "install", "epel-release"])],
                      log=lambda m: None)

    assert ran == [["dnf", "-y", "install", "epel-release"]]


def test_only_the_almalinux_route_pins(monkeypatch):
    """On 8 and on rebuilds the module is DKMS, built against the running kernel, so there is no
    kABI to match and nothing to pin."""
    native = gpusetup.install_plan(gpusetup.STATE_DRIVER_MISSING, major="10")
    nvidia = gpusetup.install_plan(gpusetup.STATE_DRIVER_MISSING, major="8", machine="x86_64",
                                   kernel_release="4.18.0-553.el8.x86_64")

    assert native[-1].resolve is not None
    assert nvidia[-1].resolve is None
