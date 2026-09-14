"""KVM staging, and specifically who is allowed to say "disabled by BIOS".

The regression these cover: the test used to search the entire boot log for the
substring "disabled by bios". AlmaLinux 8 logs `x86/cpu: SGX disabled by BIOS`
on hardware where SGX is simply switched off, which has nothing to do with KVM,
so every el8 run failed virtualization on a machine whose el9 and el10 runs
passed. Only a line the kvm subsystem itself printed counts now, and it is only
consulted when the module actually refused to load.
"""

import pytest

from alma_certify import procutil
from alma_certify.config import Config
from alma_certify.registry import RunContext
from alma_certify.validate import virt as virt_mod
from alma_certify.validate.virt import KvmFunctional

# Real el8 output from a machine with VT-x on and SGX off.
DMESG_SGX_OFF = """\
[    0.000000] Linux version 4.18.0-553.el8_10.x86_64
[    0.041000] x86/cpu: SGX disabled by BIOS.
[    0.512000] ACPI FADT declares the system doesn't support PCIe ASPM
[    2.884000] kvm: Nested Virtualization enabled
"""

# What the kernel prints when virtualization really is locked off.
DMESG_KVM_OFF = """\
[    0.000000] Linux version 4.18.0-553.el8_10.x86_64
[    0.041000] x86/cpu: SGX disabled by BIOS.
[    3.104000] kvm: disabled by bios
"""

DMESG_KVM_AMD_OFF = """\
[    3.104000] kvm_amd: SVM disabled by BIOS in SYSCFG.svme
"""


class FakeCmds:
    def __init__(self, *, modprobe_ok=True, dmesg=DMESG_SGX_OFF):
        self.modprobe_ok = modprobe_ok
        self.dmesg = dmesg
        self.calls = []

    def cmd(self, argv, timeout=None, artifact=None, check=False):
        self.calls.append(list(argv))
        if argv[0] == "modprobe":
            return procutil.CmdResult(
                argv=list(argv),
                returncode=0 if self.modprobe_ok else 1,
                stdout="",
                stderr="" if self.modprobe_ok
                else "modprobe: ERROR: could not insert 'kvm_intel': "
                     "Operation not supported",
            )
        if argv[0] == "dmesg":
            return procutil.CmdResult(argv=list(argv), returncode=0,
                                      stdout=self.dmesg, stderr="")
        raise AssertionError("unexpected call: %r" % (argv,))


def make_ctx(tmp_path, fake):
    ctx = RunContext(Config.load("/nonexistent"), str(tmp_path), inventory={"summary": {}})
    ctx.log = lambda msg: None
    ctx.cmd = fake.cmd
    return ctx


@pytest.fixture
def intel(monkeypatch):
    monkeypatch.setattr(virt_mod.hwquery, "cpu_virt_flag", lambda: "vmx")


@pytest.fixture
def no_kvm_device(monkeypatch):
    """Stop the test after stage 1 without booting anything."""
    monkeypatch.setattr(virt_mod.os.path, "exists", lambda path: False)


# --- the regression -----------------------------------------------------------

def test_sgx_disabled_by_bios_is_not_a_kvm_verdict(tmp_path, intel, no_kvm_device):
    fake = FakeCmds(modprobe_ok=True, dmesg=DMESG_SGX_OFF)
    result = KvmFunctional().run(make_ctx(tmp_path, fake))
    # It stops on the missing device, which is the next real check - crucially
    # it is not blaming firmware for a line about a different feature.
    assert "BIOS" not in result.reason
    assert result.reason == "/dev/kvm does not exist"


def test_loaded_module_is_never_overruled_by_the_log(tmp_path, intel, no_kvm_device):
    """A module the kernel accepted is direct evidence; the log is not."""
    fake = FakeCmds(modprobe_ok=True, dmesg=DMESG_KVM_OFF)
    result = KvmFunctional().run(make_ctx(tmp_path, fake))
    assert "BIOS" not in result.reason
    # And with the module up, dmesg is not even consulted.
    assert not any(call[0] == "dmesg" for call in fake.calls)


# --- firmware detection still works where it belongs --------------------------

def test_failed_modprobe_with_kvm_line_blames_firmware(tmp_path, intel):
    fake = FakeCmds(modprobe_ok=False, dmesg=DMESG_KVM_OFF)
    result = KvmFunctional().run(make_ctx(tmp_path, fake))
    assert result.status == "fail"
    assert result.reason == "virtualization is disabled in BIOS/firmware"


def test_failed_modprobe_recognizes_the_amd_wording(tmp_path, monkeypatch):
    monkeypatch.setattr(virt_mod.hwquery, "cpu_virt_flag", lambda: "svm")
    fake = FakeCmds(modprobe_ok=False, dmesg=DMESG_KVM_AMD_OFF)
    result = KvmFunctional().run(make_ctx(tmp_path, fake))
    assert result.reason == "virtualization is disabled in BIOS/firmware"
    assert ["modprobe", "kvm_amd"] in fake.calls


def test_failed_modprobe_without_a_kvm_line_reports_the_module(tmp_path, intel):
    """Some other reason the module would not load - say what happened."""
    fake = FakeCmds(modprobe_ok=False, dmesg=DMESG_SGX_OFF)
    result = KvmFunctional().run(make_ctx(tmp_path, fake))
    assert result.status == "fail"
    assert result.reason == "could not load kvm_intel"
    assert "Operation not supported" in result.details["modprobe_error"]


# --- the pattern itself -------------------------------------------------------

@pytest.mark.parametrize("line", [
    "[    3.104000] kvm: disabled by bios",
    "[    3.104000] kvm: disabled by BIOS",
    "kvm: support for 'kvm_intel' disabled by bios",
    "[    3.104000] kvm_amd: SVM disabled by BIOS in SYSCFG.svme",
    "[    3.104000] kvm_intel: VMX disabled by BIOS",
])
def test_pattern_matches_kvm_lines(line):
    assert virt_mod._KVM_DISABLED_RE.search(line)


@pytest.mark.parametrize("line", [
    "[    0.041000] x86/cpu: SGX disabled by BIOS.",
    "[    0.041000] sgx: SGX disabled by BIOS",
    "[    0.512000] DMAR: IOMMU disabled by BIOS",
    "[    0.512000] AMD-Vi: IOMMU disabled by BIOS",
    "[    0.041000] tpm_tis: TPM disabled by BIOS",
    "[    1.200000] thunderbolt: disabled by BIOS",
])
def test_pattern_ignores_other_subsystems(line):
    assert not virt_mod._KVM_DISABLED_RE.search(line)
