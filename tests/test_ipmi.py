"""IPMI/BMC checks against captured ipmitool output.

Fixtures are the real output shapes: `mc info` and `chassis status` as
key/value blocks, `sdr elist` as five pipe-separated columns, `sel list` as
event rows. Every verdict is reachable without a BMC.
"""

import pytest

from alma_certify import procutil
from alma_certify.config import Config
from alma_certify.registry import RunContext
from alma_certify.validate import ipmi as ipmi_mod
from alma_certify.validate.ipmi import BmcReachable, IpmiEventLog, IpmiHealth

MC_INFO = """\
Device ID                 : 32
Device Revision           : 1
Firmware Revision         : 2.75
IPMI Version              : 2.0
Manufacturer ID           : 674
Manufacturer Name         : Dell Inc.
Product ID                : 256 (0x0100)
Device Available          : yes
Additional Device Support :
    Sensor Device
    SDR Repository Device
"""

CHASSIS_OK = """\
System Power         : on
Power Overload       : false
Main Power Fault     : false
Power Control Fault  : false
Chassis Intrusion    : active
Drive Fault          : false
Cooling/Fan Fault    : false
"""

CHASSIS_FAN_FAULT = CHASSIS_OK.replace("Cooling/Fan Fault    : false",
                                       "Cooling/Fan Fault    : true")

SDR_HEALTHY = """\
Inlet Temp       | 04h | ok  |  7.1 | 21 degrees C
CPU1 Temp        | 01h | ok  |  3.1 | 45 degrees C
FAN1             | 30h | ok  |  7.1 | 4200 RPM
FAN5             | 34h | ns  |  7.1 | No Reading
PS Redundancy    | 77h | ok  | 21.1 | Fully Redundant
"""

SDR_CRITICAL = SDR_HEALTHY + "FAN2             | 31h | cr  |  7.1 | 300 RPM\n"
SDR_WARNING = SDR_HEALTHY + "Exhaust Temp     | 05h | nc  |  7.1 | 72 degrees C\n"

SEL_QUIET = """\
   1 | 04/12/2026 | 09:15:22 | Power Unit #0x01 | Power off/down | Asserted
   2 | 04/12/2026 | 09:16:02 | System Boot Initiated | Initiated by power up
"""

SEL_SERIOUS = SEL_QUIET + (
    "   3 | 05/01/2026 | 22:04:11 | Memory #0x53 | "
    "Uncorrectable ECC | Asserted\n"
)


class FakeIpmitool:
    """Answers the ipmitool subcommands these tests drive."""

    def __init__(self, *, mc_info=MC_INFO, mc_ok=True, chassis=CHASSIS_OK,
                 sdr=SDR_HEALTHY, sdr_ok=True, sel_list=SEL_QUIET,
                 sel_ok=True, mc_stderr=""):
        self.mc_info, self.mc_ok, self.mc_stderr = mc_info, mc_ok, mc_stderr
        self.chassis = chassis
        self.sdr, self.sdr_ok = sdr, sdr_ok
        self.sel_list, self.sel_ok = sel_list, sel_ok
        self.calls = []

    def cmd(self, argv, timeout=None, artifact=None, check=False):
        self.calls.append(list(argv))

        def done(stdout, rc=0, stderr=""):
            return procutil.CmdResult(argv=list(argv), returncode=rc,
                                      stdout=stdout, stderr=stderr)

        if argv[1:3] == ["mc", "info"]:
            return done(self.mc_info if self.mc_ok else "",
                        0 if self.mc_ok else 1, self.mc_stderr)
        if argv[1:3] == ["chassis", "status"]:
            return done(self.chassis, 0 if self.chassis else 1)
        if argv[1:3] == ["sdr", "elist"]:
            return done(self.sdr if self.sdr_ok else "",
                        0 if self.sdr_ok else 1)
        if argv[1:3] == ["sel", "info"]:
            return done("Entries          : 2\nPercent Used     : 1%\n",
                        0 if self.sel_ok else 1)
        if argv[1:3] == ["sel", "list"]:
            return done(self.sel_list if self.sel_ok else "",
                        0 if self.sel_ok else 1)
        raise AssertionError("unexpected ipmitool call: %r" % (argv,))


@pytest.fixture
def has_device(monkeypatch):
    """Pretend the in-band char device exists."""
    monkeypatch.setattr(ipmi_mod, "_ipmi_devices", lambda: ["/dev/ipmi0"])


@pytest.fixture
def no_device(monkeypatch):
    """A machine with no BMC, as far as the kernel is concerned.

    Both probes, not just the device nodes. ``_bmc_declared`` also consults /sys/class/ipmi, and
    with only the first stubbed these tests described a machine without a BMC while still reading
    the builder's own. That is exactly what happened: the package's %check failed on an AlmaLinux
    10 builder whose /sys/class/ipmi was populated, and passed everywhere without one.
    """
    monkeypatch.setattr(ipmi_mod, "_ipmi_devices", lambda: [])
    monkeypatch.setattr(ipmi_mod, "_ipmi_sysfs", lambda: [])


def make_ctx(tmp_path, fake=None, bmc=None):
    ctx = RunContext(Config.load("/nonexistent"), str(tmp_path),
                     inventory={"summary": {"bmc": bmc if bmc is not None
                                            else {"present": True}}})
    ctx.log = lambda msg: None
    if fake is not None:
        ctx.cmd = fake.cmd
    return ctx


def make_test(cls, **attrs):
    test = cls()
    test._modprobe = {}
    for key, value in attrs.items():
        setattr(test, key, value)
    return test


# --- applicability ------------------------------------------------------------


def test_skips_where_no_bmc_exists(tmp_path, no_device):
    """A desktop or laptop: SMBIOS type 38 absent, nothing probed."""
    ctx = make_ctx(tmp_path, bmc={"present": False})
    reason = make_test(BmcReachable).applicable(ctx)
    assert reason is not None
    assert "no IPMI device" in reason


def test_applicable_when_smbios_declares_a_bmc(tmp_path, no_device):
    ctx = make_ctx(tmp_path, bmc={"present": True, "interface": "KCS"})
    assert make_test(BmcReachable).applicable(ctx) is None


def test_applicable_when_the_kernel_found_a_bmc_smbios_did_not_declare(
        tmp_path, has_device):
    ctx = make_ctx(tmp_path, bmc={"present": False})
    assert make_test(BmcReachable).applicable(ctx) is None


# --- reachability -------------------------------------------------------------


def test_bmc_that_answers_passes_and_records_its_identity(tmp_path, has_device):
    fake = FakeIpmitool()
    result = make_test(BmcReachable).run(make_ctx(tmp_path, fake))

    assert result.status == "pass"
    assert "Dell Inc." in result.reason
    assert "2.75" in result.reason
    assert result.details["mc_info"]["Manufacturer Name"] == "Dell Inc."
    assert result.details["chassis"]["System Power"] == "on"


def test_interface_present_but_silent_bmc_fails(tmp_path, has_device):
    """The one hardware-attributable failure: the host interface is there and
    the controller behind it does not answer."""
    fake = FakeIpmitool(mc_ok=False, mc_stderr="Unable to establish IPMI v2 session")
    result = make_test(BmcReachable).run(make_ctx(tmp_path, fake))

    assert result.status == "fail"
    assert "did not answer" in result.reason
    assert "Unable to establish" in result.reason


def test_bmc_reporting_itself_unavailable_fails(tmp_path, has_device):
    fake = FakeIpmitool(mc_info=MC_INFO.replace("Device Available          : yes",
                                               "Device Available          : no"))
    result = make_test(BmcReachable).run(make_ctx(tmp_path, fake))
    assert result.status == "fail"
    assert "unavailable" in result.reason


def test_interface_that_never_came_up_skips_with_the_modprobe_error(
        tmp_path, no_device):
    """SMBIOS type 38 is occasionally stale and a BMC can be disabled in
    firmware, so this is not a defect. The cause goes in the reason."""
    test = make_test(BmcReachable,
                     _modprobe={"ipmi_si": "no such device", "ipmi_devintf": "loaded"})
    result = test.run(make_ctx(tmp_path))

    assert result.status == "skip"
    assert "no in-band IPMI device appeared" in result.reason
    assert "ipmi_si: no such device" in result.reason
    assert "ipmi_devintf" not in result.reason      # loaded modules are not noise


# --- chassis health -----------------------------------------------------------


def test_healthy_chassis_passes(tmp_path, has_device):
    fake = FakeIpmitool()
    result = make_test(IpmiHealth).run(make_ctx(tmp_path, fake))

    assert result.status == "pass"
    assert "none critical" in result.reason
    assert result.details["sensor_count"] == 5
    assert result.details["sensor_states"]["ok"] == 4
    assert result.details["sensor_states"]["no reading"] == 1


def test_critical_sensor_fails(tmp_path, has_device):
    fake = FakeIpmitool(sdr=SDR_CRITICAL)
    result = make_test(IpmiHealth).run(make_ctx(tmp_path, fake))

    assert result.status == "fail"
    assert "FAN2" in result.reason
    assert "300 RPM" in result.reason


def test_non_critical_sensor_is_surfaced_without_gating(tmp_path, has_device):
    """A threshold warning is worth a reviewer's attention, not a failure."""
    fake = FakeIpmitool(sdr=SDR_WARNING)
    result = make_test(IpmiHealth).run(make_ctx(tmp_path, fake))

    assert result.status == "pass"
    assert "worth a look" in result.reason
    assert "Exhaust Temp" in result.reason


def test_chassis_fault_flag_fails(tmp_path, has_device):
    fake = FakeIpmitool(chassis=CHASSIS_FAN_FAULT)
    result = make_test(IpmiHealth).run(make_ctx(tmp_path, fake))

    assert result.status == "fail"
    assert "Cooling/Fan Fault" in result.reason


def test_chassis_intrusion_alone_does_not_fail(tmp_path, has_device):
    """Asserted on any machine whose case has ever been opened, which is most
    test hardware."""
    fake = FakeIpmitool()
    assert "Chassis Intrusion    : active" in fake.chassis
    assert make_test(IpmiHealth).run(make_ctx(tmp_path, fake)).status == "pass"


def test_unpopulated_fan_headers_do_not_gate(tmp_path, has_device):
    fake = FakeIpmitool()
    result = make_test(IpmiHealth).run(make_ctx(tmp_path, fake))
    states = {s["name"]: s["state"] for s in IpmiHealth._parse_sdr(fake.sdr)}
    assert states["FAN5"] == "no reading"
    assert result.status == "pass"


def test_sdr_list_three_column_form_is_also_parsed():
    """Older ipmitool and `sdr list` put the state in the third column."""
    sensors = IpmiHealth._parse_sdr("Inlet Temp | 21 degrees C | ok\n")
    assert sensors == [{"name": "Inlet Temp", "reading": "21 degrees C",
                       "state": "ok"}]


# --- event log ----------------------------------------------------------------


def test_quiet_event_log_passes(tmp_path, has_device):
    fake = FakeIpmitool()
    result = make_test(IpmiEventLog).run(make_ctx(tmp_path, fake))

    assert result.status == "pass"
    assert result.details["entry_count"] == 2
    assert result.details["serious_entries"] == []


def test_serious_event_is_reported_but_never_gates(tmp_path, has_device):
    """The SEL is historical: it survives OS installs and often predates the
    current owner, so it is reported, never failed."""
    fake = FakeIpmitool(sel_list=SEL_SERIOUS)
    result = make_test(IpmiEventLog).run(make_ctx(tmp_path, fake))

    assert result.status == "pass"          # informational, by design
    assert "worth a look" in result.reason
    assert len(result.details["serious_entries"]) == 1
    assert "Uncorrectable ECC" in result.details["serious_entries"][0]


def test_routine_power_events_are_not_flagged(tmp_path, has_device):
    fake = FakeIpmitool()
    result = make_test(IpmiEventLog).run(make_ctx(tmp_path, fake))
    assert "Power off/down" not in str(result.details["serious_entries"])


def test_unreadable_event_log_skips(tmp_path, has_device):
    fake = FakeIpmitool(sel_ok=False)
    result = make_test(IpmiEventLog).run(make_ctx(tmp_path, fake))
    assert result.status == "skip"
    assert "readable System Event Log" in result.reason


def test_the_event_log_test_never_fails_certification():
    assert IpmiEventLog.severity == "informational"


def test_no_bmc_means_no_bmc_whatever_the_build_host_has(tmp_path, no_device, monkeypatch):
    """The fixture has to describe the machine completely, not partly.

    ``_bmc_declared`` asks two questions - device nodes, and the kernel's IPMI class - and only the
    first was stubbed. On a builder whose /sys/class/ipmi was populated the second answered for the
    real host, so a test describing a BMC-less machine ran the BMC check anyway and the package's
    own %check failed. Anything reading the host here is caught by making the raw glob explode.
    """
    def no_peeking(pattern):
        raise AssertionError(
            "read the build host at %r; stub it in the no_device fixture" % pattern
        )

    monkeypatch.setattr(ipmi_mod.glob, "glob", no_peeking)

    reason = make_test(BmcReachable).applicable(make_ctx(tmp_path, bmc={"present": False}))

    assert reason is not None
    assert "no IPMI device" in reason
