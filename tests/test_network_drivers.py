"""PCI network controllers must have drivers bound.

Why this exists separately from the link check: a controller with no driver
never gets a netdev, so `ip link` cannot see it at all. Unsupported wired or
wireless hardware would otherwise pass certification by being invisible.
"""

import pytest

from alma_certify.config import Config
from alma_certify.procutil import CmdResult
from alma_certify.registry import RunContext
from alma_certify.validate.network import PciNetworkDrivers

# Real `lspci -vmmnnk` output shapes, trimmed to the fields the test reads.
BOUND_ETHERNET = """\
Slot:\t05:00.0
Class:\tEthernet controller [0200]
Vendor:\tIntel Corporation [8086]
Device:\tEthernet 10G 2P X520 Adapter [154d]
Driver:\tixgbe
Module:\tixgbe
"""

UNBOUND_WIRELESS = """\
Slot:\t3b:00.0
Class:\tNetwork controller [0280]
Vendor:\tIntel Corporation [8086]
Device:\tWi-Fi 6 AX210/AX211/AX411 160MHz [2725]
Module:\tiwlwifi
"""

UNBOUND_NO_MODULE = """\
Slot:\t04:00.0
Class:\tEthernet controller [0200]
Vendor:\tExotic Networks [abcd]
Device:\t400GbE Unobtainium [0001]
"""

NON_NETWORK = """\
Slot:\t01:00.0
Class:\tVGA compatible controller [0300]
Vendor:\tNVIDIA Corporation [10de]
Device:\tAD102 [GeForce RTX 4090] [2684]
Driver:\tnvidia
"""


class FakeCtx(RunContext):
    def __init__(self, tmp_path, lspci_output, interfaces=None):
        super().__init__(Config.load("/nonexistent"), str(tmp_path))
        self._out = lspci_output
        self.interfaces = interfaces if interfaces is not None else {}

    def cmd(self, argv, timeout=None, artifact=None, check=False):
        if argv[:1] == ["lspci"]:
            return CmdResult(argv, 0, self._out, "")
        return CmdResult(argv, 0, "", "")


@pytest.fixture
def netdevs(monkeypatch):
    """Stand in for /sys/bus/pci/devices/<slot>/net."""
    mapping = {}

    def fake(slot):
        return mapping.get(slot, [])

    monkeypatch.setattr(
        "alma_certify.validate.network._interfaces_for", fake
    )
    return mapping


def test_all_controllers_bound_passes(tmp_path, netdevs):
    netdevs["05:00.0"] = ["enp5s0f0"]
    result = PciNetworkDrivers().run(FakeCtx(tmp_path, BOUND_ETHERNET))
    assert result.status == "pass"
    assert result.details["controllers"][0]["driver"] == "ixgbe"
    assert result.details["controllers"][0]["kind"] == "ethernet"


def test_unbound_wireless_fails_and_names_the_module(tmp_path, netdevs):
    """The case ip link cannot see: no driver, so no wlan interface exists."""
    result = PciNetworkDrivers().run(
        FakeCtx(tmp_path, BOUND_ETHERNET + "\n" + UNBOUND_WIRELESS)
    )
    assert result.status == "fail"
    assert "no driver bound" in result.reason
    assert "3b:00.0" in result.reason
    assert "wireless" in result.reason
    # naming the available module points at a different fix than "unsupported"
    assert "iwlwifi" in result.reason


def test_unbound_with_no_module_at_all_fails(tmp_path, netdevs):
    result = PciNetworkDrivers().run(FakeCtx(tmp_path, UNBOUND_NO_MODULE))
    assert result.status == "fail"
    assert "400GbE Unobtainium" in result.reason
    assert "module available" not in result.reason   # there is none to name


def test_wireless_is_classified_as_wireless(tmp_path, netdevs):
    result = PciNetworkDrivers().run(FakeCtx(tmp_path, UNBOUND_WIRELESS))
    kinds = [c["kind"] for c in result.details["controllers"]]
    assert kinds == ["wireless"]


def test_driver_bound_but_no_interface_fails(tmp_path, netdevs):
    """A driver that attaches but creates no netdev is its own fault mode."""
    result = PciNetworkDrivers().run(FakeCtx(tmp_path, BOUND_ETHERNET))
    assert result.status == "fail"
    assert "no interface created" in result.reason


def test_non_network_devices_are_ignored(tmp_path, netdevs):
    netdevs["05:00.0"] = ["enp5s0f0"]
    result = PciNetworkDrivers().run(
        FakeCtx(tmp_path, NON_NETWORK + "\n" + BOUND_ETHERNET)
    )
    assert result.status == "pass"
    slots = [c["pci"] for c in result.details["controllers"]]
    assert slots == ["05:00.0"]          # the GPU is not a network controller


def test_skips_when_there_are_no_network_controllers(tmp_path, netdevs):
    ctx = FakeCtx(tmp_path, NON_NETWORK)
    assert PciNetworkDrivers().applicable(ctx) == "no PCI network controllers found"


def test_device_and_vendor_ids_are_stripped_for_display(tmp_path, netdevs):
    netdevs["05:00.0"] = ["enp5s0f0"]
    result = PciNetworkDrivers().run(FakeCtx(tmp_path, BOUND_ETHERNET))
    entry = result.details["controllers"][0]
    assert entry["device"] == "Ethernet 10G 2P X520 Adapter"
    assert entry["vendor"] == "Intel Corporation"
