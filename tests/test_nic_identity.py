"""A NIC has to be nameable before it can be certified.

Reported: "I notice the NIC is not showing up as a component. Why is that? We should be detecting
NICs."

The collector found them all along - ``ip -j link`` enumerated every physical interface, with its
driver, MAC, PCI slot, and firmware. None of that names a *part*. "enp2s0 running r8169" is a local
fact about this kernel's view of the machine, not a product anybody can look up, so the catalog
had nothing to build an entry from and NICs were the one component class that never appeared.

The GPU collector had solved the same problem by joining ``lspci -vmmnnk``; the NIC collector
never did. This is that join - and it reports every name lspci gives without choosing between
them, because choosing is the server's job. See ``nic_identity`` there, and the note in this
module's docstring about what happened when the choosing lived here.
"""
import json
import os

import pytest

from alma_certify.inventory import network

LSPCI = """Slot:\t02:00.0
Class:\tEthernet controller [0200]
Vendor:\tRealtek Semiconductor Co., Ltd. [10ec]
Device:\tRTL8111/8168/8411 PCI Express Gigabit Ethernet Controller [8168]
SVendor:\tDell [1028]
SDevice:\tDevice [09a8]
Driver:\tr8169

Slot:\t03:00.0
Class:\tNetwork controller [0280]
Vendor:\tIntel Corporation [8086]
Device:\tWi-Fi 6 AX200 [2723]
Driver:\tiwlwifi

Slot:\t04:00.0
Class:\tEthernet controller [0200]
Vendor:\tIntel Corporation [8086]
Device:\tEthernet Controller X710 for 10GbE SFP+ [1572]
SVendor:\tIntel Corporation [8086]
SDevice:\tEthernet Converged Network Adapter X710-DA2 [0006]
Driver:\ti40e

Slot:\t00:02.0
Class:\tVGA compatible controller [0300]
Vendor:\tIntel Corporation [8086]
Device:\tCometLake-S GT2 [UHD Graphics 630] [9bc5]
Driver:\ti915
"""


@pytest.fixture
def inv_dir(tmp_path):
    (tmp_path / "lspci.txt").write_text(LSPCI)
    return str(tmp_path)


def _collect(inv_dir, monkeypatch, links, slots):
    """Run the collector against a fabricated machine.

    ``ip -j link`` and the /sys lookups are both stubbed, because the real ones describe the
    machine running the tests.
    """
    monkeypatch.setattr(
        network.procutil, "run_cmd",
        lambda *a, **k: type("R", (), {"ok": True, "stdout": json.dumps(links)})(),
    )
    monkeypatch.setattr(os.path, "isdir", lambda path: True)
    monkeypatch.setattr(network, "_pci_addr", lambda name: slots.get(name))
    monkeypatch.setattr(network, "_ethtool_info", lambda d, n: {"driver": "stub"})
    return network.collect(inv_dir)["nics"]


def test_every_lspci_name_is_reported(inv_dir, monkeypatch):
    """Verbatim, ids and all. Which of them is the product's name is the server's decision, so
    the bundle carries the evidence for it rather than the conclusion."""
    nics = _collect(
        inv_dir, monkeypatch,
        [{"ifname": "enp2s0", "address": "aa:bb:cc:dd:ee:ff"}],
        {"enp2s0": "0000:02:00.0"},
    )

    assert nics[0]["pci_ids"] == {
        "vendor": "Realtek Semiconductor Co., Ltd. [10ec]",
        "device": "RTL8111/8168/8411 PCI Express Gigabit Ethernet Controller [8168]",
        "subsystem_vendor": "Dell [1028]",
        # The placeholder pci.ids has for this subsystem, kept as written. Rejecting it is the
        # reader's job; the first version of this rejected it here and every bundle already
        # submitted kept the wrong answer.
        "subsystem_device": "Device [09a8]",
    }


def test_the_card_name_is_reported_when_there_is_one(inv_dir, monkeypatch):
    nics = _collect(
        inv_dir, monkeypatch, [{"ifname": "ens1f0"}], {"ens1f0": "0000:04:00.0"},
    )

    assert nics[0]["pci_ids"]["subsystem_device"] == (
        "Ethernet Converged Network Adapter X710-DA2 [0006]"
    )


def test_the_domain_prefix_does_not_break_the_join(inv_dir, monkeypatch):
    """``/sys/class/net/<if>/device`` resolves to "0000:02:00.0"; lspci prints "02:00.0".

    Comparing them raw matches nothing, which would leave every NIC unnamed while looking exactly
    like lspci had not reported it - a silent version of the reported bug.
    """
    assert network._normalize_slot("0000:02:00.0") == "02:00.0"
    assert network._normalize_slot("02:00.0") == "02:00.0"
    assert network._normalize_slot(None) == ""


def test_wireless_counts_too(inv_dir, monkeypatch):
    """PCI class 0280 is a network controller as much as 0200 is, and a laptop's Wi-Fi working on
    AlmaLinux is exactly the kind of enablement this catalog exists to record."""
    nics = _collect(
        inv_dir, monkeypatch, [{"ifname": "wlp3s0"}], {"wlp3s0": "0000:03:00.0"},
    )

    assert nics[0]["pci_ids"]["device"] == "Wi-Fi 6 AX200 [2723]"


def test_a_gpu_is_not_mistaken_for_a_nic(inv_dir, monkeypatch):
    """The class filter, asserted rather than assumed: 03xx sits in the same lspci output."""
    nics = _collect(
        inv_dir, monkeypatch, [{"ifname": "weird0"}], {"weird0": "0000:00:02.0"},
    )

    assert nics[0]["pci_ids"] == {}, "a display adapter is not a network device"


def test_a_nic_off_the_pci_bus_carries_no_names(inv_dir, monkeypatch):
    """USB ethernet, or an SoC's built-in MAC. The server treats a NIC it cannot name as one it
    cannot catalog, which is the honest outcome."""
    nics = _collect(inv_dir, monkeypatch, [{"ifname": "usb0"}], {"usb0": None})

    assert nics[0]["pci_ids"] == {}


def test_the_local_facts_are_still_recorded(inv_dir, monkeypatch):
    """The identity is added, nothing is taken away: the driver and slot are what a reviewer reads
    to tell an onboard port from an add-in card."""
    nics = _collect(
        inv_dir, monkeypatch,
        [{"ifname": "enp2s0", "address": "aa:bb:cc:dd:ee:ff", "operstate": "UP"}],
        {"enp2s0": "0000:02:00.0"},
    )

    assert nics[0]["name"] == "enp2s0"
    assert nics[0]["pci"] == "0000:02:00.0"
    assert nics[0]["driver"] == "stub"
    assert nics[0]["operstate"] == "UP"


def test_no_lspci_leaves_every_nic_unnamed_without_failing(tmp_path, monkeypatch):
    """A machine without pciutils still produces a report. Losing the whole inventory over a
    missing optional tool would be the wrong trade."""
    nics = _collect(
        str(tmp_path), monkeypatch, [{"ifname": "enp2s0"}], {"enp2s0": "0000:02:00.0"},
    )

    assert nics[0]["pci_ids"] == {}
