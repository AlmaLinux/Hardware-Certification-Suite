"""Backlight control: set a brightness, verify the display took it, restore.

Two routes to cover. The sysfs route uses a fake /sys/class/backlight tree so
every outcome is reachable without a laptop panel, including the one that
matters on GUI systems: a desktop power daemon snapping the value back. The
DDC/CI route uses a fake ddcutil so an external monitor's outcomes are
reachable without a monitor, including the case that produced this code -
a desktop with no kernel backlight device whose brightness is nonetheless
under system control.
"""

import glob
import os

import pytest

from alma_certify import procutil
from alma_certify.config import Config
from alma_certify.registry import RunContext
from alma_certify.validate.power import BacklightControl


def make_backlight(tmp_path, name="intel_backlight", *, maximum=1000,
                   current=800, kind="raw", actual=None, writable=True):
    d = tmp_path / "sys" / name
    d.mkdir(parents=True)
    (d / "type").write_text(kind + "\n")
    (d / "max_brightness").write_text(f"{maximum}\n")
    (d / "brightness").write_text(f"{current}\n")
    if actual is not None:
        (d / "actual_brightness").write_text(f"{actual}\n")
    if not writable:
        (d / "brightness").chmod(0o444)
    return d


@pytest.fixture
def patched(monkeypatch, tmp_path):
    """Point the test at the fake tree and skip the settle sleeps."""
    (tmp_path / "sys").mkdir(exist_ok=True)
    monkeypatch.setattr(
        BacklightControl, "_sysfs_devices",
        staticmethod(lambda: sorted(glob.glob(str(tmp_path / "sys" / "*")))),
    )
    monkeypatch.setattr(BacklightControl, "SETTLE_SECONDS", 0)
    monkeypatch.setattr(BacklightControl, "DDC_SETTLE_SECONDS", 0)
    return tmp_path


def make_test(**attrs):
    """A test instance with setup()'s attributes preset.

    setup() is deliberately not called: it would shell out to whatever
    ddcutil the machine running the suite's own tests happens to have.
    """
    test = BacklightControl()
    test._ddc_ready = False
    test._ddcutil_unavailable = False
    test._ddcutil_version = ""
    test._i2c_dev_loaded = False
    for key, value in attrs.items():
        setattr(test, key, value)
    return test


def run_test(tmp_path, behavior="follow", cmd=None, **attrs):
    """behavior: how the fake panel responds to a write.

    follow  - actual_brightness tracks the write (working hardware)
    revert  - a daemon puts the old value back
    ignore  - the write lands nowhere useful
    """
    ctx = RunContext(Config.load("/nonexistent"), str(tmp_path))
    ctx.log = lambda msg: None
    if cmd is not None:
        ctx.cmd = cmd
    original_write = BacklightControl._write

    def fake_write(path, value):
        if behavior == "clamp":
            # A driver that silently stores something other than what it was
            # given, which is the one failure this test can attribute to it.
            with open(os.path.join(path, "brightness"), "w") as fh:
                fh.write("%d\n" % (value // 2))
            return
        original_write(path, value)
        actual = os.path.join(path, "actual_brightness")
        if not os.path.exists(actual):
            return
        if behavior == "follow":
            with open(actual, "w") as fh:
                fh.write("%d\n" % value)
        elif behavior == "ignore":
            # Moves the wrong way: the target always asks for a change away
            # from the current value, so pinning actual to the far end is a
            # response the write cannot explain. Simply not moving is
            # "revert" - a different outcome.
            with open(os.path.join(path, "max_brightness")) as fh:
                maximum = int(fh.read().strip())
            with open(actual, "w") as fh:
                fh.write("%d\n" % (maximum if value < maximum // 2 else 1))
        # "revert" leaves actual_brightness at its original value

    BacklightControl._write = staticmethod(fake_write)
    try:
        return make_test(**attrs).run(ctx)
    finally:
        BacklightControl._write = staticmethod(original_write)


# --- applicability ------------------------------------------------------------


def test_skips_when_nothing_is_plugged_in(patched, monkeypatch):
    """A headless server: no backlight device and no connected output."""
    monkeypatch.setattr(BacklightControl, "_connected_outputs", staticmethod(list))
    reason = make_test().applicable(
        RunContext(Config.load("/nonexistent"), str(patched))
    )
    assert reason is not None
    assert "no display attached" in reason
    # It must not claim anything about controllability it did not check.
    assert "not under system control" not in reason


def test_applicable_when_a_monitor_is_attached_without_a_backlight(patched,
                                                                  monkeypatch):
    """The Dell OptiPlex case: empty /sys/class/backlight, monitor plugged in,
    brightness reachable over DDC/CI. Skipping here was the bug."""
    monkeypatch.setattr(BacklightControl, "_connected_outputs",
                        staticmethod(lambda: ["card0-DP-1"]))
    assert make_test().applicable(
        RunContext(Config.load("/nonexistent"), str(patched))
    ) is None


def test_applicable_once_a_panel_is_present(patched):
    make_backlight(patched, actual=800)
    ctx = RunContext(Config.load("/nonexistent"), str(patched))
    assert make_test().applicable(ctx) is None


# --- sysfs route: the outcomes -------------------------------------------------


def test_panel_that_follows_the_write_passes(patched):
    make_backlight(patched, maximum=1000, current=800, actual=800)
    result = run_test(patched, "follow")

    assert result.status == "pass"
    entry = result.details["displays"][0]
    assert entry["result"] == "controlled"
    assert entry["interface"] == "sysfs"
    assert entry["type"] == "raw"
    assert entry["target"] != entry["original"]      # a real change was asked for
    assert entry["restored"] is True


def test_value_reverted_by_a_daemon_is_a_skip_not_a_failure(patched):
    """The GUI case: a power daemon owns the backlight and puts the brightness
    back, which says nothing about the hardware."""
    make_backlight(patched, maximum=1000, current=800, actual=800)
    result = run_test(patched, "revert")

    assert result.status == "skip"
    assert "reverted" in result.reason
    assert "console" in result.reason                # tells the operator what to do
    assert result.details["displays"][0]["result"] == "reverted"


def test_amdgpu_pwm_curve_scale_counts_as_control(patched, monkeypatch):
    """Regression, run 9aac4289: a Lenovo ThinkPad P16s on amdgpu.

    The driver accepted 299250 of 399000 and actual_brightness read a rock
    steady 267001, because amdgpu reports it through the panel's PWM curve.
    Requiring equality made every AMD laptop unverifiable; what proves control
    is that the panel moved the way it was told. Real numbers from that run.
    """
    make_backlight(patched, maximum=399000, current=95761, actual=None)
    # actual_brightness on its own curve: ~0.24 of range before, ~0.67 after.
    readings = iter([53_900] + [267_001] * 4)
    monkeypatch.setattr(BacklightControl, "_read_actual",
                        staticmethod(lambda path: next(readings)))
    result = run_test(patched, "follow")

    assert result.status == "pass"
    entry = result.details["displays"][0]
    assert entry["target"] == 299250            # matches the real run
    assert entry["baseline"] == 53_900          # measured before the write
    assert entry["result"] == "controlled"


def test_panel_that_does_not_respond_at_all_is_inconclusive(patched, monkeypatch):
    """Same scale mismatch, but the panel never moved: nothing to conclude."""
    make_backlight(patched, maximum=399000, current=95761, actual=None)
    monkeypatch.setattr(BacklightControl, "_read_actual",
                        staticmethod(lambda path: 53_900))
    result = run_test(patched, "follow")

    assert result.status == "skip"
    assert result.details["displays"][0]["result"] == "reverted"


def test_movement_the_wrong_way_is_not_control(patched, monkeypatch):
    """Dimming when we asked for brighter is somebody else's doing."""
    make_backlight(patched, maximum=399000, current=95761, actual=None)
    readings = iter([53_900] + [20_000] * 4)
    monkeypatch.setattr(BacklightControl, "_read_actual",
                        staticmethod(lambda path: next(readings)))
    result = run_test(patched, "follow")

    assert result.status == "skip"
    assert result.details["displays"][0]["result"] == "unverifiable"


def test_stable_unexpected_value_is_inconclusive_not_a_failure(patched):
    """The driver took the value and the panel reports something else, stably.
    From software that is ambiguous - the panel may have ignored it, or the
    driver may report actual_brightness on its own scale - so it is not a
    defect we can pin on the hardware."""
    make_backlight(patched, maximum=1000, current=800, actual=800)
    result = run_test(patched, "ignore")

    assert result.status == "skip"
    assert "different scale" in result.reason
    assert result.details["displays"][0]["result"] == "unverifiable"


def test_driver_that_stores_a_different_value_fails(patched):
    """The one hardware-attributable failure: the value was inside
    [1, max_brightness] and the driver still refused to keep it."""
    make_backlight(patched, maximum=1000, current=800, actual=800)
    result = run_test(patched, "clamp")

    assert result.status == "fail"
    entry = result.details["displays"][0]
    assert entry["result"] == "write rejected"
    assert "driver stored" in entry["error"]


def test_laptop_that_dims_itself_after_the_write_passes(patched, monkeypatch):
    """Regression, run 4f47867b: a Lenovo laptop on amdgpu whose brightness KDE
    controls fine. The write landed and something (idle dimming) then moved the
    panel to a value we never set, which a single late read called a failure.
    These are the real numbers from that run."""
    make_backlight(patched, maximum=399000, current=239400, actual=239400)
    readings = iter([239400, 99750, 52635, 52635, 52635])
    monkeypatch.setattr(BacklightControl, "_read_actual",
                        staticmethod(lambda path: next(readings)))
    result = run_test(patched, "follow")

    assert result.status == "pass"
    entry = result.details["displays"][0]
    assert entry["target"] == 99750                  # matches the real run
    assert entry["result"] == "controlled"
    assert entry["samples"] == [99750, 52635, 52635, 52635]


def test_value_moving_the_wrong_way_is_reported_as_contended(patched,
                                                               monkeypatch):
    """Still moving, and away from what we asked for, so the write does not
    explain it. A fade *toward* our value would be control, not contention."""
    make_backlight(patched, maximum=1000, current=800, actual=800)
    readings = iter([800, 900, 950, 980, 999])
    monkeypatch.setattr(BacklightControl, "_read_actual",
                        staticmethod(lambda path: next(readings)))
    result = run_test(patched, "follow")

    assert result.status == "skip"
    assert result.details["displays"][0]["result"] == "contended"


# --- restoring and edge cases -------------------------------------------------


def test_brightness_is_restored_afterwards(patched):
    d = make_backlight(patched, maximum=1000, current=800, actual=800)
    run_test(patched, "follow")
    assert (d / "brightness").read_text().strip() == "800"


def test_never_targets_zero(patched):
    """A display left fully dark looks like a dead machine."""
    make_backlight(patched, maximum=4, current=1, actual=1)
    result = run_test(patched, "follow")
    assert result.details["displays"][0]["target"] >= 1


def test_single_step_backlight_has_nothing_to_verify(patched):
    """max_brightness of 1 is an on/off stub, usually a vestigial acpi_video0.
    There is no range to set, so the machine is not at fault for it."""
    make_backlight(patched, maximum=1, current=1, actual=1)
    result = run_test(patched, "follow")
    assert result.status == "skip"
    assert "no adjustable range" in result.reason
    assert result.details["displays"][0]["result"] == "not adjustable"


def test_falls_back_to_brightness_when_actual_is_absent(patched):
    """Some drivers expose no actual_brightness at all."""
    make_backlight(patched, maximum=1000, current=800, actual=None)
    result = run_test(patched, "follow")
    assert result.status == "pass"


def test_one_working_panel_is_enough(patched):
    """A stale acpi_video0 alongside a working native control is common and
    should not fail the machine."""
    make_backlight(patched, "acpi_video0", maximum=1, current=1, actual=1)
    make_backlight(patched, "intel_backlight", maximum=1000, current=800,
                   actual=800)
    result = run_test(patched, "follow")
    assert result.status == "pass"
    kinds = {e["device"]: e["result"] for e in result.details["displays"]}
    assert kinds["intel_backlight"] == "controlled"
    assert kinds["acpi_video0"] == "not adjustable"


# --- DDC/CI route -------------------------------------------------------------

# Captured from ddcutil 2.2.1 on a machine with no monitor attached. Note that
# the exit status is 0 and the diagnostics share the left margin with real
# display blocks, which is why detect output is parsed rather than checked.
NO_DISPLAYS = """\
Device /dev/i2c-0 is not readable and writable.  Error = EACCES(13): Permission denied
Devices possibly used for DDC/CI communication cannot be opened: /dev/i2c-0
See https://www.ddcutil.com/i2c_permissions
No displays found.
"""

DETECT_TERSE = """\
Display 1
   I2C bus:  /dev/i2c-4
   DRM connector: card1-DP-1
   Monitor:  DEL:DELL U2415:7MT01A5C0BSL
"""

DETECT_TWO_PLUS_INVALID = """\
Display 1
   I2C bus:  /dev/i2c-4
   DRM connector: card1-DP-1
   Monitor:  DEL:DELL U2415:7MT01A5C0BSL
Invalid display
   I2C bus:  /dev/i2c-6
   Monitor:  ACI:ASUS VS228:
Display 2
   I2C bus:  /dev/i2c-7
   DRM connector: card1-HDMI-A-1
   Monitor:  GSM:LG HDR 4K:0x01010101
"""


class FakeDdcutil:
    """One simulated DDC/CI monitor, driven through ctx.cmd.

    behavior mirrors the sysfs fake: follow, revert, ignore, refuse.
    """

    def __init__(self, *, detect=DETECT_TERSE, current=60, maximum=100,
                 behavior="follow", supports_brightness=True, quantize=1):
        self.detect_output = detect
        self.current = current
        self.maximum = maximum
        self.behavior = behavior
        self.supports_brightness = supports_brightness
        self.quantize = quantize
        self.calls = []

    def cmd(self, argv, timeout=None, artifact=None, check=False):
        self.calls.append(list(argv))

        def done(stdout, rc=0):
            return procutil.CmdResult(argv=list(argv), returncode=rc,
                                      stdout=stdout, stderr="")

        if "detect" in argv:
            return done(self.detect_output)
        if "getvcp" in argv:
            if not self.supports_brightness:
                return done(
                    "VCP code 0x10 (Brightness): Unsupported feature code", rc=3
                )
            return done("VCP 10 C %d %d\n" % (self.current, self.maximum))
        if "setvcp" in argv:
            if self.behavior == "refuse":
                return done("setvcp failed", rc=1)
            wanted = int(argv[-1])
            if self.behavior == "follow":
                self.current = wanted - (wanted % self.quantize)
            elif self.behavior == "ignore":
                # The wrong way, not merely imprecise: a monitor that dims
                # when asked to dim is still under system control.
                self.current = self.maximum if wanted < self.maximum // 2 else 1
            # "revert" leaves self.current where it was
            return done("")
        raise AssertionError("unexpected ddcutil call: %r" % (argv,))


def run_ddc(tmp_path, monkeypatch, fake, **attrs):
    monkeypatch.setattr(BacklightControl, "_connected_outputs",
                        staticmethod(lambda: ["card1-DP-1"]))
    ctx = RunContext(Config.load("/nonexistent"), str(tmp_path))
    ctx.log = lambda msg: None
    ctx.cmd = fake.cmd
    return make_test(_ddc_ready=True, **attrs).run(ctx)


def test_detect_output_is_parsed_not_exit_code_checked():
    assert BacklightControl._parse_detect(NO_DISPLAYS) == []
    assert BacklightControl._parse_detect(DETECT_TERSE) == [{
        "display": "1", "bus": "4", "connector": "card1-DP-1",
        "monitor": "DEL:DELL U2415:7MT01A5C0BSL",
    }]


def test_invalid_display_blocks_are_not_treated_as_monitors():
    parsed = BacklightControl._parse_detect(DETECT_TWO_PLUS_INVALID)
    assert [d["bus"] for d in parsed] == ["4", "7"]


def test_monitor_under_ddc_control_passes(patched, monkeypatch):
    """The reported bug: no kernel backlight, brightness controllable anyway."""
    fake = FakeDdcutil(current=60, maximum=100)
    result = run_ddc(patched, monkeypatch, fake)

    assert result.status == "pass"
    entry = result.details["displays"][0]
    assert entry["interface"] == "ddc"
    assert entry["device"] == "card1-DP-1"
    assert entry["monitor"] == "DEL:DELL U2415:7MT01A5C0BSL"
    assert entry["result"] == "controlled"
    assert entry["restored"] is True
    assert fake.current == 60                        # put back where it started


def test_ddc_monitor_that_quantizes_still_counts_as_controlled(patched,
                                                               monkeypatch):
    """Monitors are free to round what they accept; stepping in fives is still
    the system controlling the brightness."""
    fake = FakeDdcutil(current=60, maximum=100, quantize=5)
    assert run_ddc(patched, monkeypatch, fake).status == "pass"


def test_ddc_write_reverted_is_a_skip(patched, monkeypatch):
    fake = FakeDdcutil(current=60, maximum=100, behavior="revert")
    result = run_ddc(patched, monkeypatch, fake)
    assert result.status == "skip"
    assert "reverted" in result.reason


def test_ddc_monitor_reporting_a_value_we_did_not_set_is_inconclusive(patched,
                                                                     monkeypatch):
    """The monitor took the write and reports something else. Same ambiguity as
    the sysfs case, so it is not the machine's defect."""
    fake = FakeDdcutil(current=60, maximum=100, behavior="ignore")
    result = run_ddc(patched, monkeypatch, fake)
    assert result.status == "skip"
    assert result.details["displays"][0]["result"] == "unverifiable"


def test_ddc_write_refused_fails(patched, monkeypatch):
    """setvcp failing is the monitor refusing outright, which is attributable."""
    fake = FakeDdcutil(current=60, maximum=100, behavior="refuse")
    result = run_ddc(patched, monkeypatch, fake)
    assert result.status == "fail"
    assert result.details["displays"][0]["result"] == "write refused"


def test_monitor_without_brightness_feature_skips(patched, monkeypatch):
    """Plenty of monitors answer DDC/CI but expose no brightness control. That
    is the monitor's limitation, not the system's defect."""
    fake = FakeDdcutil(supports_brightness=False)
    result = run_ddc(patched, monkeypatch, fake)
    assert result.status == "skip"
    assert "VCP 0x10" in result.details["displays"][0]["note"]
    assert "no brightness control over DDC/CI" in result.reason


def test_no_monitor_answers_ddc_skips_with_that_reason(patched, monkeypatch):
    fake = FakeDdcutil(detect=NO_DISPLAYS)
    result = run_ddc(patched, monkeypatch, fake)
    assert result.status == "skip"
    assert "no display answered DDC/CI" in result.reason


def test_missing_ddcutil_says_so_instead_of_guessing(patched, monkeypatch):
    """EPEL 8 ships no ddcutil, so el8 desktops cannot test this route. The
    skip has to name that rather than imply the hardware cannot dim."""
    monkeypatch.setattr(BacklightControl, "_connected_outputs",
                        staticmethod(lambda: ["card1-DP-1"]))
    ctx = RunContext(Config.load("/nonexistent"), str(patched))
    ctx.log = lambda msg: None
    result = make_test(_ddcutil_unavailable=True).run(ctx)

    assert result.status == "skip"
    assert "ddcutil is not available" in result.reason
    assert "AlmaLinux 8" in result.reason


def test_sysfs_wins_when_both_routes_exist(patched, monkeypatch):
    """A laptop docked to an external monitor: its own panel is the subject,
    so ddcutil is never called."""
    make_backlight(patched, maximum=1000, current=800, actual=800)
    fake = FakeDdcutil()
    monkeypatch.setattr(BacklightControl, "_connected_outputs",
                        staticmethod(lambda: ["card1-DP-1"]))
    result = run_test(patched, "follow", cmd=fake.cmd, _ddc_ready=True)

    assert result.status == "pass"
    assert [e["interface"] for e in result.details["displays"]] == ["sysfs"]
    assert fake.calls == []
