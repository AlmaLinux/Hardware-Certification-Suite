"""Kernel taint: fail on hardware faults, not on distribution packaging.

The regression these pin down: an unmodified AlmaLinux live-media kernel
failed certification. Live media routinely sets the firmware-workaround bit
and AlmaLinux's own "unsupported module" bit, neither of which says anything
about the hardware under test.
"""

import pytest

from alma_certify.validate.kernel import TAINT_BITS, TaintCheck, describe_taint


def bits(*positions):
    value = 0
    for p in positions:
        value |= 1 << p
    return value


class FakeCtx:
    """Stands in for the run context; dmesg supplies the kernel's own words."""

    def __init__(self, dmesg=""):
        self._dmesg = dmesg

    def cmd(self, argv, timeout=None, artifact=None, check=False):
        from alma_certify.procutil import CmdResult

        return CmdResult(argv, 0, self._dmesg, "")


def run_with(monkeypatch, value, dmesg=""):
    monkeypatch.setattr(
        "alma_certify.validate.kernel.procutil.read_file",
        lambda path, default=None: str(value),
    )
    return TaintCheck().run(FakeCtx(dmesg))


# --- the live-media regression ------------------------------------------------


def test_firmware_workaround_alone_does_not_fail(monkeypatch):
    """Bit 11: the kernel worked around a firmware bug, so the system works.
    Set on a great deal of shipping hardware."""
    result = run_with(monkeypatch, bits(11))
    assert result.status == "pass"
    assert "firmware workaround" in result.reason
    assert result.details["letters"] == "I"


def test_distro_auxiliary_taint_does_not_fail(monkeypatch):
    """Bit 16: AlmaLinux and RHEL use it for unsupported/Tech Preview
    modules. It was not even in the old table, so it printed "bit 16"."""
    result = run_with(monkeypatch, bits(16))
    assert result.status == "pass"
    assert "unsupported module" in result.reason
    assert result.details["letters"] == "X"


def test_typical_live_media_combination_passes(monkeypatch):
    """Firmware workaround + staging driver + unsupported module."""
    result = run_with(monkeypatch, bits(10, 11, 16))
    assert result.status == "pass"
    assert result.details["letters"] == "C I X"


def test_gpu_driver_taints_still_pass(monkeypatch):
    """Proprietary + out-of-tree + unsigned: an NVIDIA driver."""
    result = run_with(monkeypatch, bits(0, 12, 13))
    assert result.status == "pass"
    assert result.details["letters"] == "P O E"


def test_kernel_warning_alone_does_not_fail(monkeypatch):
    """validate.kernel.dmesg judges warnings with an allowlist; a bare taint
    bit cannot tell a benign driver grumble from a real problem."""
    assert run_with(monkeypatch, bits(9)).status == "pass"


# --- genuine hardware faults still fail ---------------------------------------


@pytest.mark.parametrize(
    "bit,fragment",
    [
        (4, "machine check"),
        (5, "bad page"),
        (7, "kernel died"),
        (14, "soft lockup"),
    ],
)
def test_hardware_faults_fail(monkeypatch, bit, fragment):
    result = run_with(monkeypatch, bits(bit))
    assert result.status == "fail"
    assert fragment in result.reason


def test_a_hardware_fault_fails_even_amid_benign_flags(monkeypatch):
    result = run_with(monkeypatch, bits(0, 11, 16, 4))
    assert result.status == "fail"
    assert "machine check" in result.reason
    # the benign flags are still recorded for context
    assert len(result.details["flags"]) == 4
    assert len(result.details["faults"]) == 1


# --- reporting ----------------------------------------------------------------


def test_untainted_kernel_passes_quietly(monkeypatch):
    result = run_with(monkeypatch, 0)
    assert result.status == "pass"
    assert result.reason is None
    assert result.details["letters"] == ""


def test_failure_reason_is_self_diagnosing(monkeypatch):
    """The raw value and letter codes go in the message, so the failure can
    be matched against the kernel's own documentation without a second run."""
    value = bits(4, 11)
    result = run_with(monkeypatch, value)
    assert "tainted=%d" % value in result.reason
    assert "I" in result.details["letters"]


def test_unreadable_taint_value_is_an_error(monkeypatch):
    monkeypatch.setattr(
        "alma_certify.validate.kernel.procutil.read_file",
        lambda path, default=None: "not a number",
    )
    assert TaintCheck().run(FakeCtx()).status == "error"


# --- bit 2: reported for review, not a failure --------------------------------


def test_out_of_spec_bit_does_not_fail(monkeypatch):
    """tainted=4 on unmodified AlmaLinux live media. RHEL-family kernels use
    this bit to flag hardware the vendor does not support, which is a policy
    statement rather than a malfunction."""
    result = run_with(monkeypatch, 4)
    assert result.status == "pass"
    assert result.details["letters"] == "S"
    assert result.details["faults"] == []
    assert result.details["notable"]


def test_out_of_spec_is_surfaced_not_buried(monkeypatch):
    result = run_with(monkeypatch, 4)
    assert "worth a look" in result.reason
    assert "tainted=4" in result.reason


def test_kernel_explanation_is_captured(monkeypatch):
    """The bit alone explains nothing, so the kernel's own line comes too."""
    dmesg = (
        "[    0.000000] Linux version 5.14.0-503.el9.x86_64\n"
        "[    0.210000] Intel Skylake Xeon is not supported by this kernel\n"
        "[    1.500000] usb 1-1: new high-speed USB device\n"
    )
    result = run_with(monkeypatch, 4, dmesg=dmesg)
    assert any("not supported by" in line for line in result.details["kernel_log"])
    assert "kernel said:" in result.reason


def test_no_kernel_log_is_gathered_for_a_clean_kernel(monkeypatch):
    result = run_with(monkeypatch, 0)
    assert "kernel_log" not in result.details


def test_a_real_fault_still_wins_over_a_notable_bit(monkeypatch):
    result = run_with(monkeypatch, bits(2, 4))
    assert result.status == "fail"
    assert "machine check" in result.reason
    assert result.details["notable"]      # still recorded alongside


def test_undocumented_bit_is_reported_not_gated(monkeypatch):
    """A future kernel bit should not fail a machine on a guess."""
    result = run_with(monkeypatch, bits(40))
    assert result.status == "pass"
    assert "undocumented taint bit 40" in result.details["flags"][0]


def test_every_documented_bit_is_well_formed():
    for entry in TAINT_BITS.values():
        letter, description, weight = entry
        assert len(letter) == 1
        assert description
        assert weight in {"fault", "notable", "routine"}


def test_describe_taint_is_pure():
    assert describe_taint(0)["flags"] == []
    assert describe_taint(bits(11))["faults"] == []
    assert describe_taint(bits(4))["faults"]
    assert describe_taint(bits(2))["notable"]
